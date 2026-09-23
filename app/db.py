"""SQLite schema and repository functions. One connection, used from the asyncio thread."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,                -- telegram | import
    chat_id INTEGER,
    message_id INTEGER,
    type TEXT NOT NULL,                  -- text | voice | caption | link
    text TEXT,
    transcript TEXT,
    file_id TEXT,
    has_attachment INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',  -- new|scored|shortlisted|skipped|parked|drafting|used|unpublishable
    park_until TEXT,
    UNIQUE(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id),
    run_id TEXT,
    publishability INTEGER,
    category TEXT,
    core_gap TEXT,
    opening_type TEXT,
    needs_facts TEXT,
    risk_flags TEXT,
    cluster_with TEXT,
    reason TEXT,
    not_publishable_reason TEXT,
    model TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_ids TEXT NOT NULL,
    news_ranking TEXT,                   -- JSON list of news_items ids, best first
    news_pos INTEGER DEFAULT 0,          -- index into news_ranking currently used; -1 = no news
    news_status TEXT,                    -- ok | none | rss_failed
    news_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_request_id INTEGER REFERENCES draft_requests(id),
    query TEXT, title TEXT, source TEXT, link TEXT, published_at TEXT, snippet TEXT,
    chosen INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER REFERENCES draft_requests(id),
    note_ids TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    parent_draft_id INTEGER,
    post_text TEXT NOT NULL,
    meta_json TEXT,
    lint_json TEXT,
    news_item_id INTEGER,
    revision_instruction TEXT,
    prompt_version TEXT,
    model TEXT,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_est REAL DEFAULT 0,
    lint_retries INTEGER DEFAULT 0,
    lint_hard_fail_first INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'delivered',  -- delivered|revised|approved|rejected|superseded
    reject_reason TEXT,
    post_message_ids TEXT,
    card_message_id INTEGER,
    delivered_at TEXT,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS finals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER REFERENCES drafts(id),
    final_text TEXT NOT NULL,
    edit_ratio REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exemplars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                  -- linkedin | newsletter | approved
    name TEXT UNIQUE,
    category TEXT,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_key TEXT, old_status TEXT, new_status TEXT, value TEXT, changed_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER,
    latency_ms INTEGER, cost_est REAL, ok INTEGER, error TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL, payload_json TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class DB:
    def __init__(self, path: str | Path):
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- helpers ---------------------------------------------------------
    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def run(self, sql: str, params: Iterable[Any] = ()) -> int:
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cur.lastrowid

    def event(self, type_: str, **payload: Any) -> None:
        self.run("INSERT INTO events(type, payload_json, created_at) VALUES (?,?,?)",
                 (type_, json.dumps(payload, default=str), iso()))

    # ---- kv ---------------------------------------------------------------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.run("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))

    # ---- notes ------------------------------------------------------------
    def upsert_note(self, *, source: str, chat_id: int | None, message_id: int | None, type_: str,
                    text: str | None, transcript: str | None = None, file_id: str | None = None,
                    has_attachment: bool = False, created_at: str | None = None) -> tuple[int, bool]:
        """Insert a note, or update text if (chat_id, message_id) exists. Returns (id, created)."""
        if chat_id is not None and message_id is not None:
            row = self.one("SELECT id FROM notes WHERE chat_id=? AND message_id=?", (chat_id, message_id))
            if row:
                self.run("UPDATE notes SET text=?, type=?, has_attachment=?, "
                         "transcript=COALESCE(?, transcript), file_id=COALESCE(?, file_id) WHERE id=?",
                         (text, type_, int(has_attachment), transcript, file_id, row["id"]))
                return row["id"], False
        nid = self.run(
            "INSERT INTO notes(source, chat_id, message_id, type, text, transcript, file_id, has_attachment, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (source, chat_id, message_id, type_, text, transcript, file_id, int(has_attachment), created_at or iso()))
        return nid, True

    def note(self, note_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM notes WHERE id=?", (note_id,))

    def note_body(self, row: sqlite3.Row) -> str:
        parts = [p for p in (row["text"], row["transcript"]) if p]
        body = "\n".join(parts).strip()
        return body or "(empty note)"

    def set_note_status(self, note_ids: Iterable[int], status: str, park_days: int | None = None) -> None:
        park_until = iso(now() + timedelta(days=park_days)) if park_days else None
        for nid in note_ids:
            self.run("UPDATE notes SET status=?, park_until=? WHERE id=?", (status, park_until, nid))

    def latest_notes(self, n: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (n,))

    def notes_with_status(self, status: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM notes WHERE status=? ORDER BY id", (status,))

    def release_expired_parks(self) -> int:
        cur = self.conn.execute(
            "UPDATE notes SET status='scored', park_until=NULL "
            "WHERE status IN ('skipped','parked') AND park_until IS NOT NULL AND park_until <= ?", (iso(),))
        self.conn.commit()
        return cur.rowcount

    # ---- scores -----------------------------------------------------------
    def add_score(self, run_id: str, model: str, s: dict) -> None:
        self.run(
            "INSERT INTO scores(note_id, run_id, publishability, category, core_gap, opening_type, needs_facts, "
            "risk_flags, cluster_with, reason, not_publishable_reason, model, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s["note_id"], run_id, s["publishability"], s["category"], s["core_gap"], s["opening_type"],
             json.dumps(s.get("needs_facts", [])), json.dumps(s.get("risk_flags", [])),
             json.dumps(s.get("cluster_with", [])), s["reason"], s.get("not_publishable_reason"), model, iso()))

    def latest_score(self, note_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM scores WHERE note_id=? ORDER BY id DESC LIMIT 1", (note_id,))

    def shortlist_candidates(self, min_score: int) -> list[sqlite3.Row]:
        """Latest score per eligible note, best first."""
        return self.q(
            """SELECT n.*, s.publishability, s.category, s.core_gap, s.opening_type, s.risk_flags,
                      s.cluster_with, s.reason, s.needs_facts
               FROM notes n JOIN scores s ON s.id = (SELECT MAX(id) FROM scores WHERE note_id = n.id)
               WHERE n.status IN ('scored','shortlisted') AND s.publishability >= ?
               ORDER BY s.publishability DESC, n.id DESC""", (min_score,))

    # ---- drafts -----------------------------------------------------------
    def draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM drafts WHERE id=?", (draft_id,))

    def draft_by_message(self, message_id: int) -> sqlite3.Row | None:
        for row in self.q("SELECT * FROM drafts WHERE post_message_ids IS NOT NULL OR card_message_id IS NOT NULL "
                          "ORDER BY id DESC LIMIT 200"):
            ids = json.loads(row["post_message_ids"] or "[]")
            if message_id in ids or row["card_message_id"] == message_id:
                return row
        return None

    def recent_approved(self, n: int = 5) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM drafts WHERE status='approved' ORDER BY decided_at DESC LIMIT ?", (n,))

    def llm_log(self, **kw: Any) -> None:
        self.run("INSERT INTO llm_calls(purpose, model, tokens_in, tokens_out, latency_ms, cost_est, ok, error, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (kw.get("purpose"), kw.get("model"), kw.get("tokens_in", 0), kw.get("tokens_out", 0),
                  kw.get("latency_ms", 0), kw.get("cost_est", 0.0), int(kw.get("ok", True)), kw.get("error"), iso()))
