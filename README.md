# Skinstinct Content Engine

A Telegram bot that turns Meera's private channel notes into LinkedIn drafts in her voice.

- Meera keeps dropping notes (text or voice) into her private Telegram channel, exactly as she does now.
- Three mornings a week (Mon/Wed/Fri 08:00 IST) the bot sends her a shortlist of the strongest notes.
- She taps **Draft this**. The bot looks for a current news angle on Google News, writes a draft in her voice,
  checks it against her voice rules, and sends it to her private chat with the bot.
- She taps **Approve**, **Revise**, **Regenerate**, **Change angle**, **No news** or **Reject**.
- When she approves, she gets a clean copy-ready version and **pastes it into LinkedIn herself**.
  The bot never posts anywhere.

The full build brief is in `CLAUDE.md`. Meera's voice contract is `prompts/voice_skills.txt`.

**Hosting on Vercel?** Follow [VERCEL.md](VERCEL.md) instead of the local setup below.

---

## Setup (about 15 minutes, no coding)

### 1. Install Python and the packages

You need Python 3.11 or newer. In a terminal in this folder:

```bash
pip install -r requirements-dev.txt
```

### 2. Fill in your secrets

Open the file `.env` in this folder (a copy of `.env.example`) in any text editor and paste in:

| Line | What to paste | Where to get it |
|---|---|---|
| `TELEGRAM_BOT_TOKEN=` | The bot token | The message from @BotFather when you created the bot |
| `GEMINI_API_KEY=` | Your Gemini key | Google AI Studio. New keys start with `AQ.`, which is correct |
| `MEERA_USER_ID=` | Meera's numeric Telegram ID | Step 4 below |

Paste each value on one line, with no spaces or quotation marks. **Never share `.env` or commit it to git**
(it's already in `.gitignore`).

`TELEGRAM_NOTES_CHAT_ID` is already set to the notes channel `-1003976391640`.

### 3. Make the bot an admin of the notes channel

A bot only sees channel posts where it's an administrator.
Open the channel → channel name → **Administrators** → **Add Admin** → pick the bot → enable **Post Messages**
(you can leave everything else off) → **Save**.

### 4. Get Meera's user ID

Start the bot (step 6), then Meera opens a private chat with the bot and sends `/start`.
The bot replies with her numeric ID. Put it in `.env` as `MEERA_USER_ID=...`, then stop the bot
(Ctrl+C) and start it again.

From then on, the bot ignores everyone except Meera.

### 5. Load the backlog

The Telegram API can't read old channel messages, so the historical notes and published pieces are imported
from files:

```bash
python -m app.importer notes data/notes/
python -m app.importer published data/published/
```

`data/notes/` holds the raw notes (`.txt`, `.md` or `.rtf`, one per file, or several in one file separated by a
`---` line). `data/published/` holds the 15 published pieces (the seed PDF, or `.txt`/`.md` files with a
`Category:` line). Running the importer again doesn't create duplicates.

### 6. Run it

```bash
python -m app
```

On startup the bot checks Telegram and Gemini. If the Gemini key is rejected, it stops and says why.

In Meera's private chat, send `/health`. Everything should show ✅.

---

## Commands (Meera's private chat)

| Command | What it does |
|---|---|
| `/triage` | Score new notes now and send the shortlist |
| `/notes [n]` | Latest notes with their ids |
| `/draft <id>` | Draft a specific note |
| `/final <text>` | Record what you actually posted (or reply `final` + your text to a draft) |
| `/facts` | Resolve the four fact-bank conflicts (C1-C4) |
| `/stats` | Progress against 3 posts a week and 15 minutes a post |
| `/pause` / `/resume` | Stop or restart the scheduled shortlists |
| `/health` | Check Telegram, Gemini, Google News and the database |

A weekly summary arrives on Sundays at 18:00 IST.

## How it keeps drafts safe

- **Only Meera picks notes and approves posts.** `AUTO_DRAFT_TOP_N=0`, and there is no LinkedIn integration.
- **No invented facts.** Drafts may use only the note, the canonical facts in `config/facts.yaml`, and a news item
  actually retrieved from Google News (shown with its link). Anything else becomes `[DATA NEEDED: …]` or
  `[SOURCE NEEDED: …]`, and the bot won't let her approve until those are gone.
- **Voice lint.** Every draft is checked against `config/lint_rules.yaml`: no `!`, emoji, em dashes, bullets,
  hashtags, rhetorical questions, banned hype, or American spellings, and 380-520 words. A failing draft is
  regenerated up to twice; if it still fails, it arrives with a visible ⚠️ list.
- **Fact conflicts.** The four known contradictions in her past writing (launch timeline, return metrics,
  niacinamide %, which product is in development) are blocked until Meera resolves them with `/facts`.

## Settings you might change (in `.env`)

| Setting | Default | Notes |
|---|---|---|
| `TRIAGE_SCHEDULE` | `MON,WED,FRI@08:00` | Days and time of the shortlist, in `TIMEZONE` |
| `SHORTLIST_SIZE` | `4` | Notes per shortlist |
| `NEWS_LOOKBACK_DAYS` | `14` | How recent a news item must be |
| `CAPTURE_REACTION` | `false` | `true` adds a 👍 reaction in the channel when a note is captured |
| `ALLOW_HASHTAGS` | `false` | Meera's call. If `true`, at most 3 on the last line |
| `GEMINI_MODEL` | `gemini-3.7-flash` | Falls back to `GEMINI_FALLBACK_MODELS` if unavailable |
| `PRICE_INPUT_PER_M` / `PRICE_OUTPUT_PER_M` | 0.30 / 2.50 | USD per million tokens, for cost estimates only |

## Development

```bash
python -m pytest
```

The tests cover the linter (all 15 published pieces pass, all §16 off-voice samples fail), the importer, news
filtering, triage, the drafting retry loop, and the review gate end to end with a fake Telegram bot.

See [VERCEL.md](VERCEL.md) for Vercel, or `DEPLOY.md` for an always-on server (VM, Railway, Render).
