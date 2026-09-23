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
    return SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=user_id), message=None,
                           effective_message=q.message, effective_chat=SimpleNamespace(id=user_id),
                           channel_post=None, edited_channel_post=None)


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
    m = SimpleNamespace(text="Our serum pH is 5.5-5.8", reply_to_message=None, reply_text=_async_noop)
    msg_update = SimpleNamespace(effective_user=SimpleNamespace(id=42), callback_query=None, message=m,
                                 effective_message=m, effective_chat=SimpleNamespace(id=42),
                                 channel_post=None, edited_channel_post=None)
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


# ---- single-chat mode: everything happens in the notes channel ------------------------------
CHANNEL = -1003976391640


@pytest.fixture
def channel_ctx(db, fake, settings, monkeypatch):
    async def no_news(db_, gemini, settings_, req, text):
        db_.run("UPDATE draft_requests SET news_status='none', news_reason='No relevant item.', news_ranking='[]', "
                "news_pos=-1 WHERE id=?", (req,))
    monkeypatch.setattr(news, "find_angle", no_news)
    captured = []

    async def fake_capture(msg, db_, gemini, react=False):
        captured.append(msg.text)
    monkeypatch.setattr(tb.capture, "capture_message", fake_capture)
    settings.review_chat_id = CHANNEL
    assert settings.single_chat
    state = tb.State(settings=settings, db=db, gemini=fake)
    bot = FakeBot()
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"state": state}), bot=bot, args=[])
    ctx.captured = captured
    return ctx


def channel_post(ctx, text, reply_to=None):
    replies = []

    async def reply_text(t, reply_markup=None):
        replies.append(t)
        return await ctx.bot.send_message(CHANNEL, t, reply_markup)
    msg = SimpleNamespace(text=text, chat_id=CHANNEL, message_id=900 + len(ctx.bot.sent),
                          reply_to_message=SimpleNamespace(message_id=reply_to) if reply_to else None,
                          reply_text=reply_text)
    upd = SimpleNamespace(channel_post=msg, edited_channel_post=None, effective_message=msg,
                          effective_chat=SimpleNamespace(id=CHANNEL), effective_user=None, callback_query=None,
                          message=None)
    return upd, replies


async def test_channel_plain_post_is_a_note(channel_ctx):
    upd, _ = channel_post(channel_ctx, "Batch 14 pH dropped 0.4 after a preservative change.")
    await tb.on_channel_post(upd, channel_ctx)
    assert channel_ctx.captured == ["Batch 14 pH dropped 0.4 after a preservative change."]


async def test_channel_command_runs_and_replies_in_channel(channel_ctx, db, fake):
    ids = add_notes(db, 1)
    fake.queue("triage", {"scores": [score(ids[0], 8)]})
    upd, _ = channel_post(channel_ctx, "/triage@Meeras_Content_bot")
    await tb.on_channel_post(upd, channel_ctx)
    assert channel_ctx.captured == []                                   # commands are not notes
    assert all(m["chat_id"] == CHANNEL for m in channel_ctx.bot.sent)   # everything stays in the channel
    assert any(m["text"].startswith("Shortlist: 1 note") for m in channel_ctx.bot.sent)


async def test_channel_draft_buttons_and_reply_to_revise(channel_ctx, db, fake):
    nid = add_notes(db, 1)[0]
    fake.queue("draft", draft_json(GOOD_POST))
    await tb.on_callback(cb_update(f"d:{nid}"), channel_ctx)            # Meera taps Draft this (user 42)
    d = db.one("SELECT * FROM drafts")
    post_msg_id = json.loads(d["post_message_ids"])[0]

    fake.queue("draft", draft_json(GOOD_POST.replace("Batch fourteen", "Batch 14")))
    upd, replies = channel_post(channel_ctx, "Use the digit 14 in the first line", reply_to=post_msg_id)
    await tb.on_channel_post(upd, channel_ctx)
    new = db.one("SELECT * FROM drafts ORDER BY id DESC LIMIT 1")
    assert new["parent_draft_id"] == d["id"] and new["revision_instruction"] == "Use the digit 14 in the first line"
    assert channel_ctx.captured == []


async def test_channel_revise_button_then_reply_to_prompt(channel_ctx, db, fake):
    nid = add_notes(db, 1)[0]
    fake.queue("draft", draft_json(GOOD_POST))
    await tb.start_draft(channel_ctx, [nid])
    did = db.one("SELECT id FROM drafts")["id"]
    await tb.on_callback(cb_update(f"rv:{did}"), channel_ctx)
    prompt = channel_ctx.bot.sent[-1]
    assert "Reply to this message" in prompt["text"] and prompt["markup"] is None   # no ForceReply in channels
    fake.queue("draft", draft_json(GOOD_POST))
    upd, _ = channel_post(channel_ctx, "Make it shorter", reply_to=prompt["id"])
    await tb.on_channel_post(upd, channel_ctx)
    assert db.one("SELECT revision_instruction r FROM drafts ORDER BY id DESC LIMIT 1")["r"] == "Make it shorter"


async def test_channel_buttons_still_only_answer_meera(channel_ctx, db):
    nid = add_notes(db, 1)[0]
    await tb.on_callback(cb_update(f"d:{nid}", user_id=7), channel_ctx)
    assert channel_ctx.bot.sent == [] and db.one("SELECT COUNT(*) c FROM drafts")["c"] == 0


async def test_private_mode_channel_commands_are_just_notes(ctx, db):
    upd, _ = channel_post(ctx, "/triage")
    upd.effective_message.chat_id = CHANNEL
    captured = []

    async def fake_capture(msg, db_, gemini, react=False):
        captured.append(msg.text)
    tb.capture.capture_message, orig = fake_capture, tb.capture.capture_message
    try:
        await tb.on_channel_post(upd, ctx)
    finally:
        tb.capture.capture_message = orig
    assert captured == ["/triage"]
