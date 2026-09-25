"""Vercel entrypoint (FastAPI). Vercel detects `app` in server.py and runs it as one serverless function.

Routes
  POST /api/telegram       Telegram webhook (verified with the secret Telegram echoes back)
  GET  /api/cron/triage    Vercel Cron, Mon/Wed/Fri ~08:00 IST: score notes, send the shortlist
  GET  /api/cron/weekly    Vercel Cron, Sunday ~18:00 IST: weekly summary
  GET  /api/setup?key=...  One-time setup: create tables, import seed data, check Gemini, register the webhook
  GET  /                   Web dashboard (password protected); its API lives under /api/web

Nothing here posts anywhere public. The only outbound messages go to Meera's private chat with the bot.
"""
from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from telegram import BotCommand, Update

from app.__main__ import setup_logging
from app.config import DATA_DIR, ROOT, get_settings, mask
from app.db import DB
from app.gemini_client import Gemini, GeminiAuthError, GeminiUnavailable
from app.importer import import_notes, import_published
from app.telegram_bot import COMMANDS, build_application, job_weekly, run_triage
from app.web import build_router

settings = get_settings()
setup_logging(settings.log_level, settings.telegram_bot_token, settings.gemini_api_key, settings.cron_secret,
              settings.database_url)
log = logging.getLogger("server")
ON_VERCEL = bool(os.environ.get("VERCEL"))
ALLOWED_UPDATES = ["message", "edited_message", "channel_post", "edited_channel_post", "callback_query"]

app = FastAPI(title="Skinstinct content engine", docs_url=None, redoc_url=None, openapi_url=None)
_db: DB | None = None


def get_db() -> DB:
    """One connection per warm instance; reconnects if the pooler dropped it."""
    global _db
    if ON_VERCEL and not settings.database_url:
        raise HTTPException(500, "No database configured. Connect Supabase (or Neon) to this Vercel project so "
                                 "POSTGRES_URL / DATABASE_URL is set, then redeploy.")
    if _db is None:
        _db = DB(settings.db_target)
    return _db


@asynccontextmanager
async def telegram_app():
    db = get_db()
    tg = build_application(settings, db, Gemini(settings, db), polling=False)
    await tg.initialize()
    try:
        yield tg
    finally:
        await tg.shutdown()


def _authorised(request: Request, key: str = "") -> None:
    if not settings.cron_secret:
        raise HTTPException(500, "CRON_SECRET is not set in the project's environment variables.")
    bearer = request.headers.get("authorization", "")
    ok = hmac.compare_digest(bearer, f"Bearer {settings.cron_secret}") or \
        (key and hmac.compare_digest(key, settings.cron_secret))
    if not ok:
        raise HTTPException(401, "Unauthorised")


app.include_router(build_router(lambda: settings, get_db, lambda: telegram_app()))


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(ROOT / "web" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def status():
    return {"service": "Skinstinct content engine", "ok": True}


@app.post("/api/telegram")
async def telegram_webhook(request: Request):
    token = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(token, settings.telegram_webhook_secret):
        raise HTTPException(403, "Forbidden")
    data = await request.json()
    update_id = data.get("update_id")
    if update_id is not None and not get_db().first_time_update(int(update_id)):
        return {"ok": True, "duplicate": True}  # Telegram retried an update we already handled
    async with telegram_app() as tg:
        try:
            await tg.process_update(Update.de_json(data, tg.bot))
        except Exception:
            log.exception("Update %s failed", update_id)
    # Always 200, otherwise Telegram keeps re-sending the same update.
    return {"ok": True}


@app.get("/api/cron/triage")
async def cron_triage(request: Request):
    _authorised(request)
    db = get_db()
    if db.get_kv("triage_paused") == "1":
        return {"ok": True, "skipped": "paused"}
    if not settings.review_chat:
        return {"ok": False, "skipped": "MEERA_USER_ID not set"}
    async with telegram_app() as tg:
        await run_triage(SimpleNamespace(bot=tg.bot, application=tg), announce_empty=True)
    return {"ok": True}


@app.get("/api/cron/weekly")
async def cron_weekly(request: Request):
    _authorised(request)
    if not settings.review_chat:
        return {"ok": False, "skipped": "MEERA_USER_ID not set"}
    async with telegram_app() as tg:
        await job_weekly(SimpleNamespace(bot=tg.bot, application=tg))
    return {"ok": True}


@app.get("/api/setup")
async def setup(request: Request, key: str = ""):
    """Safe to run more than once. Visit https://<your-app>.vercel.app/api/setup?key=<CRON_SECRET>."""
    _authorised(request, key)
    report: dict = {}
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token),
                              ("GEMINI_API_KEY", settings.gemini_api_key)] if not v]
    if missing:
        return {"ok": False, "missing_env": missing}

    db = get_db()
    report["database"] = "postgres" if db.is_pg else "sqlite (local only)"
    report["notes_imported_now"] = import_notes(db, DATA_DIR / "notes")
    report["exemplars_imported_now"] = import_published(db, DATA_DIR / "published")
    report["notes_total"] = db.one("SELECT COUNT(*) c FROM notes")["c"]
    report["exemplars_total"] = db.one("SELECT COUNT(*) c FROM exemplars")["c"]

    try:
        report["gemini_model"] = await Gemini(settings, db).health_check()
    except (GeminiAuthError, GeminiUnavailable) as e:
        report["gemini_error"] = str(e)

    prod = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")
    base = f"https://{prod}" if prod else str(request.base_url).rstrip("/")
    async with telegram_app() as tg:
        await tg.bot.set_webhook(url=f"{base}/api/telegram", secret_token=settings.telegram_webhook_secret,
                                 allowed_updates=ALLOWED_UPDATES, max_connections=10)
        await tg.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        info = await tg.bot.get_webhook_info()
        me = await tg.bot.get_me()
        try:
            member = await tg.bot.get_chat_member(settings.telegram_notes_chat_id, me.id)
            report["bot_is_channel_admin"] = member.status in ("administrator", "creator")
        except Exception as e:
            report["bot_is_channel_admin"] = f"unknown ({e.__class__.__name__}: add the bot as a channel admin)"
    report.update(bot=f"@{me.username}", webhook_url=info.url, webhook_pending_updates=info.pending_update_count,
                  webhook_last_error=info.last_error_message, meera_user_id_set=bool(settings.meera_user_id),
                  notes_channel=settings.telegram_notes_chat_id)
    report["next_step"] = ("Send /start to the bot from Meera's account, add MEERA_USER_ID in Vercel and redeploy."
                           if not settings.meera_user_id else "Send /health to the bot from Meera's account.")
    report["ok"] = "gemini_error" not in report
    log.info("Setup ran: bot=%s webhook=%s token=%s", report["bot"], info.url, mask(settings.telegram_bot_token))
    return report
