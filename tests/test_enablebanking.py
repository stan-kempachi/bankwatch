import base64
import json
from datetime import date

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from bankwatch import dashboard
from bankwatch.config import settings
from bankwatch.db import Database
from bankwatch.enablebanking import EnableBankingClient, pick_balance, to_raw
from bankwatch.sync import eb_finalize, eb_start_link, eb_sync_all

APP_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    p = tmp_path_factory.mktemp("k") / "eb.pem"
    p.write_bytes(pem)
    return p, key.public_key(), pem


TX = [
    {"entry_reference": "E1", "transaction_amount": {"amount": "64.30", "currency": "EUR"},
     "credit_debit_indicator": "DBIT", "status": "BOOK", "booking_date": "2026-11-01",
     "creditor": {"name": "CARREFOUR MARKET"}, "remittance_information": ["CB CARREFOUR 01/11"]},
    {"entry_reference": "E2", "transaction_amount": {"amount": "64.30", "currency": "EUR"},
     "credit_debit_indicator": "DBIT", "status": "BOOK", "booking_date": "2026-11-01",
     "creditor": {"name": "CARREFOUR MARKET"}, "remittance_information": ["CB CARREFOUR 01/11"]},
    {"entry_reference": "E3", "transaction_amount": {"amount": "3200", "currency": "EUR"},
     "credit_debit_indicator": "CRDT", "status": "PDNG", "booking_date": "2026-11-02",
     "debtor": {"name": "CLIENT"}, "remittance_information": ["VIR FACTURE"]},
]


def make_api(public_key, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["Authorization"].split()[1]
        claims = jwt.decode(token, public_key, algorithms=["RS256"], audience="api.enablebanking.com")
        assert claims["iss"] == "enablebanking.com"
        assert jwt.get_unverified_header(token)["kid"] == APP_ID
        calls.append((request.method, request.url.path, dict(request.url.params),
                      json.loads(request.content) if request.content else None))
        p = request.url.path
        if p == "/aspsps":
            return httpx.Response(200, json={"aspsps": [{"name": "Boursorama", "country": "FR"},
                                                        {"name": "Société Générale", "country": "FR"}]})
        if p == "/auth":
            return httpx.Response(200, json={"url": "https://bank.example/consent", "authorization_id": "A1"})
        if p == "/sessions":
            return httpx.Response(200, json={"session_id": "S1", "accounts": [
                {"uid": "acc-uid-1", "account_id": {"iban": "FR7600000000000000000000001"}, "name": "Compte courant", "currency": "EUR"},
                {"uid": None, "name": "Produit non couvert"}]})
        if p.endswith("/transactions"):
            if "continuation_key" not in request.url.params:
                return httpx.Response(200, json={"transactions": TX[:2], "continuation_key": "K2"})
            return httpx.Response(200, json={"transactions": TX[2:]})
        if p.endswith("/balances"):
            return httpx.Response(200, json={"balances": [
                {"balance_type": "CLBD", "balance_amount": {"amount": "100.00", "currency": "EUR"}},
                {"balance_type": "ITAV", "balance_amount": {"amount": "1234.56", "currency": "EUR"}, "reference_date": "2026-11-03"}]})
        return httpx.Response(404, json={"error": p})
    return handler


@pytest.fixture
def eb(keypair, monkeypatch):
    path, pub, _ = keypair
    calls = []
    http = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(make_api(pub, calls)))
    monkeypatch.setattr(settings, "eb_redirect_url", "https://bankwatch.example/callback")
    c = EnableBankingClient(app_id=APP_ID, key_path=path, base_url="https://api.test", http=http)
    c.calls = calls
    return c


def test_jwt_and_aspsps(eb):
    assert [a["name"] for a in eb.find_aspsp("bourso")] == ["Boursorama"]


def test_full_link_and_sync_flow(eb, tmp_path):
    db = Database(tmp_path / "t.db")
    url = eb_start_link(db, eb, "boursobank", "Boursorama")
    assert url == "https://bank.example/consent"
    body = [c for c in eb.calls if c[1] == "/auth"][0][3]
    assert body["aspsp"] == {"name": "Boursorama", "country": "FR"}
    assert body["redirect_url"] == "https://bankwatch.example/callback"
    state = body["state"]

    bank, n = eb_finalize(db, eb, "CODE", state)
    assert (bank, n) == ("boursobank", 1)
    acc = db.accounts()[0]
    assert acc["iban"].startswith("FR76") and acc["requisition_id"] == "S1"

    results, new = eb_sync_all(db, eb)
    assert results[0].error is None and len(new) == 3  # pagination suivie
    amounts = sorted(t.amount for t in new)
    assert amounts == [-64.3, -64.3, 3200.0]  # DBIT négatif, deux débits identiques conservés
    assert {t.status for t in new} == {"booked", "pending"}
    assert db.accounts()[0]["balance"] == 1234.56  # ITAV préféré
    # resync : rien de nouveau
    assert eb_sync_all(db, eb)[1] == []


