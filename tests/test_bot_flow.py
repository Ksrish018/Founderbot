"""Offline end-to-end run of the review gate with a fake Telegram bot: nothing is sent anywhere but REVIEW_CHAT."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import news, telegram_bot as tb
from tests.conftest import GOOD_POST
from tests.test_triage_drafting import add_notes, draft_json, score


class FakeBot:
    id = 999

    def __init__(self):
        self.sent: list[dict] = []
        self._mid = 100

    async def send_message(self, chat_id, text, reply_markup=None, link_preview_options=None):
        self._mid += 1
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup, "id": self._mid})
        return SimpleNamespace(message_id=self._mid)


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, cb, when, data=None):
        self.jobs.append((cb, when, data))


@pytest.fixture
def ctx(db, fake, settings, monkeypatch):
    async def no_news(db_, gemini, settings_, req, text):
        db_.run("UPDATE draft_requests SET news_status='none', news_reason='No relevant item.', news_ranking='[]', "
                "news_pos=-1 WHERE id=?", (req,))
    monkeypatch.setattr(news, "find_angle", no_news)
    state = tb.State(settings=settings, db=db, gemini=fake)
    app = SimpleNamespace(bot_data={"state": state})
    return SimpleNamespace(application=app, bot=FakeBot(), job_queue=FakeJobQueue(), args=[])


def cb_update(data: str, user_id: int = 42):
    async def noop(*a, **k):
        return None
    q = SimpleNamespace(data=data, answer=noop, edit_message_reply_markup=noop, edit_message_text=noop,
                        message=SimpleNamespace(text="card"))
    return SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=user_id), message=None)


async def test_shortlist_then_draft_then_approve(ctx, db, fake):
    ids = add_notes(db, 2)
    fake.queue("triage", {"scores": [score(ids[0], 9), score(ids[1], 4)]})
    await tb.run_triage(ctx, announce_empty=True)
    texts = [m["text"] for m in ctx.bot.sent]
    assert any(t.startswith("Shortlist: 1 note") for t in texts)
    assert all(m["chat_id"] == 42 for m in ctx.bot.sent)          # only Meera's private chat

    ctx.bot.sent.clear()
    fake.queue("draft", draft_json(GOOD_POST))
    await tb.on_callback(cb_update(f"d:{ids[0]}"), ctx)
    post, card = ctx.bot.sent[-2], ctx.bot.sent[-1]
    assert post["text"] == GOOD_POST and post["markup"] is None     # plain post text, exactly as pasted
    assert "Voice lint passed" in card["text"] and "No angle" in card["text"]
    buttons = [b.callback_data for row in card["markup"].inline_keyboard for b in row]
    assert all(len(b.encode()) < 64 for b in buttons) and len(buttons) == 6
    draft_id = db.one("SELECT id FROM drafts")["id"]

    ctx.bot.sent.clear()
    await tb.on_callback(cb_update(f"a:{draft_id}"), ctx)
    assert db.draft(draft_id)["status"] == "approved"
    assert db.note(ids[0])["status"] == "used"
    assert any(m["text"] == GOOD_POST for m in ctx.bot.sent)
    # double tap does nothing more
    n = len(ctx.bot.sent)
    await tb.on_callback(cb_update(f"a:{draft_id}"), ctx)
    assert "no longer current" in ctx.bot.sent[-1]["text"] and len(ctx.bot.sent) == n + 1


async def test_approve_refused_with_placeholders_then_revise(ctx, db, fake):
    nid = add_notes(db, 1)[0]
    text = GOOD_POST.replace("5.5-5.8", "[DATA NEEDED: current serum pH range, from Meera]")
    fake.queue("draft", draft_json(text))
    await tb.start_draft(ctx, [nid])
    did = db.one("SELECT id FROM drafts")["id"]
    assert "Placeholders to fill" in ctx.bot.sent[-1]["text"]

    await tb.on_callback(cb_update(f"a:{did}"), ctx)
    assert "Can't approve yet" in ctx.bot.sent[-1]["text"]
    assert db.draft(did)["status"] == "delivered"

    await tb.on_callback(cb_update(f"rv:{did}"), ctx)
    assert ctx.application.bot_data["state"].awaiting == ("revise", str(did))
    fake.queue("draft", draft_json(GOOD_POST))
    msg_update = SimpleNamespace(effective_user=SimpleNamespace(id=42), callback_query=None,
                                 message=SimpleNamespace(text="Our serum pH is 5.5-5.8", reply_to_message=None,
                                                         reply_text=_async_noop))
    await tb.on_text(msg_update, ctx)
    new = db.one("SELECT * FROM drafts ORDER BY id DESC LIMIT 1")
    assert new["parent_draft_id"] == did and new["revision_instruction"] == "Our serum pH is 5.5-5.8"
    assert db.draft(did)["status"] == "revised"


async def test_unauthorised_user_is_ignored(ctx, db):
    nid = add_notes(db, 1)[0]
    await tb.on_callback(cb_update(f"d:{nid}", user_id=7), ctx)
    assert ctx.bot.sent == [] and db.one("SELECT COUNT(*) c FROM drafts")["c"] == 0


async def test_gemini_outage_offers_retry_button(ctx, db, fake):
    from app.gemini_client import GeminiUnavailable

    async def down(**k):
        raise GeminiUnavailable("503")
    fake.generate_json = down
    nid = add_notes(db, 1)[0]
    await tb.start_draft(ctx, [nid])
    last = ctx.bot.sent[-1]
    assert "Drafting delayed" in last["text"]
    assert last["markup"].inline_keyboard[0][0].callback_data == f"rt:{nid}"
    assert db.note(nid)["status"] != "drafting"
    assert db.try_lock(f"note:{nid}")  # lock was released


async def test_db_lock_blocks_concurrent_draft(ctx, db, fake):
    nid = add_notes(db, 1)[0]
    assert db.try_lock(f"note:{nid}")          # another invocation is drafting this note
    await tb.start_draft(ctx, [nid])
    assert "Already drafting" in ctx.bot.sent[-1]["text"]
    assert db.one("SELECT COUNT(*) c FROM drafts")["c"] == 0


async def test_reject_with_reason(ctx, db, fake):
    nid = add_notes(db, 1)[0]
    fake.queue("draft", draft_json(GOOD_POST))
    await tb.start_draft(ctx, [nid])
    did = db.one("SELECT id FROM drafts")["id"]
    await tb.on_callback(cb_update(f"rj:{did}"), ctx)
    await tb.on_callback(cb_update(f"rr:{did}:o"), ctx)
    d = db.draft(did)
    assert d["status"] == "rejected" and d["reject_reason"] == "Off-voice"


def test_split_text_respects_limit():
    long = "\n\n".join([("word " * 300).strip()] * 5)
    chunks = tb.split_text(long, 4000)
    assert all(len(c) <= 4000 for c in chunks) and "".join(chunks).replace("\n", "") != ""


async def _async_noop(*a, **k):
    return None
