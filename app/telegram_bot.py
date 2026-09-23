"""Telegram UI: capture handler, commands, shortlist and draft cards, the review gate, scheduled jobs.

Nothing here posts anywhere public. The only outputs are messages in Meera's private chat with the bot.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import time

from telegram import (BotCommand, ForceReply, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb,
                      LinkPreviewOptions, Update)
from telegram.constants import ChatMemberStatus
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from . import capture, drafting, facts, metrics, news, triage
from .config import Settings, parse_schedule
from .db import DB, iso
from .gemini_client import Gemini, GeminiAuthError, GeminiUnavailable
from .schemas import OPENING_TYPES

log = logging.getLogger(__name__)
TG_LIMIT = 4000
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
REJECT_REASONS = {"o": "Off-voice", "w": "Wrong facts", "k": "Weak idea", "n": "Not now"}
OPENING_NAMES = {"A": "Reader's shelf", "B": "Dated scene", "C": "Data drop", "D": "Stated intention",
                 "E": "Reluctant admission", "F": "Customer question", "G": "Conceded truism",
                 "H": "Phrase under inspection", "I": "Shortest version"}
DRAFT_RETRY_DELAY = 300
DRAFT_MAX_RETRIES = 3

HELP = (
    "I turn your channel notes into LinkedIn drafts. I never post anything; you paste approved posts yourself.\n\n"
    "/triage - score new notes and send the shortlist\n"
    "/notes [n] - latest notes with their ids\n"
    "/draft <note_id> - draft a specific note\n"
    "/final <text> - record what you actually posted (or reply 'final' + your text to a draft)\n"
    "/facts - resolve fact-bank conflicts\n"
    "/stats - progress against 3 posts a week\n"
    "/pause, /resume - scheduled shortlists\n"
    "/health - check Telegram, Gemini, news and the database\n"
    "/start - your user id and setup status"
)


@dataclass
class State:
    settings: Settings
    db: DB
    gemini: Gemini
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    awaiting: tuple[str, str] | None = None   # ("revise", draft_id) | ("fact", key) | ("final", draft_id)
    gemini_ok: bool = False

    def lock(self, key: str) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())


def st(context: ContextTypes.DEFAULT_TYPE) -> State:
    return context.application.bot_data["state"]


def is_meera(update: Update, s: State) -> bool:
    u = update.effective_user
    return bool(u and s.settings.meera_user_id and u.id == s.settings.meera_user_id)


def meera_only(fn):
    """Every command, message and callback outside the notes channel is ignored unless it is from Meera."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        s = st(context)
        if not is_meera(update, s):
            if update.callback_query:
                await update.callback_query.answer()
            log.info("Ignored update from unauthorised user %s", update.effective_user.id if update.effective_user else None)
            return
        return await fn(update, context)
    wrapper.__name__ = fn.__name__
    return wrapper


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split on paragraph boundaries so each piece fits in one Telegram message."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        cand = f"{cur}\n\n{para}" if cur else para
        if len(cand) <= limit:
            cur = cand
            continue
        if cur:
            chunks.append(cur)
        while len(para) > limit:
            cut = para.rfind(". ", 0, limit) + 1 or limit
            chunks.append(para[:cut].strip())
            para = para[cut:].strip()
        cur = para
    if cur:
        chunks.append(cur)
    return chunks


async def send(context, text: str, markup=None) -> int:
    s = st(context)
    msg = await context.bot.send_message(s.settings.review_chat, text[:TG_LIMIT], reply_markup=markup,
                                         link_preview_options=NO_PREVIEW)
    return msg.message_id


# ---- capture ----------------------------------------------------------------
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = st(context)
    msg = update.effective_message
    if msg is None or msg.chat_id != s.settings.telegram_notes_chat_id:
        return
    await capture.capture_message(msg, s.db, s.gemini, react=s.settings.capture_reaction)


