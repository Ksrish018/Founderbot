"""channel_post -> note. Silent in the channel: it is Meera's thinking space."""
from __future__ import annotations

import logging

from telegram import Message, ReactionTypeEmoji

from .db import DB, iso
from .gemini_client import Gemini, GeminiUnavailable

log = logging.getLogger(__name__)


def classify(msg: Message) -> tuple[str, str | None, str | None, bool]:
    """Returns (type, text, file_id, has_attachment)."""
    if msg.voice or msg.audio:
        f = msg.voice or msg.audio
        return "voice", msg.caption, f.file_id, False
    if msg.photo or msg.document or msg.video:
        return "caption", msg.caption or "", None, True
    text = msg.text or msg.caption or ""
    urls = [e for e in (msg.entities or []) if e.type in ("url", "text_link")]
    if urls or msg.forward_origin is not None:
        links = [e.url for e in urls if e.type == "text_link" and e.url]
        if links:
            text = text + "\n" + "\n".join(links)
        return "link", text, None, False
    return "text", text, None, False


async def capture_message(msg: Message, db: DB, gemini: Gemini, *, react: bool = False) -> int:
    type_, text, file_id, has_att = classify(msg)
    transcript = None
    if type_ == "voice" and file_id:
        transcript = await transcribe_file(msg.get_bot(), gemini, file_id,
                                           (msg.voice or msg.audio).mime_type or "audio/ogg")
    nid, created = db.upsert_note(source="telegram", chat_id=msg.chat_id, message_id=msg.message_id, type_=type_,
                                  text=text, transcript=transcript, file_id=file_id, has_attachment=has_att,
                                  created_at=iso(msg.date))
    if not created:
        # An edit puts the note back into the queue so it gets re-scored.
        db.run("UPDATE notes SET status='new' WHERE id=? AND status IN ('scored','shortlisted','unpublishable')", (nid,))
    db.event("capture" if created else "capture_edit", note_id=nid, type=type_)
    log.info("Captured note %s (%s, %s)", nid, type_, "new" if created else "edited")
    if react and created:
        try:
            await msg.set_reaction(ReactionTypeEmoji("👍"))
        except Exception:  # reactions are optional
            pass
    return nid


async def transcribe_file(bot, gemini: Gemini, file_id: str, mime: str) -> str | None:
    try:
        f = await bot.get_file(file_id)
        audio = bytes(await f.download_as_bytearray())
        return await gemini.transcribe(audio, mime)
    except GeminiUnavailable as e:
        log.warning("Transcription deferred: %s", e)
        return None
    except Exception as e:
        log.warning("Voice download/transcription failed: %s", e.__class__.__name__)
        return None


async def retry_pending_transcripts(bot, db: DB, gemini: Gemini) -> int:
    done = 0
    for row in db.q("SELECT * FROM notes WHERE type='voice' AND transcript IS NULL AND file_id IS NOT NULL"):
        t = await transcribe_file(bot, gemini, row["file_id"], "audio/ogg")
        if t:
            db.run("UPDATE notes SET transcript=? WHERE id=?", (t, row["id"]))
            done += 1
    return done
