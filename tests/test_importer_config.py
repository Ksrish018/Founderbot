from __future__ import annotations

from datetime import time

from app.config import DATA_DIR, mask, parse_schedule
from app.importer import import_notes, import_published, load_note_files, split_fragments


def test_schedule_parse_uses_ptb_sunday_zero():
    assert parse_schedule("MON,WED,FRI@08:00") == ((1, 3, 5), time(8, 0))
    assert parse_schedule("sun@18:30") == ((0,), time(18, 30))


def test_mask_never_reveals_secret():
    assert mask("8957123456:AAHxyzLEB4") == "8957…LEB4"
    assert mask("") == "<empty>" and mask("short") == "…"


def test_notes_import_is_idempotent(db):
    notes = load_note_files(DATA_DIR / "notes")
    assert len(notes) == 5
    assert all("�" not in t and "—" not in t for _, t in notes)
    assert import_notes(db, DATA_DIR / "notes") == 5
    assert import_notes(db, DATA_DIR / "notes") == 0
    assert db.one("SELECT COUNT(*) c FROM notes WHERE source='import' AND status='new'")["c"] == 5


def test_published_import(db):
    assert import_published(db, DATA_DIR / "published") == 15
    rows = {r["kind"]: r["c"] for r in db.q("SELECT kind, COUNT(*) c FROM exemplars GROUP BY kind")}
    assert rows == {"linkedin": 4, "newsletter": 11}
    cats = {r["category"] for r in db.q("SELECT category FROM exemplars")}
    assert "Unknown" not in cats and "Founder Story" in cats


def test_split_on_delimiters():
    assert split_fragments("one idea\n---\nsecond idea\n\n===\nthird") == ["one idea", "second idea", "third"]
    assert split_fragments("a single note - with a spaced hyphen") == ["a single note - with a spaced hyphen"]


def test_pg_url_password_with_special_characters_is_encoded():
    from urllib.parse import unquote, urlsplit

    from app.db import clean_pg_url
    for pw in ["pa@ss321", "a#b/c?d:e", "plain123", "already%40encoded"]:
        url = clean_pg_url(f"postgresql://postgres.ref:{pw}@aws-0-ap-south-1.pooler.supabase.com:6543/postgres")
        parts = urlsplit(url)
        assert parts.hostname == "aws-0-ap-south-1.pooler.supabase.com" and parts.port == 6543
        assert parts.username == "postgres.ref"
        assert unquote(parts.password) == unquote(pw)
        assert "sslmode=require" in url



def test_existing_database_gets_new_columns(tmp_path):
    import sqlite3

    from app.db import DB
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE news_items (id INTEGER PRIMARY KEY AUTOINCREMENT, draft_request_id INTEGER, query TEXT, "
              "title TEXT, source TEXT, link TEXT, published_at TEXT, snippet TEXT, chosen INTEGER DEFAULT 0)")
    c.commit()
    c.close()
    db = DB(path)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(news_items)").fetchall()}
    assert {"source_domain", "credibility"} <= cols
    DB(path)  # running again is harmless
