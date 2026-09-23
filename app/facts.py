"""Fact bank (config/facts.yaml). Drafts see canonical facts only; conflicts are resolved by Meera via /facts."""
from __future__ import annotations

from pathlib import Path

import yaml

from .config import CONFIG_DIR
from .db import DB, iso

FACTS_PATH = CONFIG_DIR / "facts.yaml"


def load(path: Path = FACTS_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def save(data: dict, path: Path = FACTS_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=120)


def canonical_block(data: dict | None = None) -> str:
    """Text block for the drafting prompt: canonical facts, resolved conflicts, unresolved conflicts, open loops."""
    data = data or load()
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


def conflicts(data: dict | None = None) -> list[dict]:
    return (data or load()).get("conflicts", [])


def resolve(db: DB, key: str, value: str, path: Path = FACTS_PATH) -> dict:
    data = load(path)
    for c in data.get("conflicts", []):
        if c["key"] == key:
            old = c.get("status")
            c["status"] = "canonical"
            c["resolution"] = value.strip()
            c["resolved_at"] = iso()
            save(data, path)
            db.run("INSERT INTO facts_log(fact_key, old_status, new_status, value, changed_at) VALUES (?,?,?,?,?)",
                   (key, old, "canonical", value.strip(), iso()))
            return c
    raise KeyError(key)
