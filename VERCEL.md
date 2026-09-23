# Hosting on Vercel and connecting Telegram

On Vercel the bot doesn't run all the time. Telegram calls it (a **webhook**) whenever Meera posts a note or taps a
button, and **Vercel Cron** calls it for the Mon/Wed/Fri shortlist and the Sunday summary. Notes and drafts live in
a free **Supabase** Postgres database, because Vercel's own disk is wiped between calls.

```
Meera's channel / private chat ──> Telegram ──webhook──> Vercel (server.py) ──> Gemini, Google News
                                                             │
                                  Vercel Cron (Mon/Wed/Fri) ─┤
                                                             └──> Supabase Postgres (notes, drafts, stats)
```

Time needed: about 20 minutes. You need: the GitHub repo (already pushed), your bot token, your Gemini key.

---

## Step 1. Create a secret for the cron jobs

This is a password that only Vercel and you know. It protects the cron and setup pages.
Run this once and copy the output:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Step 2. Import the project into Vercel

1. Go to https://vercel.com and sign in with GitHub.
2. Click **Add New… → Project**, find **Ksrish018/Founderbot** and click **Import**.
   (If you don't see it, click "Adjust GitHub App Permissions" and give Vercel access to the repo.)
3. Leave **Framework Preset** as detected (FastAPI or Other) and **Root Directory** as `./`.
4. Open **Environment Variables** and add:

   | Name | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | your bot token from @BotFather |
   | `GEMINI_API_KEY` | your Gemini key (starts with `AQ.`) |
   | `CRON_SECRET` | the secret from Step 1 |
   | `TELEGRAM_NOTES_CHAT_ID` | `-1003976391640` |
   | `DATABASE_URL` | your Supabase **Transaction pooler** connection string, with the password filled in (the same line as in your local `.env`) |

   Leave `MEERA_USER_ID` out for now; you get it in Step 5.
   If you add `DATABASE_URL` here, skip Step 3.
5. Click **Deploy**.

## Step 3. Add the database (Supabase) - skip if you set DATABASE_URL in Step 2

1. In your Vercel project, open the **Storage** tab → **Create Database** (or **Browse Marketplace**) →
   choose **Supabase** → free plan → pick a region near India (e.g. Mumbai/Singapore) → **Create**.
2. When asked, connect it to this project for **all environments**. Vercel adds `POSTGRES_URL` and a few other
   variables automatically. You don't need to copy anything.
3. Redeploy so the new variables take effect: **Deployments** tab → the latest deployment → **⋯** → **Redeploy**.

The tables are created automatically on first use, with Row Level Security switched on so Supabase's public REST
API can't read them.

**Already created a Supabase project yourself?** Skip the marketplace and add the connection string by hand:

1. In Supabase, open the project → click **Connect** (top of the page) → **Connection String** tab →
   choose **Transaction pooler** (port `6543`). Copy the URI. It looks like
   `postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:6543/postgres`
2. Replace `[YOUR-PASSWORD]` with your database password (Project Settings → Database → Reset database password
   if you don't know it). If the password contains `@ # / ? : %`, pick a new letters-and-numbers password instead.
3. In Vercel add it as the environment variable `DATABASE_URL`, then redeploy.

Use the **Transaction pooler**, not "Direct connection": the direct address is IPv6-only and Vercel can't reach it.
You don't need the Supabase project URL or API keys; the bot talks to Postgres directly.

## Step 4. Connect Telegram (one click)

Make sure the bot is an **admin of the notes channel** with "Post Messages" (see README step 3). Then open this URL
in your browser, with your project's domain and your cron secret:

```
https://<your-project>.vercel.app/api/setup?key=<CRON_SECRET>
```

It creates the tables, imports the 5 notes and 15 published pieces, checks Gemini, and tells Telegram where to send
updates. You'll see something like:

```json
{
  "database": "postgres",
  "notes_total": 5, "exemplars_total": 15,
  "gemini_model": "gemini-3.7-flash",
  "bot": "@your_bot", "webhook_url": "https://<your-project>.vercel.app/api/telegram",
  "bot_is_channel_admin": true, "webhook_last_error": null,
  "next_step": "Send /start to the bot from Meera's account, ...", "ok": true
}
```

If `gemini_error` appears, the key is wrong or the model isn't available to it. The message says which.

## Step 5. Tell the bot who Meera is

1. From Meera's Telegram account, open a private chat with the bot and send `/start`.
   The bot replies with her numeric user ID.
2. In Vercel: **Settings → Environment Variables** → add `MEERA_USER_ID` = that number → **Save**.
3. **Redeploy** again (Deployments → ⋯ → Redeploy).
4. Send `/health` to the bot. Every line should be ✅. Then try `/triage`.

From now on the bot answers only Meera, and posting a note in the channel is all she needs to do.

---

## How the schedule works on Vercel

`vercel.json` defines two cron jobs (times are UTC):

| Job | Schedule | India time |
|---|---|---|
| Shortlist | `30 2 * * 1,3,5` | Mon/Wed/Fri 08:00 IST |
| Weekly summary | `30 12 * * 0` | Sunday 18:00 IST |

On Vercel's free **Hobby** plan a cron job may fire any time within that hour (08:00-08:59). `/pause` and `/resume`
still work; the cron checks them. To change the times, edit `vercel.json` and push. `TRIAGE_SCHEDULE` in `.env` only
applies when running locally with `python -m app`.

Each Vercel call can run for up to 5 minutes (set in `vercel.json`), which is enough for a draft (typically 30-90
seconds: news lookup, drafting, and up to two voice-lint retries).

## Troubleshooting

- **The bot doesn't respond.** Open `/api/setup?key=...` again and read `webhook_last_error`. Then check
  Vercel → your project → **Logs**.
- **You changed an environment variable and nothing happened.** Vercel only applies changes to new deployments.
  Redeploy.
- **`401` errors in `webhook_last_error`.** Vercel Deployment Protection is blocking Telegram. Settings →
  Deployment Protection → make sure production isn't protected (it isn't by default).
- **You ran `python -m app` on your laptop.** Local polling removes the webhook. Open `/api/setup?key=...` again
  to reconnect the Vercel version. Run only one copy of the bot at a time.
- **Supabase says the project is paused.** Free Supabase projects pause after a long idle period. The Mon/Wed/Fri
  cron normally keeps it awake; if it pauses, restore it from the Supabase dashboard.

## What changed compared to running it locally

| Local (`python -m app`) | Vercel |
|---|---|
| Long polling | Telegram webhook at `/api/telegram`, verified with a secret derived from the bot token |
| SQLite file `data/app.db` | Supabase Postgres (`POSTGRES_URL`) |
| Built-in scheduler | Vercel Cron → `/api/cron/triage`, `/api/cron/weekly` |
| Import with `python -m app.importer ...` | `/api/setup` imports the bundled seed data |
| `/facts` answers | stored in the database (`fact_resolutions`), not in `config/facts.yaml` |
