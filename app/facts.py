"""Fact bank. config/facts.yaml is the read-only baseline from voice skill §14; Meera's /facts resolutions are
stored in the database (fact_resolutions) and overlaid on it, so they survive serverless deploys."""
from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path

import yaml

from .config import CONFIG_DIR
from .db import DB, iso

FACTS_PATH = CONFIG_DIR / "facts.yaml"


@lru_cache
def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load(db: DB | None = None, path: Path = FACTS_PATH) -> dict:
    """Baseline facts with any resolutions from the database applied."""
    data = copy.deepcopy(_load_yaml(str(path)))
    if db is not None:
        res = {r["fact_key"]: r for r in db.q("SELECT * FROM fact_resolutions")}
        for c in data.get("conflicts", []):
            r = res.get(c["key"])
            if r:
                c.update(status="canonical", resolution=r["value"], resolved_at=r["resolved_at"])
    return data


def canonical_block(data: dict) -> str:
    """Text block for prompts: canonical facts, resolved conflicts, unresolved conflicts, open loops."""
    lines = ["CANONICAL FACTS (the only facts about Meera and Skinstinct you may state):"]
    lines += [f"- [{f['key']}] {f['text']}" for f in data.get("facts", []) if f.get("status") == "canonical"]
    resolved = [c for c in data.get("conflicts", []) if c.get("status") == "canonical" and c.get("resolution")]
    lines += [f"- [{c['key']}] {c['title']}: {c['resolution']}" for c in resolved]
    open_conf = [c for c in data.get("conflicts", []) if c.get("status") == "conflict"]
    if open_conf:
        lines.append("\nCONFLICTS (DO NOT USE; if the post needs one, insert [DATA NEEDED: ...] and tell Meera):")
        lines += [f"- [{c['key']}] {c['title']}: {c['text']}" for c in open_conf]
    loops = data.get("open_loops", [])
    if loops:
        lines.append("\nOPEN PUBLIC PROMISES (reference or leave open; never pretend they are closed):")
        lines += [f"- {l}" for l in loops]
    return "\n".join(lines)


def conflicts(data: dict) -> list[dict]:
    return data.get("conflicts", [])


def resolve(db: DB, key: str, value: str, path: Path = FACTS_PATH) -> dict:
    data = load(db, path)
    c = next((c for c in data.get("conflicts", []) if c["key"] == key), None)
    if c is None:
        raise KeyError(key)
    old, value, t = c.get("status"), value.strip(), iso()
    db.run("INSERT INTO fact_resolutions(fact_key, value, resolved_at) VALUES (?,?,?) "
           "ON CONFLICT(fact_key) DO UPDATE SET value=excluded.value, resolved_at=excluded.resolved_at", (key, value, t))
    db.run("INSERT INTO facts_log(fact_key, old_status, new_status, value, changed_at) VALUES (?,?,?,?,?)",
           (key, old, "canonical", value, t))
    c.update(status="canonical", resolution=value, resolved_at=t)
    return c
