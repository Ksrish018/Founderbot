"""Settings loaded from .env. Secrets never leave this object except to the SDKs that need them."""
from __future__ import annotations

import hashlib
from datetime import time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

# PTB run_daily: 0 = Sunday ... 6 = Saturday
_DAYS = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}


def mask(secret: str | None) -> str:
    """8957…LEB4 style masking for any secret that might reach a log line."""
    if not secret:
        return "<empty>"
    s = str(secret)
    if len(s) <= 8:
        return "…"
    return f"{s[:4]}…{s[-4:]}"


def parse_schedule(spec: str) -> tuple[tuple[int, ...], time]:
    """'MON,WED,FRI@08:00' -> ((1, 3, 5), time(8, 0))"""
    days_part, _, time_part = spec.partition("@")
    days = tuple(sorted(_DAYS[d.strip().upper()[:3]] for d in days_part.split(",") if d.strip()))
    hh, mm = (time_part or "08:00").strip().split(":")
    return days, time(int(hh), int(mm))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    telegram_notes_chat_id: int = -1003976391640
    meera_user_id: int | None = None
    review_chat_id: int | None = None
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"
    gemini_fallback_models: str = "gemini-3.6-flash,gemini-3.1-flash-lite"
    timezone: str = "Asia/Kolkata"
    triage_schedule: str = "MON,WED,FRI@08:00"
    shortlist_size: int = 4
    news_lookback_days: int = 30
    # If nothing from a trusted publisher turns up in that window, look back this far before giving up.
    news_max_lookback_days: int = 60
    # Only cite publishers listed as trusted in config/news_sources.yaml (blocked ones are never used).
    news_trusted_only: bool = True
    # Professor's workflow: every new note is scored straight away; notes scoring >= MIN_SHORTLIST_SCORE are drafted
    # (with a Google News check) and sent for review. Low scores are rejected with a reason. Meera still approves.
    auto_draft_on_capture: bool = True
    auto_draft_top_n: int = 0
    allow_hashtags: bool = False
    capture_reaction: bool = False
    db_path: str = "data/app.db"
    # Postgres connection string. On Vercel the Supabase/Neon integration sets one of these automatically.
    database_url: str = Field("", validation_alias=AliasChoices("DATABASE_URL", "POSTGRES_URL"))
    # Vercel sends "Authorization: Bearer <CRON_SECRET>" to cron endpoints; also protects /api/setup.
    cron_secret: str = ""
    log_level: str = "INFO"
    draft_temperature: float = 0.7
    triage_temperature: float = 0.2
    price_input_per_m: float = 0.30
    price_output_per_m: float = 2.50
    webhook_url: str = ""
    webhook_port: int = 8443
    min_shortlist_score: int = Field(6)

    @field_validator("meera_user_id", "review_chat_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return v

    @property
    def review_chat(self) -> int | None:
        """Where shortlists and drafts go. Default: the notes channel itself (single chat).
        Set REVIEW_CHAT_ID to Meera's user ID to review in a private chat with the bot instead."""
        return self.review_chat_id or self.telegram_notes_chat_id

    @property
    def single_chat(self) -> bool:
        """REVIEW_CHAT_ID set to the notes channel: notes, shortlists, drafts and commands all live in the channel."""
        return self.review_chat == self.telegram_notes_chat_id

    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.gemini_fallback_models.split(",") if m.strip()]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_target(self) -> str:
        """Postgres URL if configured, else the local SQLite file."""
        return self.database_url or str(self.db_file)

    @property
    def telegram_webhook_secret(self) -> str:
        """Secret Telegram echoes in X-Telegram-Bot-Api-Secret-Token; derived from the bot token."""
        return hashlib.sha256(f"{self.telegram_bot_token}:webhook".encode()).hexdigest()[:48]

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else ROOT / p

    def __repr__(self) -> str:  # never print secrets
        return (f"Settings(bot_token={mask(self.telegram_bot_token)}, gemini_key={mask(self.gemini_api_key)}, "
                f"model={self.gemini_model}, notes_chat={self.telegram_notes_chat_id})")

    __str__ = __repr__


@lru_cache
def get_settings() -> Settings:
    return Settings()
