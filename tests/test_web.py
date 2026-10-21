"""Web dashboard API: login, feed, capture -> score -> draft, approve / reject / revise. Telegram and Gemini are faked."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

import server
from app import news, web
from app.db import DB
from tests.conftest import GOOD_POST
from tests.test_triage_drafting import draft_json, score


@pytest.fixture
def client(monkeypatch, settings, fake):
    db = DB(":memory:")
    settings.dashboard_password = "open-sesame"
    settings.web_secure_cookie = False          # TestClient talks plain http
    settings.web_mirror_telegram = False        # NullBot: nothing goes to Telegram in tests
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(web, "Gemini", lambda s, d: fake)

    async def no_news(db_, gemini, settings_, req, text):
        db_.run("UPDATE draft_requests SET news_status='none', news_reason='No trusted, relevant story.', "
                "news_ranking='[]', news_pos=-1 WHERE id=?", (req,))
    monkeypatch.setattr(news, "find_angle", no_news)
    c = TestClient(server.app)
    c.db, c.fake = db, fake
    return c


def login(c):
    r = c.post("/api/web/login", json={"password": "open-sesame"})
    assert r.status_code == 200


def test_feed_is_a_read_only_demo_without_login(client):
    r = client.get("/api/web/feed")
    assert r.status_code == 200 and r.json()["demo"] is True
    assert r.json()["config"]["channel"] is None
    assert client.post("/api/web/notes", json={"text": "x"}).status_code == 401
    assert client.post("/api/web/notes/1/draft").status_code == 401
    assert client.post("/api/web/drafts/1/approve", json={}).status_code == 401
    login(client)
    assert client.get("/api/web/feed").json()["demo"] is False


def _pass(secret, app, exp_ms):
    enc = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    payload = enc(json.dumps({"app": app, "email": "visitor@example.com", "exp": exp_ms}).encode())
    return payload + "." + enc(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


def test_access_link_grants_a_session_until_it_expires(client, settings):
    settings.access_secret = "shared"
    exp = int(time.time() * 1000) + 3_600_000
    r = client.get("/api/web/access", params={"pass": _pass("shared", "founderbot", exp)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/api/web/feed").json()["demo"] is False


def test_access_link_rejects_bad_links(client, settings):
    settings.access_secret = "shared"
    later = int(time.time() * 1000) + 3_600_000
    for token in (_pass("shared", "founderbot", int(time.time() * 1000) - 1),   # expired
                  _pass("shared", "funding-radar", later),                      # another app
                  _pass("other", "founderbot", later),                          # wrong secret
                  "garbage"):
        r = client.get("/api/web/access", params={"pass": token}, follow_redirects=False)
        assert r.status_code == 403
    assert client.get("/api/web/feed").json()["demo"] is True


def test_wrong_password_rejected(client):
    assert client.post("/api/web/login", json={"password": "nope"}).status_code == 401


def test_forged_cookie_rejected(client):
    client.cookies.set(web.COOKIE, "9999999999.deadbeef")
    assert client.get("/api/web/feed").json()["demo"] is True
    assert client.post("/api/web/notes", json={"text": "x"}).status_code == 401


def test_capture_strong_note_drafts_it(client):
    login(client)
    client.fake.queue("triage", {"scores": [score(1, 8)]})
    client.fake.queue("draft", draft_json(GOOD_POST))
    r = client.post("/api/web/notes", json={"text": "Batch 14 pH dropped 0.4 after a supplier preservative change."})
    assert r.status_code == 200
    note = r.json()
    assert note["source"] == "web" and note["score"]["value"] == 8
    assert note["draft"]["status"] == "delivered" and note["draft"]["text"] == GOOD_POST
    assert note["draft"]["lint_ok"] and note["draft"]["news"] is None
    assert "No trusted" in note["draft"]["news_reason"]
    feed = client.get("/api/web/feed").json()
    assert feed["notes"][0]["id"] == note["id"] and feed["config"]["min_score"] == 6


def test_capture_weak_note_is_not_drafted(client):
    login(client)
    s = score(1, 3)
    s["not_publishable_reason"] = "Venting."
    client.fake.queue("triage", {"scores": [s]})
    note = client.post("/api/web/notes", json={"text": "long day"}).json()
    assert note["score"]["value"] == 3 and note["draft"] is None and note["score"]["why_not"] == "Venting."


def test_approve_then_final(client):
    login(client)
    client.fake.queue("triage", {"scores": [score(1, 9)]})
    client.fake.queue("draft", draft_json(GOOD_POST))
    did = client.post("/api/web/notes", json={"text": "CoA baseline idea"}).json()["draft"]["id"]
    r = client.post(f"/api/web/drafts/{did}/approve", json={})
    assert r.status_code == 200 and r.json()["draft"]["status"] == "approved"
    assert client.db.note(1)["status"] == "used"
    r = client.post(f"/api/web/drafts/{did}/final", json={"text": GOOD_POST.replace("fourteen", "14")})
    assert r.json()["draft"]["final"]["edit_ratio"] < 0.05


def test_approve_blocked_by_placeholders(client):
    login(client)
    client.fake.queue("triage", {"scores": [score(1, 9)]})
    client.fake.queue("draft", draft_json(GOOD_POST.replace("5.5-5.8", "[DATA NEEDED: serum pH, from Meera]")))
    did = client.post("/api/web/notes", json={"text": "idea"}).json()["draft"]["id"]
    r = client.post(f"/api/web/drafts/{did}/approve", json={})
    assert r.status_code == 409 and "DATA NEEDED" in r.json()["detail"]


def test_revise_and_reject(client):
    login(client)
    client.fake.queue("triage", {"scores": [score(1, 9)]})
    client.fake.queue("draft", draft_json(GOOD_POST))
    did = client.post("/api/web/notes", json={"text": "idea"}).json()["draft"]["id"]
    assert client.post(f"/api/web/drafts/{did}/revise", json={}).status_code == 400
    client.fake.queue("draft", draft_json(GOOD_POST))
    new = client.post(f"/api/web/drafts/{did}/revise", json={"instruction": "Shorter opening"}).json()["draft"]
    assert new["id"] != did and new["revision_instruction"] == "Shorter opening" and new["version"] == 2
    r = client.post(f"/api/web/drafts/{new['id']}/reject", json={"reason": "o"}).json()["draft"]
    assert r["status"] == "rejected" and r["reject_reason"] == "Off-voice"


def test_note_text_is_returned_raw_for_safe_client_side_escaping(client):
    login(client)
    client.fake.queue("triage", {"scores": [score(1, 2)]})
    note = client.post("/api/web/notes", json={"text": "<script>alert(1)</script>"}).json()
    assert note["text"] == "<script>alert(1)</script>"   # the page escapes it before rendering
