"""Web dashboard API: the same workflow as the Telegram channel, in a browser.

Everything reads and writes the same database, and every action is mirrored into the Telegram review chat, so the
channel and the dashboard stay in sync. Access is protected by DASHBOARD_PASSWORD (falls back to CRON_SECRET).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import drafting, metrics, telegram_bot as tb
from .config import Settings
from .db import DB, iso
from .gemini_client import Gemini, GeminiAuthError, GeminiUnavailable

log = logging.getLogger(__name__)
COOKIE = "mce_session"
SESSION_SECONDS = 14 * 24 * 3600


# ---- auth ---------------------------------------------------------------------------
def _password(settings: Settings) -> str:
    return settings.dashboard_password or settings.cron_secret


def _sign(settings: Settings, expires: int) -> str:
    key = hashlib.sha256(f"{_password(settings)}:{settings.telegram_bot_token}:web".encode()).digest()
    return f"{expires}.{hmac.new(key, str(expires).encode(), hashlib.sha256).hexdigest()}"


def _valid(settings: Settings, token: str | None) -> bool:
    if not token or "." not in token or not _password(settings):
        return False
    exp, _, _ = token.partition(".")
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(token, _sign(settings, int(exp)))


# ---- Telegram mirroring ---------------------------------------------------------------
class NullBot:
    """Used when Telegram isn't configured (local preview). Records nothing, returns fake message ids."""
    _next = -1_000_000

    async def send_message(self, *a, **k):
        NullBot._next -= 1
        return SimpleNamespace(message_id=NullBot._next)


class MirrorBot:
    """Sends to Telegram, but never lets a Telegram failure break a web action."""

    def __init__(self, bot):
        self.bot = bot

    async def send_message(self, *a, **k):
        try:
            return await self.bot.send_message(*a, **k)
        except Exception as e:
            log.warning("Telegram mirror failed: %s", e.__class__.__name__)
            return await NullBot().send_message()


class WebQuery:
    """Stands in for a Telegram callback query so the bot's button handlers can be reused unchanged."""
    message = SimpleNamespace(text="")

    async def answer(self, *a, **k):
        return None

    async def edit_message_reply_markup(self, *a, **k):
        return None

    async def edit_message_text(self, *a, **k):
        return None


# ---- serialisation -----------------------------------------------------------------------
def _j(v: Any, default: Any) -> Any:
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def draft_view(db: DB, d) -> dict:
    meta, lint = _j(d["meta_json"], {}), _j(d["lint_json"], {})
    req = db.one("SELECT * FROM draft_requests WHERE id=?", (d["request_id"],))
    item = db.one("SELECT * FROM news_items WHERE id=?", (d["news_item_id"],)) if d["news_item_id"] else None
    qa = meta.get("self_qa") or {}
    return {
        "id": d["id"], "version": d["version"], "status": d["status"], "text": d["post_text"],
        "note_ids": _j(d["note_ids"], []), "category": meta.get("category"), "opening_type": meta.get("opening_type"),
        "opening_name": tb.OPENING_NAMES.get(meta.get("opening_type") or "", ""),
        "word_count": lint.get("word_count"), "qa_total": qa.get("total") or sum(x.get("score", 0) for x in qa.get("scores", [])),
        "lint_ok": lint.get("ok", True), "lint_hard": lint.get("hard", []), "lint_soft": lint.get("soft", []),
        "retries": d["lint_retries"], "placeholders": meta.get("placeholders_in_text") or [],
        "verify_flags": meta.get("verify_flags") or [], "note_to_meera": meta.get("note_to_meera") or "",
        "fact_check": meta.get("fact_check"),
        "revision_instruction": d["revision_instruction"], "reject_reason": d["reject_reason"],
        "delivered_at": d["delivered_at"], "decided_at": d["decided_at"],
        "news": ({"title": item["title"], "publisher": item["source"], "domain": item["source_domain"],
                  "credibility": item["credibility"], "date": (item["published_at"] or "")[:10], "link": item["link"]}
                 if item else None),
        "news_reason": req["news_reason"] if req else None,
        "news_status": req["news_status"] if req else None,
        "final": (lambda f: {"text": f["final_text"], "edit_ratio": f["edit_ratio"]} if f else None)(
            db.one("SELECT * FROM finals WHERE draft_id=? ORDER BY id DESC LIMIT 1", (d["id"],))),
    }


