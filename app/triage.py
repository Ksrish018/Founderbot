"""Batch scoring, clustering and the shortlist. The AI ranks; Meera picks (AUTO_DRAFT_TOP_N=0)."""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field

from . import facts
from .config import Settings
from .db import DB
from .gemini_client import Gemini
from .schemas import TriageBatch, system_for

log = logging.getLogger(__name__)
BATCH_SIZE = 15


@dataclass
class Candidate:
    note_id: int
    score: int
    category: str
    reason: str
    core_gap: str
    preview: str
    cluster: list[int] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)

    @property
    def note_ids(self) -> list[int]:
        return [self.note_id, *self.cluster]


def pending_notes(db: DB) -> list:
    return [r for r in db.notes_with_status("new") if not (r["type"] == "voice" and not r["transcript"])]


def build_prompt(db: DB, rows: list) -> str:
    notes = "\n\n".join(f"NOTE id={r['id']} (captured {r['created_at'][:10]}, {r['type']}):\n{db.note_body(r)}"
                        for r in rows)
    return (f"{facts.canonical_block()}\n\n"
            f"Score each of these {len(rows)} notes. Use the exact note ids.\n\n{notes}")


async def score_pending(db: DB, gemini: Gemini, settings: Settings) -> int:
    rows = pending_notes(db)
    if not rows:
        return 0
    system, version = system_for("triage")
    run_id = uuid.uuid4().hex[:8]
    scored = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        ids = {r["id"] for r in batch}
        res = await gemini.generate_json(purpose="triage", prompt=build_prompt(db, batch), schema=TriageBatch,
                                         system=system, temperature=settings.triage_temperature)
        for s in res.data.scores:
            if s.note_id not in ids:
                continue
            d = s.model_dump()
            d["cluster_with"] = [c for c in s.cluster_with if c in ids and c != s.note_id]
            d["risk_flags"] = [f for f in s.risk_flags if f != "none"]
            db.add_score(run_id, res.model, d)
            db.set_note_status([s.note_id], "unpublishable" if s.publishability <= 3 else "scored")
            scored += 1
    db.event("triage_scored", run_id=run_id, count=scored, prompt_version=version)
    return scored


def build_shortlist(db: DB, settings: Settings) -> list[Candidate]:
    """All eligible candidates, best first, with clusters merged."""
    db.release_expired_parks()
    rows = db.shortlist_candidates(settings.min_shortlist_score)
    eligible = {r["id"] for r in rows}
    consumed: set[int] = set()
    out: list[Candidate] = []
    for r in rows:
        if r["id"] in consumed:
            continue
        cluster = [c for c in json.loads(r["cluster_with"] or "[]") if c in eligible and c not in consumed]
        consumed.update([r["id"], *cluster])
        body = db.note_body(r)
        out.append(Candidate(note_id=r["id"], score=r["publishability"], category=r["category"], reason=r["reason"],
                             core_gap=r["core_gap"], preview=body[:120] + ("…" if len(body) > 120 else ""),
                             cluster=cluster, risk_flags=json.loads(r["risk_flags"] or "[]")))
    return out


def mark_shown(db: DB, cands: list[Candidate]) -> None:
    for c in cands:
        db.run("UPDATE notes SET status='shortlisted' WHERE id IN (%s) AND status='scored'"
               % ",".join("?" * len(c.note_ids)), c.note_ids)


def candidate_text(c: Candidate) -> str:
    merged = f"\nMerged with note(s) {', '.join(map(str, c.cluster))}" if c.cluster else ""
    risk = f"\nRisk: {', '.join(c.risk_flags)}" if c.risk_flags else ""
    return (f"Note {c.note_id} · {c.score}/10 · {c.category}\n"
            f"Why: {c.reason}\n"
            f"Gap: {c.core_gap}{merged}{risk}\n\n"
            f"“{c.preview}”")
