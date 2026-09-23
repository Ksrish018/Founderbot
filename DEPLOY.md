# Deploying

**For Vercel, see [VERCEL.md](VERCEL.md).** This page covers running the bot as a long-running process.

The bot uses **long polling**, so it needs no public URL. It only has to keep running somewhere.

## Option 1: this laptop (simplest, for testing)

```bash
python -m app
```

The bot runs while the terminal is open and the laptop is awake. Scheduled shortlists (Mon/Wed/Fri 08:00 IST)
only arrive if it's running at that time.

## Option 2: a small VM (always on)

On any Linux VM (1 vCPU / 512 MB is enough):

```bash
git clone <your repo> skinstinct && cd skinstinct
cp .env.example .env        # then paste the real values in with nano .env
docker build -t skinstinct .
docker run -d --name skinstinct --restart unless-stopped --env-file .env \
  -v $PWD/data:/app/data -v $PWD/config:/app/config skinstinct
docker exec skinstinct python -m app.importer notes data/notes/
docker exec skinstinct python -m app.importer published data/published/
docker logs -f skinstinct
```

Mounting `config/` keeps the fact-bank resolutions you make with `/facts`.

## Option 3: Railway or Render

1. Push this folder to a private GitHub repo. `.env` and `data/app.db` are git-ignored, so no secrets are pushed.
2. Create a new **background worker** (Render) or service (Railway) from the repo. It builds from the `Dockerfile`.
3. Add every variable from `.env.example` in the dashboard's environment settings.
4. Attach a persistent disk/volume at `/app/data` (otherwise notes and drafts are lost on each deploy).
5. Run the two importer commands once from the service shell.

Run **one** copy of the bot only. Two copies polling the same token will fight over updates.

## Webhook mode (optional, later)

Set `WEBHOOK_URL=https://your-domain` and `WEBHOOK_PORT` in `.env`, and install the extra:
`pip install "python-telegram-bot[webhooks]"`. Leave `WEBHOOK_URL` empty to keep long polling.