# ---- commands ---------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = st(context)
    uid = update.effective_user.id
    if not s.settings.meera_user_id:
        await update.message.reply_text(
            f"Your Telegram user ID is {uid}.\nPut it in .env as MEERA_USER_ID={uid} and restart the bot.")
        return
    if not is_meera(update, s):
        return
    n = s.db.one("SELECT COUNT(*) c FROM notes")["c"]
    e = s.db.one("SELECT COUNT(*) c FROM exemplars")["c"]
    admin = await bot_is_channel_admin(context, s)
    days, at = parse_schedule(s.settings.triage_schedule)
    paused = s.db.get_kv("triage_paused") == "1"
    await update.message.reply_text(
        f"Hi Meera. Your user ID is {uid}.\n\n"
        f"Setup status\n"
        f"Notes channel {s.settings.telegram_notes_chat_id}: {'bot is admin ✅' if admin else 'bot is NOT admin ❌'}\n"
        f"Gemini: {'ready (' + s.gemini.model + ') ✅' if s.gemini_ok else 'not ready ❌ (see /health)'}\n"
        f"Notes in DB: {n} · voice exemplars: {e}\n"
        f"Shortlists: {s.settings.triage_schedule} {s.settings.timezone}{' (paused)' if paused else ''}\n\n"
        + HELP)


