"""Pick 2-3 published pieces as few-shot voice exemplars, matched by category. Always at least one LinkedIn post."""
from __future__ import annotations

import sqlite3

from .db import DB


def pick(db: DB, category: str | None, k: int = 3) -> list[sqlite3.Row]:
    rows = db.q("SELECT * FROM exemplars ORDER BY id")
    if not rows:
        return []

    def rank(r: sqlite3.Row) -> tuple:
        # Same category first, then Meera's own approved finals, then LinkedIn over newsletter.
        return (r["category"] != category, r["kind"] != "approved", r["kind"] != "linkedin", -r["id"])

    ordered = sorted(rows, key=rank)
    chosen = ordered[:k]
    if not any(r["kind"] in ("linkedin", "approved") for r in chosen):
        li = next((r for r in ordered if r["kind"] == "linkedin"), None)
        if li is not None:
            chosen = chosen[:k - 1] + [li]
    return chosen


def block(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "VOICE EXEMPLARS: none available."
    parts = ["VOICE EXEMPLARS (published pieces by Meera; match the voice, do not copy sentences or reuse their facts "
             "unless the same facts are in CANONICAL FACTS):"]
    for r in rows:
        parts.append(f"--- {r['name'] or r['kind']} ({r['kind']}, {r['category']}) ---\n{r['text']}")
    return "\n\n".join(parts)
