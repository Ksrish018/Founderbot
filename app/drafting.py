"""Prompt assembly, Gemini drafting call, and the retry-on-lint loop."""
from __future__ import annotations

import difflib
import json
import logging
import re

from . import exemplars, facts, news
from .config import Settings
from .db import DB, iso
from .gemini_client import Gemini
from .linter import extract_verify, find_placeholders, lint
from .schemas import DraftOut, system_for

log = logging.getLogger(__name__)
MAX_LINT_RETRIES = 2
SHORT_RE = re.compile(r"\b(short|shorter|shorten|brief|tighter|cut it down)\b", re.I)


def start_request(db: DB, note_ids: list[int]) -> int:
    return db.run("INSERT INTO draft_requests(note_ids, created_at) VALUES (?,?)", (json.dumps(note_ids), iso()))


def notes_text(db: DB, note_ids: list[int]) -> str:
    parts = []
    for nid in note_ids:
        row = db.note(nid)
        if row:
            parts.append(f"NOTE {nid} (captured {row['created_at'][:10]}):\n{db.note_body(row)}")
    return "\n\n".join(parts)


def recent_openings(db: DB) -> list[str]:
    out = []
    for r in db.recent_approved(5):
        meta = json.loads(r["meta_json"] or "{}")
        if meta.get("opening_type"):
            out.append(meta["opening_type"])
    return out


def _recent_block(db: DB) -> str:
    rows = db.recent_approved(5)
    if not rows:
        return "RECENT OPENINGS: none yet."
    lines = ["RECENT OPENINGS (last 5 approved posts; do not reuse these opening types or templates):"]
    for r in rows:
        meta = json.loads(r["meta_json"] or "{}")
        first = r["post_text"].strip().split("\n")[0][:160]
        lines.append(f"- type {meta.get('opening_type', '?')}: {first}")
    return "\n".join(lines)


def build_prompt(db: DB, request_id: int, *, category_hint: str | None, avoid_opening: list[str],
                 parent: dict | None, instruction: str | None, short: bool) -> str:
    req = db.one("SELECT * FROM draft_requests WHERE id=?", (request_id,))
    note_ids = json.loads(req["note_ids"])
    ex = exemplars.pick(db, category_hint)
    sections = [
        "MEERA'S NOTE(S), VERBATIM:\n" + notes_text(db, note_ids),
        facts.canonical_block(facts.load(db)),
        exemplars.block(ex),
        news.item_block(news.current_item(db, request_id)),
        _recent_block(db),
    ]
    if avoid_opening:
        sections.append(f"Do NOT use opening type(s): {', '.join(sorted(set(avoid_opening)))}. Pick a different one.")
    sections.append("LENGTH: short mode, 150-520 words." if short else "LENGTH: 380-520 words.")
    if parent is not None:
        sections.append(f"CURRENT DRAFT:\n{parent['post_text']}")
        prev_lint = json.loads(parent.get("lint_json") or "{}")
        if prev_lint.get("hard"):
            sections.append("CURRENT DRAFT LINT FAILURES:\n- " + "\n- ".join(prev_lint["hard"]))
        sections.append(f"MEERA'S INSTRUCTION:\n{instruction}")
    return "\n\n".join(sections)


