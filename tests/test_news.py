from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from app import news
from app.drafting import start_request

NOW = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)


def rss(items: list[tuple[str, str, datetime]]) -> str:
    body = "".join(
        f"<item><title>{t} - {s}</title><link>https://news.example/{i}</link>"
        f"<pubDate>{format_datetime(d)}</pubDate><description>&lt;a href=\"x\"&gt;{t}&lt;/a&gt; snippet {i}"
        f"</description><source url=\"https://{s}\">{s}</source></item>"
        for i, (t, s, d) in enumerate(items))
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>{body}</channel></rss>"


def test_parse_strips_source_suffix_and_html():
    items = news.parse_feed(rss([("CDSCO tightens cosmetic labelling", "Mint", NOW)]), "q")
    assert items[0].title == "CDSCO tightens cosmetic labelling"
    assert items[0].source == "Mint"
    assert "<a" not in items[0].snippet


def test_filter_by_lookback_and_dedupe():
    items = news.parse_feed(rss([
        ("Sunscreen study India", "A", NOW - timedelta(days=2)),
        ("Sunscreen study India", "B", NOW - timedelta(days=3)),   # duplicate title
        ("Old niacinamide story", "C", NOW - timedelta(days=40)),  # too old
    ]), "q")
    kept = news.filter_items(items, 14, now=NOW)
    assert [i.title for i in kept] == ["Sunscreen study India"]


def test_filter_caps_at_ten():
    items = news.parse_feed(rss([(f"Item {i}", "S", NOW - timedelta(hours=i)) for i in range(15)]), "q")
    assert len(news.filter_items(items, 14, now=NOW)) == 10


async def test_none_is_a_valid_outcome(db, fake, settings, monkeypatch):
    req = start_request(db, [1])
    fake.queue("news_queries", {"queries": ["weather Mumbai"]})
    fake.queue("angle", {"ranked_indices": [], "reason": "No item relates to preservative changes."})

    async def fake_fetch(queries, days, timeout=15.0, **kw):
        return news.parse_feed(rss([("Mumbai rain alert", "X", datetime.now(timezone.utc))]), queries[0])
    monkeypatch.setattr(news, "fetch", fake_fetch)

    await news.find_angle(db, fake, settings, req, "note about preservatives")
    row = db.one("SELECT * FROM draft_requests WHERE id=?", (req,))
    assert row["news_status"] == "none" and json.loads(row["news_ranking"]) == []
    assert news.current_item(db, req) is None
    assert news.item_block(None) == "NEWS ITEM: none"


async def test_rss_failure_drafts_without_angle(db, fake, settings, monkeypatch):
    req = start_request(db, [1])
    fake.queue("news_queries", {"queries": ["niacinamide India"]})

    async def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(news, "fetch", boom)
    await news.find_angle(db, fake, settings, req, "note")
    assert db.one("SELECT news_status FROM draft_requests WHERE id=?", (req,))["news_status"] == "rss_failed"


async def test_ranking_advance_and_drop(db, fake, settings, monkeypatch):
    req = start_request(db, [1])
    fake.queue("news_queries", {"queries": ["q"]})
    fake.queue("angle", {"ranked_indices": [1, 0, 7], "reason": "Item 1 is about CoA checks."})

    async def fake_fetch(queries, days, timeout=15.0, **kw):
        now = datetime.now(timezone.utc)
        return news.parse_feed(rss([("First", "A", now), ("Second", "B", now - timedelta(hours=1))]), "q")
    monkeypatch.setattr(news, "fetch", fake_fetch)
    await news.find_angle(db, fake, settings, req, "note")

    assert news.current_item(db, req)["title"] == "Second"   # index 7 is ignored as out of range
    assert news.advance(db, req) and news.current_item(db, req)["title"] == "First"
    assert not news.advance(db, req)
    news.drop(db, req)
    assert news.current_item(db, req) is None



def test_trusted_publishers_first_blocked_never():
    items = news.parse_feed(rss([
        ("India Niacinamide Market Size", "grandviewresearch.com", NOW - timedelta(days=1)),       # blocked
        ("Best niacinamide serums", "somelisticle.example", NOW - timedelta(days=1)),              # unknown
        ("CDSCO flags two creams", "timesofindia.indiatimes.com", NOW - timedelta(days=5)),       # trusted
    ]), "q")
    assert [i.credibility for i in items] == ["blocked", "unknown", "trusted"]
    open_mode = news.filter_items(items, 14, now=NOW)
    assert [i.title for i in open_mode] == ["CDSCO flags two creams", "Best niacinamide serums"]
    strict = news.filter_items(items, 14, now=NOW, trusted_only=True)
    assert [i.title for i in strict] == ["CDSCO flags two creams"]


def test_classify_matches_subdomains_only_on_dot_boundary():
    assert news.classify("economictimes.indiatimes.com") == "trusted"
    assert news.classify("www.reuters.com") == "trusted"
    assert news.classify("notreuters.com") == "unknown"
    assert news.classify("") == "unknown"