def note_view(db: DB, n) -> dict:
    sc = db.latest_score(n["id"])
    drafts = [r for r in db.q("SELECT * FROM drafts WHERE note_ids LIKE ? ORDER BY id DESC LIMIT 20",
                              (f"%{n['id']}%",)) if n["id"] in _j(r["note_ids"], [])]
    current = next((r for r in drafts if r["status"] in ("delivered", "approved", "rejected")), drafts[0] if drafts else None)
    return {
        "id": n["id"], "source": n["source"], "type": n["type"], "text": db.note_body(n), "status": n["status"],
        "created_at": n["created_at"],
        "score": ({"value": sc["publishability"], "category": sc["category"], "reason": sc["reason"],
                   "gap": sc["core_gap"], "why_not": sc["not_publishable_reason"],
                   "risk": _j(sc["risk_flags"], [])} if sc else None),
        "draft": draft_view(db, current) if current else None,
        "draft_versions": len(drafts),
    }


# ---- router ---------------------------------------------------------------------------------
class Login(BaseModel):
    password: str


class NewNote(BaseModel):
    text: str = Field(min_length=1, max_length=6000)


class Action(BaseModel):
    instruction: str | None = Field(None, max_length=3000)
    reason: str | None = None   # o | w | k | n
    text: str | None = Field(None, max_length=10000)