def test_finalize_unknown_state(eb, tmp_path):
    with pytest.raises(ValueError):
        eb_finalize(Database(tmp_path / "t.db"), eb, "CODE", "nope")


def test_to_raw_and_balance_helpers():
    raw = to_raw(TX[0])
    assert raw["transactionAmount"]["amount"] == "-64.3" and raw["creditorName"] == "CARREFOUR MARKET"
    assert pick_balance([]) is None


# ------------------------------------------------------------- dashboard : /setup & /callback
def basic():
    return {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_password", "pw")
    monkeypatch.setattr(settings, "dashboard_user", "admin")
    monkeypatch.setattr(settings, "eb_key_path", tmp_path / "data" / "enablebanking.pem")
    db_path = tmp_path / "w.db"
    monkeypatch.setattr(dashboard, "_db", lambda: Database(db_path))
    monkeypatch.setattr(settings, "db_path", db_path)
    dashboard._failures.clear()
    dashboard._auth_cache.clear()
    return TestClient(dashboard.app), tmp_path, db_path


def test_setup_key_upload(web, keypair):
    client, tmp, _ = web
    assert client.post("/setup/key", content=b"x").status_code == 401  # auth obligatoire (pas de Accept html)
    assert client.post("/setup/key", content=b"pas une cle", headers=basic()).status_code == 400
    r = client.post("/setup/key", content=keypair[2], headers=basic())
    assert r.status_code == 200
    p = tmp / "data" / "enablebanking.pem"
    assert p.read_bytes() == keypair[2] and (p.stat().st_mode & 0o777) == 0o600
    assert client.post("/setup/key", content=keypair[2], headers=basic()).status_code == 409  # pas d'écrasement
    assert "déjà installée" in client.get("/setup", headers=basic()).text


def test_callback_error_and_missing(web):
    client, *_ = web
    r = client.get("/callback?error=access_denied&error_description=Refus%20%3Cb%3E", headers=basic())
    assert r.status_code == 400 and "<b>" not in r.text  # échappé


def test_callback_success(web, eb, monkeypatch):
    client, _, db_path = web
    db = Database(db_path)
    eb_start_link(db, eb, "sg", "Société Générale")
    state = [c for c in eb.calls if c[1] == "/auth"][-1][3]["state"]
    monkeypatch.setattr("bankwatch.enablebanking.EnableBankingClient", lambda *a, **k: eb)
    r = client.get(f"/callback?code=C&state={state}", headers=basic())
    assert r.status_code == 200 and "1 compte" in r.text


def test_daily_quota_guard(eb, tmp_path, monkeypatch):
    """3 synchros/jour : transactions à chaque fois, solde une seule fois ; jamais plus de 4 accès."""
    from bankwatch.sync import accesses_today

    monkeypatch.setattr(settings, "eb_daily_access_limit", 4)
    db = Database(tmp_path / "q.db")
    eb_start_link(db, eb, "indy", "Swan")
    state = [c for c in eb.calls if c[1] == "/auth"][-1][3]["state"]
    eb_finalize(db, eb, "CODE", state)
    acc = db.accounts()[0]["id"]
    d = date(2026, 11, 4)

    def api_calls():
        return [c for c in eb.calls if c[1].startswith(f"/accounts/{acc}/")]

    n0 = len(api_calls())
    eb_sync_all(db, eb, today=d)                       # tx (2 pages = 1 accès logique) + solde
    assert accesses_today(db, acc, d) == 2
    eb_sync_all(db, eb, today=d); eb_sync_all(db, eb, today=d)
    assert accesses_today(db, acc, d) == 4             # pas de 2e lecture du solde
    r, _ = eb_sync_all(db, eb, today=d)                # 4e passage : sauté, aucun appel
    assert r[0].skipped and len(api_calls()) == len(api_calls())
    before = len(api_calls())
    eb_sync_all(db, eb, today=d)
    assert len(api_calls()) == before
    # le lendemain, ça repart
    r, _ = eb_sync_all(db, eb, today=date(2026, 11, 5))
    assert r[0].error is None and not r[0].skipped


def test_rate_limit_is_not_a_consent_error(keypair, tmp_path, monkeypatch):
    path, pub, _ = keypair
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if "/transactions" in request.url.path:
            return httpx.Response(429, json={"code": 429, "error": "ASPSP_RATE_LIMIT_EXCEEDED", "message": "Maximum daily access exceeded"})
        return make_api(pub, [])(request)

    http = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = EnableBankingClient(app_id=APP_ID, key_path=path, base_url="https://api.test", http=http)
    db = Database(tmp_path / "r.db")
    db.upsert_account(id="acc-uid-1", bank="indy", institution_id="Swan", requisition_id="S", iban=None, name="pro", currency="EUR")
    r, _ = eb_sync_all(db, c, today=date(2026, 11, 4))
    assert r[0].rate_limited and r[0].error
    from bankwatch.sync import accesses_today

    assert accesses_today(db, "acc-uid-1", date(2026, 11, 4)) == 4  # bloqué pour le reste de la journée
