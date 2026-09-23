"""Database layer. SQLite for local runs and tests; Postgres (Supabase/Neon) when DATABASE_URL or POSTGRES_URL is set,
which is how it runs on Vercel. The SQL is written once with `?` placeholders and translated for Postgres."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TABLES = """
CREATE TABLE IF NOT EXISTS notes (
    id {pk},
    source TEXT NOT NULL,                -- telegram | import
    chat_id BIGINT,
    message_id BIGINT,
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
    id {pk},
    note_id BIGINT NOT NULL REFERENCES notes(id),
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
    id {pk},
    note_ids TEXT NOT NULL,
    news_ranking TEXT,                   -- JSON list of news_items ids, best first
    news_pos INTEGER DEFAULT 0,          -- index into news_ranking currently used; -1 = no news
    news_status TEXT,                    -- ok | none | rss_failed
    news_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_items (
    id {pk},
    draft_request_id BIGINT REFERENCES draft_requests(id),
    query TEXT, title TEXT, source TEXT, link TEXT, published_at TEXT, snippet TEXT,
    chosen INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drafts (
    id {pk},
    request_id BIGINT REFERENCES draft_requests(id),
    note_ids TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    parent_draft_id BIGINT,
    post_text TEXT NOT NULL,
    meta_json TEXT,
    lint_json TEXT,
    news_item_id BIGINT,
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
    card_message_id BIGINT,
    delivered_at TEXT,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS finals (
    id {pk},
    draft_id BIGINT REFERENCES drafts(id),
    final_text TEXT NOT NULL,
    edit_ratio REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exemplars (
    id {pk},
    kind TEXT NOT NULL,                  -- linkedin | newsletter | approved
    name TEXT UNIQUE,
    category TEXT,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts_log (
    id {pk},
    fact_key TEXT, old_status TEXT, new_status TEXT, value TEXT, changed_at TEXT
);
CREATE TABLE IF NOT EXISTS fact_resolutions (
    fact_key TEXT PRIMARY KEY, value TEXT NOT NULL, resolved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id {pk},
    purpose TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER,
    latency_ms INTEGER, cost_est REAL, ok INTEGER, error TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id {pk},
    type TEXT NOT NULL, note_id BIGINT, payload_json TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS locks (key TEXT PRIMARY KEY, until TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS processed_updates (update_id BIGINT PRIMARY KEY, created_at TEXT NOT NULL)
"""

# Tables without an `id` column: inserts into these must not ask Postgres for RETURNING id.
NO_ID_TABLES = {"kv", "locks", "processed_updates", "fact_resolutions"}
INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.I)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def clean_pg_url(url: str) -> str:
    """Vercel integrations append params psycopg doesn't understand (e.g. supa=...). Keep only known ones."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    parts = urlsplit(url)
    keep = {"sslmode", "connect_timeout", "application_name", "options"}
    q = [(k, v) for k, v in parse_qsl(parts.query) if k in keep]
    host = parts.hostname or ""
    if not any(k == "sslmode" for k, _ in q) and host not in ("localhost", "127.0.0.1"):
        q.append(("sslmode", "require"))
    return urlunsplit(parts._replace(query=urlencode(q)))


class DB:
    def __init__(self, target: str | Path):
        target = str(target)
        self.is_pg = target.startswith(("postgres://", "postgresql://"))
        self._target = clean_pg_url(target) if self.is_pg else target
        self._conn = None
        self._connect()
        self.init_schema()

    # ---- connection ---------------------------------------------------------
    def _connect(self) -> None:
        if self.is_pg:
            import psycopg
            from psycopg.rows import dict_row
            # prepare_threshold=None keeps it compatible with transaction-mode poolers (Supabase port 6543).
            self._conn = psycopg.connect(self._target, autocommit=True, row_factory=dict_row,
                                         prepare_threshold=None, connect_timeout=15)
        else:
            if self._target != ":memory:":
                Path(self._target).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._target, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")

    @property
    def conn(self):
        if self.is_pg and self._conn.closed:
            self._connect()
        return self._conn

    def init_schema(self) -> None:
        ddl = TABLES.format(pk="BIGSERIAL PRIMARY KEY" if self.is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT")
        if self.is_pg:
            ddl = re.sub(r"--[^\n]*", "", ddl)  # comments may contain ';'
            for stmt in ddl.split(";"):
                if stmt.strip():
                    self.conn.execute(stmt)
        else:
            self.conn.executescript(ddl)
            self.conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ---- helpers ------------------------------------------------------------
    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_pg else sql

    def _exec(self, sql: str, params: Iterable[Any] = ()):
        if self.is_pg:
            return self.conn.execute(self._sql(sql), tuple(params))
        cur = self.conn.execute(sql, tuple(params))
        return cur

    def q(self, sql: str, params: Iterable[Any] = ()) -> list:
        return self._exec(sql, params).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()):
        return self._exec(sql, params).fetchone()

    def run(self, sql: str, params: Iterable[Any] = ()) -> int | None:
        """Execute a write. For INSERTs into tables with an id, returns the new id."""
        m = INSERT_RE.match(sql)
        wants_id = bool(m) and m.group(1).lower() not in NO_ID_TABLES
        if self.is_pg:
            if wants_id and "RETURNING" not in sql.upper():
                sql = sql.rstrip().rstrip(";") + " RETURNING id"
            cur = self._exec(sql, params)
            if wants_id:
                row = cur.fetchone()
                return row["id"] if row else None
            return None
        cur = self._exec(sql, params)
        self.conn.commit()
        return cur.lastrowid if wants_id else None

    def run_rc(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Execute a write and return the number of affected rows."""
        cur = self._exec(sql, params)
        if not self.is_pg:
            self.conn.commit()
        return cur.rowcount

    def event(self, type_: str, note_id: int | None = None, **payload: Any) -> None:
        self.run("INSERT INTO events(type, note_id, payload_json, created_at) VALUES (?,?,?,?)",
                 (type_, note_id, json.dumps(payload, default=str), iso()))

    # ---- kv, locks, update dedupe ---------------------------------------------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str | None) -> None:
        if value is None:
            self.run("DELETE FROM kv WHERE key=?", (key,))
            return
        self.run("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))

    def try_lock(self, key: str, ttl_seconds: int = 330) -> bool:
        """Cross-invocation lock (serverless functions share nothing but the database). Expires after ttl."""
        t = iso()
        until = iso(now() + timedelta(seconds=ttl_seconds))
        return self.run_rc("INSERT INTO locks(key, until) VALUES (?,?) ON CONFLICT(key) DO UPDATE "
                           "SET until=excluded.until WHERE locks.until < ?", (key, until, t)) == 1

    def unlock(self, key: str) -> None:
        self.run("DELETE FROM locks WHERE key=?", (key,))

    def first_time_update(self, update_id: int) -> bool:
        """Telegram retries webhooks it thinks failed; process each update_id once."""
        return self.run_rc("INSERT INTO processed_updates(update_id, created_at) VALUES (?,?) "
                           "ON CONFLICT(update_id) DO NOTHING", (update_id, iso())) == 1

    # ---- notes ----------------------------------------------------------------
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

    def note(self, note_id: int):
        return self.one("SELECT * FROM notes WHERE id=?", (note_id,))

    def note_body(self, row) -> str:
        parts = [p for p in (row["text"], row["transcript"]) if p]
        body = "\n".join(parts).strip()
        return body or "(empty note)"

    def set_note_status(self, note_ids: Iterable[int], status: str, park_days: int | None = None) -> None:
        park_until = iso(now() + timedelta(days=park_days)) if park_days else None
        for nid in note_ids:
            self.run("UPDATE notes SET status=?, park_until=? WHERE id=?", (status, park_until, nid))

    def latest_notes(self, n: int) -> list:
        return self.q("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (n,))

    def notes_with_status(self, status: str) -> list:
        return self.q("SELECT * FROM notes WHERE status=? ORDER BY id", (status,))

    def release_expired_parks(self) -> int:
        return self.run_rc(
            "UPDATE notes SET status='scored', park_until=NULL "
            "WHERE status IN ('skipped','parked') AND park_until IS NOT NULL AND park_until <= ?", (iso(),))

    # ---- scores ---------------------------------------------------------------
    def add_score(self, run_id: str, model: str, s: dict) -> None:
        self.run(
            "INSERT INTO scores(note_id, run_id, publishability, category, core_gap, opening_type, needs_facts, "
            "risk_flags, cluster_with, reason, not_publishable_reason, model, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s["note_id"], run_id, s["publishability"], s["category"], s["core_gap"], s["opening_type"],
             json.dumps(s.get("needs_facts", [])), json.dumps(s.get("risk_flags", [])),
             json.dumps(s.get("cluster_with", [])), s["reason"], s.get("not_publishable_reason"), model, iso()))

    def latest_score(self, note_id: int):
        return self.one("SELECT * FROM scores WHERE note_id=? ORDER BY id DESC LIMIT 1", (note_id,))

    def shortlist_candidates(self, min_score: int) -> list:
        """Latest score per eligible note, best first."""
        return self.q(
            """SELECT n.*, s.publishability, s.category, s.core_gap, s.opening_type, s.risk_flags,
                      s.cluster_with, s.reason, s.needs_facts
               FROM notes n JOIN scores s ON s.id = (SELECT MAX(id) FROM scores WHERE note_id = n.id)
               WHERE n.status IN ('scored','shortlisted') AND s.publishability >= ?
               ORDER BY s.publishability DESC, n.id DESC""", (min_score,))

    # ---- drafts ---------------------------------------------------------------
    def draft(self, draft_id: int):
        return self.one("SELECT * FROM drafts WHERE id=?", (draft_id,))

    def draft_by_message(self, message_id: int):
        for row in self.q("SELECT * FROM drafts WHERE post_message_ids IS NOT NULL OR card_message_id IS NOT NULL "
                          "ORDER BY id DESC LIMIT 200"):
            ids = json.loads(row["post_message_ids"] or "[]")
            if message_id in ids or row["card_message_id"] == message_id:
                return row
        return None

    def recent_approved(self, n: int = 5) -> list:
        return self.q("SELECT * FROM drafts WHERE status='approved' ORDER BY decided_at DESC LIMIT ?", (n,))

    def llm_log(self, **kw: Any) -> None:
        self.run("INSERT INTO llm_calls(purpose, model, tokens_in, tokens_out, latency_ms, cost_est, ok, error, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (kw.get("purpose"), kw.get("model"), kw.get("tokens_in", 0), kw.get("tokens_out", 0),
                  kw.get("latency_ms", 0), kw.get("cost_est", 0.0), int(kw.get("ok", True)), kw.get("error"), iso()))
