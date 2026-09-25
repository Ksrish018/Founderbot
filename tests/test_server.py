"""Vercel entrypoint: webhook secret check, update dedupe, cron auth. Telegram itself is faked."""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

import server
from app.db import DB


class FakeTG:
    def __init__(self):
        self.bot = object()
        self.processed = []

    async def process_update(self, update):
        self.processed.append(update.update_id)


@pytest.fixture
def client(monkeypatch, settings):
    fake_tg = FakeTG()
    db = DB(":memory:")
    settings.cron_secret = "cron-test-secret"
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "_db", db)

    @asynccontextmanager
    async def fake_app():
        yield fake_tg
    monkeypatch.setattr(server, "telegram_app", fake_app)
    ran = []

    async def fake_triage(ctx, announce_empty):
        ran.append("triage")
    monkeypatch.setattr(server, "run_triage", fake_triage)
    c = TestClient(server.app)
    c.fake_tg, c.db, c.ran, c.settings = fake_tg, db, ran, settings
    return c


def update(uid: int) -> dict:
    return {"update_id": uid, "message": {"message_id": 1, "date": 0, "chat": {"id": 42, "type": "private"},
                                          "from": {"id": 42, "is_bot": False, "first_name": "M"}, "text": "/help"}}


def test_status_and_dashboard_are_public_and_leak_nothing(client):
    r = client.get("/api/status")
    assert r.status_code == 200 and r.json()["ok"] is True
    page = client.get("/")
    assert page.status_code == 200 and "<title>Content Capture</title>" in page.text
    for secret in (client.settings.telegram_bot_token, client.settings.cron_secret):
        assert secret not in page.text


def test_webhook_rejects_wrong_secret(client):
    r = client.post("/api/telegram", json=update(1), headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert r.status_code == 403 and client.fake_tg.processed == []


def test_webhook_processes_once(client):
    h = {"X-Telegram-Bot-Api-Secret-Token": client.settings.telegram_webhook_secret}
    assert client.post("/api/telegram", json=update(7), headers=h).json() == {"ok": True}
    assert client.post("/api/telegram", json=update(7), headers=h).json()["duplicate"] is True
    assert client.fake_tg.processed == [7]


def test_cron_requires_bearer(client):
    assert client.get("/api/cron/triage").status_code == 401
    r = client.get("/api/cron/triage", headers={"Authorization": "Bearer cron-test-secret"})
    assert r.status_code == 200 and client.ran == ["triage"]


def test_cron_respects_pause(client):
    client.db.set_kv("triage_paused", "1")
    r = client.get("/api/cron/triage", headers={"Authorization": "Bearer cron-test-secret"})
    assert r.json()["skipped"] == "paused" and client.ran == []


def test_setup_requires_key(client):
    assert client.get("/api/setup").status_code == 401
    assert client.get("/api/setup?key=wrong").status_code == 401


def test_webhook_secret_is_stable_and_not_the_token(settings):
    s = settings.telegram_webhook_secret
    assert s == settings.telegram_webhook_secret and settings.telegram_bot_token not in s
    assert len(s) == 48 and s.isalnum()