async def generate(db: DB, gemini: Gemini, settings: Settings, request_id: int, *, parent_id: int | None = None,
                   instruction: str | None = None, avoid_opening: list[str] | None = None,
                   mode: str = "draft") -> int:
    """mode: draft | regenerate | revise | angle. Returns the new draft id."""
    req = db.one("SELECT * FROM draft_requests WHERE id=?", (request_id,))
    note_ids = json.loads(req["note_ids"])
    parent = dict(db.draft(parent_id)) if parent_id else None
    parent_meta = json.loads(parent["meta_json"] or "{}") if parent else {}
    score = db.latest_score(note_ids[0])
    category_hint = parent_meta.get("category") or (score["category"] if score else None)
    short = bool(parent_meta.get("short")) or bool(instruction and SHORT_RE.search(instruction))

    wrapper = "revise" if mode == "revise" else "draft"
    system, prompt_version = system_for(wrapper)
    base_prompt = build_prompt(db, request_id, category_hint=category_hint, avoid_opening=avoid_opening or [],
                               parent=parent if mode == "revise" else None, instruction=instruction, short=short)
    temperature = settings.draft_temperature if mode != "revise" else max(0.3, settings.draft_temperature - 0.3)

    prompt = base_prompt
    tin = tout = 0
    cost = 0.0
    first_hard = 0
    recents = recent_openings(db)
    out: DraftOut | None = None
    result = None
    for attempt in range(MAX_LINT_RETRIES + 1):
        res = await gemini.generate_json(purpose=f"draft:{mode}", prompt=prompt, schema=DraftOut, system=system,
                                         temperature=temperature)
        tin, tout, cost = tin + res.tokens_in, tout + res.tokens_out, cost + res.cost
        out = res.data
        text, moved = extract_verify(out.post_text.strip())
        out.post_text = text
        out.verify_flags = list(dict.fromkeys([*out.verify_flags, *moved]))
        result = lint(text, short=short, allow_hashtags=settings.allow_hashtags, opening_type=out.opening_type,
                      recent_opening_types=recents)
        if attempt == 0:
            first_hard = int(not result.ok)
        if result.ok:
            break
        log.info("Draft attempt %s failed lint: %s", attempt + 1, result.hard)
        if attempt < MAX_LINT_RETRIES:
            prompt = (f"{base_prompt}\n\nYOUR PREVIOUS ATTEMPT FAILED THESE HARD VOICE CHECKS. Fix every one and "
                      f"keep what was good:\n- " + "\n- ".join(result.hard) +
                      f"\n\nPREVIOUS ATTEMPT:\n{text}")

    assert out is not None and result is not None
    item = news.current_item(db, request_id)
    if item is not None and not any("open link" in f.lower() for f in out.verify_flags):
        out.verify_flags.append("[VERIFY: open link before posting]")
    meta = out.model_dump(exclude={"post_text"})
    meta["word_count"] = result.word_count
    meta["short"] = short
    meta["placeholders_in_text"] = find_placeholders(out.post_text)

    version = (parent["version"] + 1) if parent else 1
    draft_id = db.run(
        "INSERT INTO drafts(request_id, note_ids, version, parent_draft_id, post_text, meta_json, lint_json, "
        "news_item_id, revision_instruction, prompt_version, model, tokens_in, tokens_out, cost_est, lint_retries, "
        "lint_hard_fail_first, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'delivered')",
        (request_id, json.dumps(note_ids), version, parent_id, out.post_text, json.dumps(meta),
         json.dumps(result.to_dict()), item["id"] if item else None, instruction, prompt_version, res.model,
         tin, tout, cost, attempt, first_hard))
    if parent_id:
        db.run("UPDATE drafts SET status=?, decided_at=? WHERE id=? AND status='delivered'",
               ("revised" if mode == "revise" else "superseded", iso(), parent_id))
    db.event("draft_created", draft_id=draft_id, mode=mode, lint_ok=result.ok, retries=attempt)
    return draft_id


def unresolved_placeholders(text: str) -> list[str]:
    tags = find_placeholders(text)
    # also catch anything that looks like a placeholder even if malformed
    tags += [m.group(0) for m in re.finditer(r"\[(?:DATA|SOURCE)[^\]]*\]?", text) if m.group(0) not in tags]
    return list(dict.fromkeys(tags))


def edit_ratio(a: str, b: str) -> float:
    """Normalised word-level edit distance: 0 = identical, 1 = completely rewritten."""
    return round(1 - difflib.SequenceMatcher(None, a.split(), b.split()).ratio(), 3)