def build_router(get_settings: Callable[[], Settings], get_db: Callable[[], DB],
                 telegram_app: Callable[[], Any]) -> APIRouter:
    r = APIRouter(prefix="/api/web")

    def auth(request: Request) -> None:
        if not _valid(get_settings(), request.cookies.get(COOKIE)):
            raise HTTPException(401, "Please log in")

    @asynccontextmanager
    async def bot_context() -> AsyncIterator[SimpleNamespace]:
        settings, db = get_settings(), get_db()
        state = tb.State(settings=settings, db=db, gemini=Gemini(settings, db))
        async with AsyncExitStack() as stack:
            bot: Any = NullBot()
            if settings.telegram_bot_token and settings.web_mirror_telegram:
                try:
                    tg = await stack.enter_async_context(telegram_app())
                    bot = MirrorBot(tg.bot)
                except Exception as e:  # Telegram unreachable: the dashboard still works
                    log.warning("Telegram unavailable for web action: %s", e.__class__.__name__)
            yield SimpleNamespace(bot=bot, application=SimpleNamespace(bot_data={"state": state}), args=[])

    @r.post("/login")
    def login(body: Login, response: Response):
        settings = get_settings()
        if not _password(settings):
            raise HTTPException(500, "Set DASHBOARD_PASSWORD (or CRON_SECRET) in the environment first.")
        if not hmac.compare_digest(body.password.strip(), _password(settings)):
            time.sleep(1)
            raise HTTPException(401, "Wrong password")
        exp = int(time.time()) + SESSION_SECONDS
        response.set_cookie(COOKIE, _sign(settings, exp), max_age=SESSION_SECONDS, httponly=True,
                            secure=settings.web_secure_cookie, samesite="strict", path="/")
        return {"ok": True}

    @r.post("/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @r.get("/feed")
    def feed(request: Request, limit: int = 40):
        auth(request)
        db, settings = get_db(), get_settings()
        notes = db.q("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),))
        s = metrics.compute(db, settings)
        return {
            "notes": [note_view(db, n) for n in notes],
            "stats": {"approved_week": s["week"]["approved"], "target": metrics.TARGET_PER_WEEK,
                      "captured_week": s["week"]["captured"], "drafted_week": s["week"]["drafted"],
                      "median_review_min": s["median_review_min"], "light_edit_share": s["light_edit_share"],
                      "total_cost": s["total_cost"]},
            "config": {"min_score": settings.min_shortlist_score, "trusted_only": settings.news_trusted_only,
                       "channel": settings.telegram_notes_chat_id,
                       "mirror": bool(settings.telegram_bot_token and settings.web_mirror_telegram)},
            "server_time": iso(),
        }

    @r.post("/notes")
    async def add_note(body: NewNote, request: Request):
        """Drop a note from the web: scored, then drafted with a Google News check if it's strong enough."""
        auth(request)
        db = get_db()
        nid, _ = db.upsert_note(source="web", chat_id=None, message_id=None, type_="text", text=body.text.strip())
        db.event("capture", note_id=nid, type="web")
        async with bot_context() as ctx:
            await tb.send(ctx, f"🌐 New note {nid} from the web dashboard:\n\n{body.text.strip()[:3500]}")
            await tb.process_new_note(ctx, nid)
        return note_view(db, db.note(nid))

    @r.post("/notes/{note_id}/draft")
    async def draft_note(note_id: int, request: Request):
        auth(request)
        db = get_db()
        if not db.note(note_id):
            raise HTTPException(404, "No such note")
        async with bot_context() as ctx:
            await tb.start_draft(ctx, [note_id])
        return note_view(db, db.note(note_id))

    @r.post("/drafts/{draft_id}/{action}")
    async def draft_action(draft_id: int, action: str, body: Action, request: Request):
        auth(request)
        db = get_db()
        d = db.draft(draft_id)
        if d is None:
            raise HTTPException(404, "No such draft")
        note_id = _j(d["note_ids"], [0])[0]
        codes = {"approve": "a", "regenerate": "rg", "angle": "ca", "nonews": "nn", "reject": "rj"}
        async with bot_context() as ctx:
            update = SimpleNamespace(callback_query=WebQuery())
            if action == "approve":
                missing = drafting.unresolved_placeholders(d["post_text"])
                if missing:
                    raise HTTPException(409, "Fill these placeholders first (use Revise): " + "; ".join(missing))
            if action in codes:
                if d["status"] != "delivered" and action != "reject":
                    raise HTTPException(409, f"This draft is no longer current ({d['status']}).")
                await tb.on_draft_action(update, ctx, codes[action], draft_id, None)
                if action == "reject" and body.reason in tb.REJECT_REASONS:
                    await tb.on_draft_action(update, ctx, "rr", draft_id, body.reason)
            elif action == "revise":
                if not (body.instruction or "").strip():
                    raise HTTPException(400, "Say what should change.")
                if d["status"] != "delivered":
                    raise HTTPException(409, f"This draft is no longer current ({d['status']}).")
                await tb.send(ctx, f"🌐 Revision requested from the web for draft #{draft_id}: {body.instruction.strip()}")
                await tb.redraft(ctx, draft_id, "revise", instruction=body.instruction.strip())
            elif action == "final":
                if not (body.text or "").strip():
                    raise HTTPException(400, "Paste what you posted.")
                if d["status"] != "approved":
                    raise HTTPException(409, "Approve the draft first.")
                ratio = drafting.edit_ratio(d["post_text"], body.text.strip())
                db.run("INSERT INTO finals(draft_id, final_text, edit_ratio, created_at) VALUES (?,?,?,?)",
                       (draft_id, body.text.strip(), ratio, iso()))
                meta = _j(d["meta_json"], {})
                db.run("INSERT INTO exemplars(kind, name, category, text) VALUES ('approved', ?, ?, ?) "
                       "ON CONFLICT(name) DO UPDATE SET text=excluded.text",
                       (f"approved_{draft_id}", meta.get("category"), body.text.strip()))
                db.event("final", draft_id=draft_id, edit_ratio=ratio)
            else:
                raise HTTPException(400, "Unknown action")
        return note_view(db, db.note(note_id)) if db.note(note_id) else {"ok": True}

    return r
