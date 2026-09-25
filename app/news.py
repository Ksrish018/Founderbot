"""Google News RSS: query generation, fetch + filter, angle selection. Never scrapes article pages."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import quote_plus, urlsplit

import feedparser
import httpx
import yaml

from .config import CONFIG_DIR, Settings
from .db import DB, iso
from .gemini_client import Gemini
from .schemas import AngleChoice, NewsQueries, load_prompt

log = logging.getLogger(__name__)
RSS_URL = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
MAX_ITEMS = 10


@dataclass
class NewsItem:
    query: str
    title: str
    source: str
    link: str
    published: datetime | None
    snippet: str
    domain: str = ""

    @property
    def credibility(self) -> str:
        return classify(self.domain)


@lru_cache
def _sources() -> dict:
    with open(CONFIG_DIR / "news_sources.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {"trusted": [d.lower() for d in data.get("trusted") or []],
            "blocked": [d.lower() for d in data.get("blocked") or []]}


def _matches(domain: str, entries: list[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in entries)


def classify(domain: str) -> str:
    """trusted | blocked | unknown, from config/news_sources.yaml."""
    domain = (domain or "").lower().removeprefix("www.")
    if not domain:
        return "unknown"
    src = _sources()
    if _matches(domain, src["blocked"]):
        return "blocked"
    return "trusted" if _matches(domain, src["trusted"]) else "unknown"


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\xa0", " ").strip()


def parse_feed(xml: str | bytes, query: str) -> list[NewsItem]:
    feed = feedparser.parse(xml)
    items = []
    for e in feed.entries:
        pub = None
        if getattr(e, "published_parsed", None):
            pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        source = e.get("source") or {}
        src = getattr(getattr(e, "source", None), "title", None) or source.get("title", "")
        domain = (urlsplit(source.get("href") or "").hostname or "").lower().removeprefix("www.")
        title = _strip_html(e.get("title", ""))
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3]
        snippet = " ".join(_strip_html(e.get("summary", "")).split())
        if snippet.startswith(title):
            snippet = snippet[len(title):].strip(" -")
        items.append(NewsItem(query, title, src or "", e.get("link", ""), pub, snippet[:400], domain))
    return items


def filter_items(items: list[NewsItem], lookback_days: int, now: datetime | None = None,
                 trusted_only: bool = False) -> list[NewsItem]:
    """Recent, deduplicated, never from blocked publishers; trusted publishers first."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    seen: set[str] = set()
    out = []
    newest_first = sorted(items, key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for it in sorted(newest_first, key=lambda i: i.credibility != "trusted"):  # stable: trusted first, then newest
        key = re.sub(r"\W+", " ", it.title.lower()).strip()
        if not it.published or it.published < cutoff or key in seen or not it.link:
            continue
        if it.credibility == "blocked" or (trusted_only and it.credibility != "trusted"):
            continue
        seen.add(key)
        out.append(it)
    return out[:MAX_ITEMS]


async def fetch(queries: list[str], lookback_days: int, timeout: float = 15.0,
                trusted_only: bool = False, max_lookback_days: int | None = None) -> list[NewsItem]:
    """Raises on total failure so the caller can report 'RSS failed' rather than 'no angle'."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (skinstinct-content-engine)"}) as client:
        async def one(q: str):
            r = await client.get(RSS_URL.format(q=quote_plus(q)))
            r.raise_for_status()
            return parse_feed(r.content, q)
        results = await asyncio.gather(*(one(q) for q in queries), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, BaseException)]
    if not ok:
        raise ConnectionError(f"All RSS queries failed: {[type(r).__name__ for r in results]}")
    everything = [i for r in ok for i in r]
    items = filter_items(everything, lookback_days, trusted_only=trusted_only)
    if not items and max_lookback_days and max_lookback_days > lookback_days:
        items = filter_items(everything, max_lookback_days, trusted_only=trusted_only)
    return items


async def find_angle(db: DB, gemini: Gemini, settings: Settings, request_id: int, note_text: str) -> None:
    """Fills news_items + draft_requests.news_ranking/news_status for a draft request."""
    qp, _ = load_prompt("news_queries")
    try:
        q = await gemini.generate_json(purpose="news_queries", prompt=f"{qp}\n\nNOTE:\n{note_text}",
                                       schema=NewsQueries, temperature=settings.triage_temperature)
        queries = [s.strip() for s in q.data.queries if s.strip()][:4]
        items = await fetch(queries, settings.news_lookback_days, trusted_only=settings.news_trusted_only,
                            max_lookback_days=settings.news_max_lookback_days)
    except Exception as e:
        log.warning("News lookup failed: %s", e)
        db.run("UPDATE draft_requests SET news_status='rss_failed', news_reason=?, news_ranking='[]', news_pos=-1 "
               "WHERE id=?", (f"News lookup failed ({e.__class__.__name__}); drafted without an angle.", request_id))
        return

    ids = [db.run("INSERT INTO news_items(draft_request_id, query, title, source, link, published_at, snippet, "
                  "source_domain, credibility) VALUES (?,?,?,?,?,?,?,?,?)",
                  (request_id, it.query, it.title, it.source, it.link, iso(it.published) if it.published else None,
                   it.snippet, it.domain, it.credibility)) for it in items]
    if not items:
        which = "trusted publishers" if settings.news_trusted_only else "news sources"
        db.run("UPDATE draft_requests SET news_status='none', news_reason=?, news_ranking='[]', news_pos=-1 WHERE id=?",
               (f"No recent coverage from {which} in the last {settings.news_max_lookback_days} days for: "
                f"{', '.join(queries)}.", request_id))
        return

    ap, _ = load_prompt("angle")
    listing = "\n".join(f"[{i}] {it.title} | {it.source} ({it.domain or 'unknown domain'}, {it.credibility}) | "
                        f"{it.published:%d %b %Y} | {it.snippet}" for i, it in enumerate(items))
    try:
        a = await gemini.generate_json(purpose="angle", prompt=f"{ap}\n\nNOTE:\n{note_text}\n\nNEWS ITEMS:\n{listing}",
                                       schema=AngleChoice, temperature=settings.triage_temperature)
        ranked = [ids[i] for i in dict.fromkeys(a.data.ranked_indices) if 0 <= i < len(ids)]
        reason = a.data.reason
    except Exception as e:
        log.warning("Angle selection failed: %s", e)
        ranked, reason = [], f"Angle selection failed ({e.__class__.__name__}); drafted without an angle."
    db.run("UPDATE draft_requests SET news_status=?, news_reason=?, news_ranking=?, news_pos=? WHERE id=?",
           ("ok" if ranked else "none", reason, json.dumps(ranked), 0 if ranked else -1, request_id))
    if ranked:
        db.run("UPDATE news_items SET chosen=1 WHERE id=?", (ranked[0],))


def current_item(db: DB, request_id: int):
    req = db.one("SELECT * FROM draft_requests WHERE id=?", (request_id,))
    ranking = json.loads(req["news_ranking"] or "[]")
    pos = req["news_pos"]
    if pos is None or pos < 0 or pos >= len(ranking):
        return None
    return db.one("SELECT * FROM news_items WHERE id=?", (ranking[pos],))


def advance(db: DB, request_id: int) -> bool:
    """Move to the next-best item. Returns False if none are left."""
    req = db.one("SELECT * FROM draft_requests WHERE id=?", (request_id,))
    ranking = json.loads(req["news_ranking"] or "[]")
    nxt = (req["news_pos"] if req["news_pos"] is not None and req["news_pos"] >= 0 else -1) + 1
    if nxt >= len(ranking):
        return False
    db.run("UPDATE draft_requests SET news_pos=? WHERE id=?", (nxt, request_id))
    db.run("UPDATE news_items SET chosen=CASE WHEN id=? THEN 1 ELSE 0 END WHERE draft_request_id=?",
           (ranking[nxt], request_id))
    return True


def drop(db: DB, request_id: int) -> None:
    db.run("UPDATE draft_requests SET news_pos=-1 WHERE id=?", (request_id,))
    db.run("UPDATE news_items SET chosen=0 WHERE draft_request_id=?", (request_id,))


def item_block(item) -> str:
    if item is None:
        return "NEWS ITEM: none"
    return (f"NEWS ITEM (reference only what the title and snippet state; attribute it to the publisher by name):\n"
            f"Title: {item['title']}\nPublisher: {item['source']} ({item['source_domain'] or 'unknown domain'}, "
            f"{item['credibility'] or 'unknown'} source)\nDate: {(item['published_at'] or '')[:10]}\n"
            f"Snippet: {item['snippet']}")
