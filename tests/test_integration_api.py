"""Jetons d'application, API d'export et webhooks CloudEvents."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from bankwatch import dashboard, tokens, webhooks
from bankwatch import session as session_mod
from bankwatch.config import settings
from bankwatch.db import Database
from bankwatch.demo import load_demo


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "d.db"
    load_demo(Database(db_path), notify=False)
    monkeypatch.setattr(dashboard, "_db", lambda: Database(db_path))
    monkeypatch.setattr(settings, "dashboard_password", "s3cret-pass")
    monkeypatch.setattr(settings, "dashboard_user", "admin")
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(session_mod, "_key_cache", None)
    dashboard._failures.clear()
    dashboard._auth_cache.clear()
    return TestClient(dashboard.app, follow_redirects=False)


def test_token_create_verify_revoke(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "d.db")
    tok = tokens.create("budget")
    assert tok.startswith("bw_") and len(tok) > 30
    assert tokens.verify(tok) == "budget"
    assert tokens.verify("bw_nope") is None
    assert tokens.verify(None) is None
    assert "hash" in tokens.list_tokens()["budget"] and tok not in (tmp_path / "tokens.json").read_text()
    assert tokens.revoke("budget") and tokens.verify(tok) is None
    with pytest.raises(ValueError):
        tokens.create("bad name!")


def test_export_requires_token(client):
    assert client.get("/api/export/transactions").status_code == 401
    assert client.get("/api/export/accounts", headers={"Authorization": "Bearer bw_faux"}).status_code == 401


def test_export_with_token_is_read_only(client):
    tok = tokens.create("budget")
    h = {"Authorization": f"Bearer {tok}"}
    accs = client.get("/api/export/accounts", headers=h)
    assert accs.status_code == 200
    a = accs.json()["accounts"][0]
    assert {"id", "bank", "iban", "pro", "balance_estimate"} <= set(a)
    txs = client.get("/api/export/transactions?since=2000-01-01", headers=h).json()
    assert txs["count"] > 0
    t = txs["items"][0]
    assert {"id", "account_id", "booking_date", "amount", "label", "category", "status", "internal"} <= set(t)
    assert client.get("/api/export/transactions?status=pending&since=2000-01-01", headers=h).status_code == 200
    # jeton = lecture seule : pas d'accès admin
    assert client.post(f"/api/sync/{a['id']}", headers=h).status_code == 403
    assert client.get("/setup", headers=h).status_code == 403


def test_signature_roundtrip():
    body = b'{"a":1}'
    sig = webhooks.sign("s3cret", body, ts=1_700_000_000)
    assert sig.startswith("t=1700000000,v1=")
    assert webhooks.verify_signature("s3cret", body, sig, now=1_700_000_100)
    assert not webhooks.verify_signature("autre", body, sig, now=1_700_000_100)
    assert not webhooks.verify_signature("s3cret", body + b" ", sig, now=1_700_000_100)
    assert not webhooks.verify_signature("s3cret", body, sig, now=1_700_001_000)  # trop vieux (rejeu)
    assert not webhooks.verify_signature("s3cret", body, "n'importe quoi", now=1_700_000_000)


class _Receiver(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        _Receiver.received.append((dict(self.headers), body))
        self.send_response(202)
        self.end_headers()

    def log_message(self, *a):  # silence
        pass


def test_emit_cloudevent_signed(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("BW_WEBHOOK_SECRET_TEST", "topsecret")
        subs = [{"name": "t", "url": f"http://127.0.0.1:{srv.server_port}/hook", "secret_env": "BW_WEBHOOK_SECRET_TEST",
                 "events": ["sync.completed"]},
                {"name": "ignored", "url": f"http://127.0.0.1:{srv.server_port}/x", "secret": "x", "events": ["alert.created"]}]
        rep = webhooks.emit("sync.completed", {"new_transactions": 3}, subs)
        assert rep == [{"name": "t", "ok": True, "status": 202, "error": None}]
        headers, body = _Receiver.received[-1]
        ev = json.loads(body)
        assert ev["specversion"] == "1.0" and ev["type"] == "dev.bankwatch.sync.completed"
        assert ev["data"] == {"new_transactions": 3}
        assert headers["Content-Type"] == "application/cloudevents+json"
        assert webhooks.verify_signature("topsecret", body, headers["X-BankWatch-Signature"])
    finally:
        srv.shutdown()


def test_emit_missing_secret_is_reported(monkeypatch):
    monkeypatch.delenv("BW_WEBHOOK_SECRET_NOPE", raising=False)
    rep = webhooks.emit("sync.completed", {}, [{"name": "x", "url": "http://127.0.0.1:9/h", "secret_env": "BW_WEBHOOK_SECRET_NOPE"}])
    assert rep[0]["ok"] is False and "secret" in rep[0]["error"]