async def bot_is_channel_admin(context, s: State) -> bool:
    try:
        m = await context.bot.get_chat_member(s.settings.telegram_notes_chat_id, context.bot.id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False


@meera_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


@meera_only
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = st(context)
    lines = []
    try:
        me = await context.bot.get_me()
        lines.append(f"✅ Telegram: @{me.username}")
    except Exception as e:
        lines.append(f"❌ Telegram: {e.__class__.__name__}")
    admin = await bot_is_channel_admin(context, s)
    lines.append(("✅" if admin else "❌") + f" Notes channel admin ({s.settings.telegram_notes_chat_id})")
    try:
        model = await s.gemini.health_check()
        s.gemini_ok = True
        lines.append(f"✅ Gemini: {model}")
    except (GeminiAuthError, GeminiUnavailable) as e:
        s.gemini_ok = False
        lines.append(f"❌ Gemini: {e}")
    try:
        items = await news.fetch(["skincare India"], 30)
        lines.append(f"✅ Google News RSS: {len(items)} recent items")
    except Exception as e:
        lines.append(f"❌ Google News RSS: {e.__class__.__name__}")
    try:
        n = s.db.one("SELECT COUNT(*) c FROM notes")["c"]
        lines.append(f"✅ Database: {n} notes")
    except Exception as e:
        lines.append(f"❌ Database: {e.__class__.__name__}")
    await update.message.reply_text("\n".join(lines))


@meera_only
async def cmd_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = st(context)
    n = int(context.args[0]) if context.args and context.args[0].isdigit() else 10
    rows = s.db.latest_notes(min(n, 30))
    if not rows:
        await update.message.reply_text("No notes yet.")
        return
    lines = []
    for r in rows:
        body = s.db.note_body(r).replace("\n", " ")
        lines.append(f"#{r['id']} [{r['status']}] ({r['type']}, {r['source']}) {body[:70]}{'…' if len(body) > 70 else ''}")
    for chunk in split_text("\n".join(lines)):
        await update.message.reply_text(chunk)


@meera_only
async def cmd_triage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Scoring new notes…")
    await run_triage(context, announce_empty=True)


@meera_only
async def cmd_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = st(context)
    if not context.args or not context.args[0].isdigit() or not s.db.note(int(context.args[0])):
        await update.message.reply_text("Usage: /draft <note_id>  (see /notes for ids)")
        return
    await start_draft(context, [int(context.args[0])])


@meera_only
async def cmd_facts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    confs = facts.conflicts()
    lines, buttons = ["Fact-bank conflicts. Drafts won't use an unresolved one; they insert [DATA NEEDED] instead.\n"], []
    for c in confs:
        mark = "✅ resolved" if c["status"] == "canonical" else "⚠️ unresolved"
        lines.append(f"{c['key']} · {c['title']} · {mark}\n{c['text']}")
        if c["status"] == "canonical":
            lines.append(f"Your answer: {c['resolution']}")
        lines.append("")
        buttons.append(Btn(f"{'Update' if c['status'] == 'canonical' else 'Resolve'} {c['key']}",
                           callback_data=f"f:{c['key']}"))
    await update.message.reply_text("\n".join(lines)[:TG_LIMIT], reply_markup=Kb([buttons[i:i + 2]
                                                                                   for i in range(0, len(buttons), 2)]))


@meera_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = st(context)
    await update.message.reply_text(metrics.format_stats(metrics.compute(s.db, s.settings)))


@meera_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st(context).db.set_kv("triage_paused", "1")
    await update.message.reply_text("Scheduled shortlists paused. /triage still works. /resume to restart.")


@meera_only
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = st(context)
    s.db.set_kv("triage_paused", "0")
    await update.message.reply_text(f"Scheduled shortlists resumed: {s.settings.triage_schedule} {s.settings.timezone}.")


@meera_only
async def cmd_final(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = st(context)
    text = update.message.text.partition(" ")[2].strip()
    row = s.db.one("SELECT d.* FROM drafts d LEFT JOIN finals f ON f.draft_id = d.id WHERE d.status='approved' "
                   "AND f.id IS NULL ORDER BY d.decided_at DESC LIMIT 1") or \
        s.db.one("SELECT * FROM drafts WHERE status='approved' ORDER BY decided_at DESC LIMIT 1")
    if not row:
        await update.message.reply_text("There's no approved draft to attach this to yet.")
        return
    if not text:
        s.awaiting = ("final", str(row["id"]))
        await update.message.reply_text(f"Paste exactly what you posted for draft #{row['id']}.",
                                        reply_markup=ForceReply(input_field_placeholder="What you posted"))
        return
    await store_final(update, s, row, text)


async def store_final(update: Update, s: State, draft, text: str) -> None:
    ratio = drafting.edit_ratio(draft["post_text"], text)
    s.db.run("INSERT INTO finals(draft_id, final_text, edit_ratio, created_at) VALUES (?,?,?,?)",
             (draft["id"], text, ratio, iso()))
    meta = json.loads(draft["meta_json"] or "{}")
    s.db.run("INSERT INTO exemplars(kind, name, category, text) VALUES ('approved', ?, ?, ?) "
             "ON CONFLICT(name) DO UPDATE SET text=excluded.text",
             (f"approved_{draft['id']}", meta.get("category"), text))
    s.db.event("final", draft_id=draft["id"], edit_ratio=ratio)
    await update.message.reply_text(
        f"Saved what you posted for draft #{draft['id']}. Edit ratio {ratio:.2f} "
        f"({'light edit' if ratio <= 0.15 else 'substantial edit'}). It's now a voice exemplar for future drafts.")


# ---- triage & shortlist -----------------------------------------------------
async def run_triage(context, *, announce_empty: bool) -> None:
    s = st(context)
    try:
        n = await triage.score_pending(s.db, s.gemini, s.settings)
    except GeminiAuthError as e:
        await send(context, f"Triage stopped: {e}")
        return
    except GeminiUnavailable as e:
        await send(context, f"Triage delayed: Gemini is unavailable ({e}). I'll try again at the next run, "
                            "or send /triage later.")
        return
    log.info("Scored %s notes", n)
    await send_shortlist(context, offset=0, announce_empty=announce_empty, newly_scored=n)
    if s.settings.auto_draft_top_n > 0:  # Meera's call only; default 0
        for c in triage.build_shortlist(s.db, s.settings)[: s.settings.auto_draft_top_n]:
            await start_draft(context, c.note_ids)


async def send_shortlist(context, *, offset: int, announce_empty: bool = True, newly_scored: int = 0) -> None:
    s = st(context)
    cands = triage.build_shortlist(s.db, s.settings)
    size = s.settings.shortlist_size
    page = cands[offset: offset + size]
    if not page:
        if announce_empty or newly_scored:
            total = s.db.one("SELECT COUNT(*) c FROM notes")["c"]
            await send(context, f"Nothing scores {s.settings.min_shortlist_score}/10 or above right now "
                                f"({newly_scored} notes scored this run, {total} notes in total). "
                                "I won't pad the list. Keep dropping notes into the channel.")
        return
    if offset == 0:
        await send(context, f"Shortlist: {len(cands)} note(s) worth developing. Showing {len(page)}.\n"
                            "Tap Draft this on one. Skip or Park hides a note for 30 days.")
    for c in page:
        await send(context, triage.candidate_text(c), Kb([[
            Btn("✍️ Draft this", callback_data=f"d:{c.note_id}"),
            Btn("⏭ Skip", callback_data=f"s:{c.note_id}"),
            Btn("🅿️ Park", callback_data=f"p:{c.note_id}")]]))
        for nid in c.note_ids:
            s.db.event("shortlisted", note_id=nid)
    triage.mark_shown(s.db, page)
    if len(cands) > offset + size:
        await send(context, f"{len(cands) - offset - size} more candidate(s).",
                   Kb([[Btn("📋 Show more", callback_data=f"sm:{offset + size}")]]))


# ---- drafting flow ------------------------------------------------------------
def cluster_for(s: State, note_id: int) -> list[int]:
    sc = s.db.latest_score(note_id)
    ids = [note_id]
    if sc:
        for c in json.loads(sc["cluster_with"] or "[]"):
            row = s.db.note(c)
            if row and row["status"] in ("scored", "shortlisted", "new"):
                ids.append(c)
    return ids


async def start_draft(context, note_ids: list[int], attempt: int = 0) -> None:
    s = st(context)
    lock = s.lock(f"note:{note_ids[0]}")
    if lock.locked():
        await send(context, f"Already drafting note {note_ids[0]}. One moment.")
        return
    async with lock:
        row = s.db.note(note_ids[0])
        if attempt == 0 and row["status"] == "drafting":
            await send(context, f"Note {note_ids[0]} is already being drafted.")
            return
        prev_status = {nid: s.db.note(nid)["status"] for nid in note_ids}
        s.db.set_note_status(note_ids, "drafting")
        if attempt == 0:
            merged = f" (merged with {', '.join(map(str, note_ids[1:]))})" if len(note_ids) > 1 else ""
            await send(context, f"Drafting note {note_ids[0]}{merged}. Checking Google News for a current angle…")
        try:
            req = drafting.start_request(s.db, note_ids)
            await news.find_angle(s.db, s.gemini, s.settings, req, drafting.notes_text(s.db, note_ids))
            draft_id = await drafting.generate(s.db, s.gemini, s.settings, req)
        except GeminiAuthError as e:
            _restore(s, prev_status)
            await send(context, f"Drafting stopped: {e}")
            return
        except GeminiUnavailable as e:
            _restore(s, prev_status)
            if attempt < DRAFT_MAX_RETRIES:
                context.job_queue.run_once(_retry_draft_job, DRAFT_RETRY_DELAY,
                                           data={"note_ids": note_ids, "attempt": attempt + 1})
                await send(context, f"Drafting delayed, will retry in {DRAFT_RETRY_DELAY // 60} minutes. ({e})")
            else:
                await send(context, f"Drafting failed after {DRAFT_MAX_RETRIES} retries ({e}). "
                                    f"Try /draft {note_ids[0]} later.")
            return
    await deliver(context, draft_id)


def _restore(s: State, prev: dict[int, str]) -> None:
    for nid, status in prev.items():
        s.db.set_note_status([nid], "shortlisted" if status == "drafting" else status)


async def _retry_draft_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    await start_draft(context, d["note_ids"], attempt=d["attempt"])


async def deliver(context, draft_id: int) -> None:
    s = st(context)
    d = s.db.draft(draft_id)
    meta = json.loads(d["meta_json"] or "{}")
    lint = json.loads(d["lint_json"] or "{}")
    ids = []
    for chunk in split_text(d["post_text"]):
        ids.append(await send(context, chunk))  # plain text, exactly as it would be pasted
    card_id = await send(context, card_text(s, d, meta, lint), draft_keyboard(draft_id))
    s.db.run("UPDATE drafts SET post_message_ids=?, card_message_id=?, delivered_at=? WHERE id=?",
             (json.dumps(ids), card_id, iso(), draft_id))


def card_text(s: State, d, meta: dict, lint: dict) -> str:
    req = s.db.one("SELECT * FROM draft_requests WHERE id=?", (d["request_id"],))
    item = s.db.one("SELECT * FROM news_items WHERE id=?", (d["news_item_id"],)) if d["news_item_id"] else None
    op = meta.get("opening_type", "?")
    qa = meta.get("self_qa", {}) or {}
    qa_total = qa.get("total") or sum(x.get("score", 0) for x in qa.get("scores", []))
    lines = [f"Draft #{d['id']} · v{d['version']} · note(s) {', '.join(map(str, json.loads(d['note_ids'])))}",
             f"{meta.get('category', '?')} · opening {op} ({OPENING_NAMES.get(op, '')}) · {lint.get('word_count')} words"
             + (" · short mode" if meta.get("short") else ""),
             f"Self-QA: {qa_total}/20"]
    if lint.get("hard"):
        lines.append(f"⚠️ Lint still failing after {d['lint_retries']} retries:\n- " + "\n- ".join(lint["hard"]))
    else:
        lines.append("✅ Voice lint passed" + (f" (after {d['lint_retries']} retr{'y' if d['lint_retries'] == 1 else 'ies'})"
                                               if d["lint_retries"] else ""))
    if lint.get("soft"):
        lines.append("Warnings:\n- " + "\n- ".join(lint["soft"]))
    if item:
        lines.append(f"🗞 News angle: {item['title']} ({item['source']}, {(item['published_at'] or '')[:10]})\n{item['link']}")
    elif req and req["news_status"] == "rss_failed":
        lines.append(f"🗞 No angle: {req['news_reason']}")
    else:
        lines.append(f"🗞 No angle{': ' + req['news_reason'] if req and req['news_reason'] else ''}")
    ph = meta.get("placeholders_in_text") or []
    if ph:
        lines.append("Placeholders to fill before approving:\n- " + "\n- ".join(ph))
    if meta.get("verify_flags"):
        lines.append("Verify before posting:\n- " + "\n- ".join(meta["verify_flags"]))
    if meta.get("note_to_meera"):
        lines.append(f"Note: {meta['note_to_meera']}")
    if d["revision_instruction"]:
        lines.append(f"Revised for: {d['revision_instruction'][:200]}")
    return "\n\n".join(lines)


def draft_keyboard(draft_id: int) -> Kb:
    return Kb([
        [Btn("✅ Approve", callback_data=f"a:{draft_id}"), Btn("✏️ Revise", callback_data=f"rv:{draft_id}"),
         Btn("🔄 Regenerate", callback_data=f"rg:{draft_id}")],
        [Btn("🗞 Change angle", callback_data=f"ca:{draft_id}"), Btn("🚫 No news", callback_data=f"nn:{draft_id}"),
         Btn("❌ Reject", callback_data=f"rj:{draft_id}")],
    ])


async def redraft(context, draft_id: int, mode: str, *, instruction: str | None = None,
                  avoid: list[str] | None = None) -> None:
    s = st(context)
    d = s.db.draft(draft_id)
    lock = s.lock(f"req:{d['request_id']}")
    if lock.locked():
        await send(context, "Already working on this draft. One moment.")
        return
    async with lock:
        if s.db.draft(draft_id)["status"] != "delivered":
            await send(context, "That draft is no longer the current one.")
            return
        try:
            new_id = await drafting.generate(s.db, s.gemini, s.settings, d["request_id"], parent_id=draft_id,
                                             instruction=instruction, avoid_opening=avoid, mode=mode)
        except (GeminiAuthError, GeminiUnavailable) as e:
            await send(context, f"Couldn't redraft right now ({e}). The current draft is unchanged; try again shortly.")
            return
    await deliver(context, new_id)


# ---- callbacks ----------------------------------------------------------------
@meera_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = st(context)
    q = update.callback_query
    parts = (q.data or "").split(":")
    kind, arg = parts[0], parts[1] if len(parts) > 1 else ""
    await q.answer()

    if kind == "d":
        await q.edit_message_reply_markup(None)
        await start_draft(context, cluster_for(s, int(arg)))
    elif kind in ("s", "p"):
        nid = int(arg)
        ids = cluster_for(s, nid)
        s.db.set_note_status(ids, "skipped" if kind == "s" else "parked", park_days=30)
        s.db.event("skip" if kind == "s" else "park", note_ids=ids)
        await q.edit_message_text(f"{q.message.text}\n\n{'⏭ Skipped' if kind == 's' else '🅿️ Parked'} for 30 days.")
    elif kind == "sm":
        await q.edit_message_reply_markup(None)
        await send_shortlist(context, offset=int(arg))
    elif kind == "f":
        s.awaiting = ("fact", arg)
        await send(context, f"Reply with the canonical version for {arg}. I'll store it with today's date and "
                            "drafts will be allowed to use it.", ForceReply(input_field_placeholder=f"{arg} answer"))
    elif kind in ("a", "rv", "rg", "ca", "nn", "rj", "rr"):
        await on_draft_action(update, context, kind, int(arg), parts[2] if len(parts) > 2 else None)


async def on_draft_action(update: Update, context, kind: str, draft_id: int, extra: str | None) -> None:
    s = st(context)
    q = update.callback_query
    d = s.db.draft(draft_id)
    if d is None:
        return
    if kind == "rr":
        reason = REJECT_REASONS.get(extra or "", "none given")
        s.db.run("UPDATE drafts SET reject_reason=? WHERE id=?", (reason, draft_id))
        if extra == "n":
            s.db.set_note_status(json.loads(d["note_ids"]), "parked", park_days=30)
        await q.edit_message_text(f"Rejected draft #{draft_id}: {reason}. Noted.")
        return
    if d["status"] != "delivered":
        await send(context, f"Draft #{draft_id} is no longer current ({d['status']}).")
        return

    if kind == "a":
        missing = drafting.unresolved_placeholders(d["post_text"])
        if missing:
            await send(context, "Can't approve yet. These placeholders are still in the text:\n- " + "\n- ".join(missing)
                       + "\n\nTap Revise and give me the facts, or tell me to cut those sentences.")
            return
        cur = s.db.conn.execute("UPDATE drafts SET status='approved', decided_at=? WHERE id=? AND status='delivered'",
                                (iso(), draft_id))
        s.db.conn.commit()
        if cur.rowcount == 0:
            return  # double tap
        s.db.set_note_status(json.loads(d["note_ids"]), "used")
        s.db.event("approved", draft_id=draft_id)
        await q.edit_message_reply_markup(None)
        meta = json.loads(d["meta_json"] or "{}")
        await send(context, "✅ Approved. The copy-ready post is below. Paste it into LinkedIn yourself.")
        for chunk in split_text(d["post_text"]):
            await send(context, chunk)
        item = s.db.one("SELECT * FROM news_items WHERE id=?", (d["news_item_id"],)) if d["news_item_id"] else None
        reminders = list(meta.get("verify_flags") or [])
        if item:
            reminders.append(f"Open the source before posting: {item['link']}")
        tail = ("Before posting:\n- " + "\n- ".join(reminders) + "\n\n") if reminders else ""
        await send(context, tail + "After you post, send /final followed by what you actually posted "
                                   "(or reply 'final' + your text to the post above), so I can learn from your edits.")
    elif kind == "rv":
        s.awaiting = ("revise", str(draft_id))
        await send(context, f"What should change in draft #{draft_id}?",
                   ForceReply(input_field_placeholder="e.g. shorter, drop the news hook, open with the batch record"))
    elif kind == "rg":
        meta = json.loads(d["meta_json"] or "{}")
        avoid = [meta.get("opening_type")] if meta.get("opening_type") in OPENING_TYPES else []
        await send(context, "Regenerating with a different opening…")
        await redraft(context, draft_id, "regenerate", avoid=avoid)
    elif kind == "ca":
        if not news.advance(s.db, d["request_id"]):
            await send(context, "No other relevant news items for this note. Tap 🚫 No news to draft without a hook.")
            return
        await send(context, "Switching to the next news angle…")
        await redraft(context, draft_id, "angle")
    elif kind == "nn":
        if d["news_item_id"] is None:
            await send(context, "This draft already has no news hook.")
            return
        news.drop(s.db, d["request_id"])
        await send(context, "Redrafting without a news hook…")
        await redraft(context, draft_id, "angle")
    elif kind == "rj":
        s.db.conn.execute("UPDATE drafts SET status='rejected', decided_at=? WHERE id=? AND status='delivered'",
                          (iso(), draft_id))
        s.db.conn.commit()
        s.db.set_note_status(json.loads(d["note_ids"]), "skipped", park_days=30)
        s.db.event("rejected", draft_id=draft_id)
        await q.edit_message_reply_markup(None)
        await send(context, f"Rejected draft #{draft_id}. Why? (optional)", Kb([[
            Btn(label, callback_data=f"rr:{draft_id}:{code}") for code, label in REJECT_REASONS.items()]]))


# ---- free text from Meera ---------------------------------------------------------
@meera_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = st(context)
    msg = update.message
    text = (msg.text or "").strip()

    if msg.reply_to_message and text.lower().startswith("final"):
        d = s.db.draft_by_message(msg.reply_to_message.message_id)
        body = text[5:].lstrip(" :\n")
        if d and body:
            await store_final(update, s, d, body)
            return

    awaiting, s.awaiting = s.awaiting, None
    if awaiting is None:
        await msg.reply_text("I only act on buttons and commands. /help lists them.")
        return
    kind, arg = awaiting
    if kind == "revise":
        await msg.reply_text("Revising…")
        await redraft(context, int(arg), "revise", instruction=text)
    elif kind == "fact":
        try:
            c = facts.resolve(s.db, arg, text)
            await msg.reply_text(f"Saved {arg} ({c['title']}) as canonical: {c['resolution']}")
        except KeyError:
            await msg.reply_text(f"Unknown fact key {arg}.")
    elif kind == "final":
        await store_final(update, s, s.db.draft(int(arg)), text)


# ---- scheduled jobs -----------------------------------------------------------------
async def job_triage(context: ContextTypes.DEFAULT_TYPE) -> None:
    s = st(context)
    if s.db.get_kv("triage_paused") == "1" or not s.settings.review_chat:
        return
    await run_triage(context, announce_empty=True)


async def job_weekly(context: ContextTypes.DEFAULT_TYPE) -> None:
    s = st(context)
    if s.settings.review_chat:
        await send(context, "Weekly summary\n\n" + metrics.format_stats(metrics.compute(s.db, s.settings)))


async def job_transcripts(context: ContextTypes.DEFAULT_TYPE) -> None:
    s = st(context)
    n = await capture.retry_pending_transcripts(context.bot, s.db, s.gemini)
    if n:
        log.info("Transcribed %s deferred voice notes", n)


# ---- wiring ---------------------------------------------------------------------
def build_application(settings: Settings, db: DB, gemini: Gemini) -> Application:
    async def post_init(app: Application) -> None:
        state: State = app.bot_data["state"]
        me = await app.bot.get_me()
        log.info("Telegram ready: @%s", me.username)
        try:
            await gemini.health_check()
            state.gemini_ok = True
        except GeminiAuthError:
            raise
        except GeminiUnavailable as e:
            log.error("Gemini not ready: %s (the bot will run; drafting retries later)", e)
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in [
            ("triage", "Score notes and send the shortlist"), ("notes", "Latest notes"),
            ("draft", "Draft a note by id"), ("final", "Record what you posted"), ("facts", "Resolve fact conflicts"),
            ("stats", "Progress this week"), ("pause", "Pause scheduled shortlists"),
            ("resume", "Resume scheduled shortlists"), ("health", "System check"), ("help", "Help")]])
        if not settings.meera_user_id:
            log.warning("MEERA_USER_ID is not set. Ask Meera to send /start to the bot, then add her id to .env.")

    app = (Application.builder().token(settings.telegram_bot_token).concurrent_updates(True)
           .post_init(post_init).build())
    app.bot_data["state"] = State(settings=settings, db=db, gemini=gemini)

    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS & filters.Chat(settings.telegram_notes_chat_id),
                                   on_channel_post))
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    for name, fn in [("help", cmd_help), ("health", cmd_health), ("notes", cmd_notes), ("triage", cmd_triage),
                     ("draft", cmd_draft), ("facts", cmd_facts), ("stats", cmd_stats), ("pause", cmd_pause),
                     ("resume", cmd_resume), ("final", cmd_final)]:
        app.add_handler(CommandHandler(name, fn, filters=private))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & private & ~filters.COMMAND, on_text))

    days, at = parse_schedule(settings.triage_schedule)
    tz = settings.tz
    app.job_queue.run_daily(job_triage, time(at.hour, at.minute, tzinfo=tz), days=days, name="triage")
    app.job_queue.run_daily(job_weekly, time(18, 0, tzinfo=tz), days=(0,), name="weekly")  # 0 = Sunday
    app.job_queue.run_repeating(job_transcripts, interval=900, first=120, name="transcripts")
    return app
