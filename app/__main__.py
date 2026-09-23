"""Entrypoint: python -m app"""
from __future__ import annotations

import logging
import secrets
import sys

from telegram import Update

from .config import get_settings, mask
from .db import DB
from .gemini_client import Gemini, GeminiAuthError
from .telegram_bot import build_application


class RedactSecrets(logging.Filter):
    """Masks the bot token and Gemini key anywhere they might appear in a log record."""

    def __init__(self, *secrets_: str):
        super().__init__()
        self.secrets = [s for s in secrets_ if s and len(s) > 8]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = msg
        for s in self.secrets:
            red = red.replace(s, mask(s))
        if red != msg:
            record.msg, record.args = red, ()
        return True


def setup_logging(level: str, *secrets_: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactSecrets(*secrets_))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "telegram.ext.ExtBot", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    settings = get_settings()
    setup_logging(settings.log_level, settings.telegram_bot_token, settings.gemini_api_key)
    log = logging.getLogger("app")

    missing = [k for k in ("telegram_bot_token", "gemini_api_key") if not getattr(settings, k)]
    if missing:
        log.error("Missing in .env: %s. Copy .env.example to .env and paste the values in.",
                  ", ".join(m.upper() for m in missing))
        return 1
    log.info("Starting with %s", settings)

    db = DB(settings.db_target)
    gemini = Gemini(settings, db)
    app = build_application(settings, db, gemini)
    try:
        if settings.webhook_url:
            path = "tg"
            app.run_webhook(listen="0.0.0.0", port=settings.webhook_port, url_path=path,
                            webhook_url=f"{settings.webhook_url.rstrip('/')}/{path}",
                            secret_token=secrets.token_urlsafe(32), allowed_updates=Update.ALL_TYPES)
        else:
            app.run_polling(allowed_updates=Update.ALL_TYPES)
    except GeminiAuthError as e:
        log.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
