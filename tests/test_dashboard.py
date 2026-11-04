import base64

import pytest
from fastapi.testclient import TestClient

from bankwatch import dashboard
from bankwatch import session as session_mod
from bankwatch.config import settings
from bankwatch.db import Database
from bankwatch.demo import load_demo


def basic(user, pwd):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "d.db"
    load_demo(Database(db_path), notify=False)
    monkeypatch.setattr(dashboard, "_db", lambda: Database(db_path))
    monkeypatch.setattr(settings, "dashboard_password", "s3cret-pass")
    monkeypatch.setattr(settings, "dashboard_user", "admin")
    monkeypatch.setattr(settings, "db_path", tmp_path / "d.db")  # users.json + secret.key dans tmp
    monkeypatch.setattr(session_mod, "_key_cache", None)
    dashboard._failures.clear()
    dashboard._auth_cache.clear()
    return TestClient(dashboard.app, follow_redirects=False)


def test_requires_auth(client):
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    assert client.get("/api/transactions").status_code == 401  # API : pas de redirection


def test_wrong_password(client):
    assert client.get("/", headers=basic("admin", "nope")).status_code == 401


def test_good_password_all_endpoints(client):
    h = basic("admin", "s3cret-pass")
    for path in ("/", "/api/summary", "/api/monthly", "/api/cumulative", "/api/recurring",
                 "/api/alerts", "/api/transactions", "/api/report"):
        r = client.get(path, headers=h)
        assert r.status_code == 200, path
        assert r.headers["x-robots-tag"] == "noindex, nofollow"


def test_healthz_public(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_lockout_after_failures(client):
    ip = {"CF-Connecting-IP": "203.0.113.9"}
    for _ in range(settings.dashboard_max_failures):
        client.get("/", headers={**basic("admin", "bad"), **ip})
    # même le bon mot de passe est refusé pendant le blocage
    assert client.get("/", headers={**basic("admin", "s3cret-pass"), **ip}).status_code == 429
    # une autre IP n'est pas bloquée
    assert client.get("/", headers={**basic("admin", "s3cret-pass"), "CF-Connecting-IP": "198.51.100.1"}).status_code == 200


def login(client, user, pwd, nxt="/"):
    return client.post("/login", data={"username": user, "password": pwd, "next": nxt})


def test_login_flow_sets_session_cookie(client):
    r = login(client, "admin", "s3cret-pass", "/?tab=x")
    assert r.status_code == 303 and r.headers["location"] == "/?tab=x"
    cookie = r.headers["set-cookie"]
    assert "bw_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    assert client.get("/", headers={"Accept": "text/html"}).status_code == 200  # cookie conservé par le client
    assert client.get("/api/me").json() == {"user": "admin", "role": "admin"}
    assert client.get("/setup", headers={"Accept": "text/html"}).status_code == 200


def test_login_wrong_password_redirects_with_error(client):
    r = login(client, "admin", "nope")
    assert r.status_code == 303 and "error=1" in r.headers["location"]
    assert "incorrect" in client.get("/login?error=1").text
    assert "bw_session" not in client.cookies


def test_login_open_redirect_blocked(client):
    r = login(client, "admin", "s3cret-pass", "https://evil.example/")
    assert r.headers["location"] == "/"
    r = login(client, "admin", "s3cret-pass", "//evil.example")
    assert r.headers["location"] == "/"


def test_logout_clears_cookie(client):
    login(client, "admin", "s3cret-pass")
    r = client.get("/logout")
    assert r.status_code == 303
    client.cookies.clear()
    assert client.get("/", headers={"Accept": "text/html"}).status_code == 303


def test_login_lockout(client):
    ip = {"CF-Connecting-IP": "203.0.113.50"}
    for _ in range(settings.dashboard_max_failures):
        client.post("/login", data={"username": "admin", "password": "bad"}, headers=ip)
    assert client.post("/login", data={"username": "admin", "password": "s3cret-pass"}, headers=ip).status_code == 429


def test_tampered_cookie_rejected(client):
    tok = session_mod.issue("admin", "admin")
    client.cookies.set("bw_session", tok[:-3] + "xyz")
    assert client.get("/api/me").status_code == 401
    assert session_mod.verify(session_mod.issue("n", "viewer"), now=session_mod.time.time() + session_mod.MAX_AGE + 1) is None


def test_viewer_session_role(client):
    from bankwatch import users

    pwd = users.add("lea")
    assert login(client, "Lea", pwd).status_code == 303
    assert client.get("/", headers={"Accept": "text/html"}).status_code == 200
    assert client.get("/setup", headers={"Accept": "text/html"}).status_code == 403
    assert client.get("/api/me").json()["role"] == "viewer"


def test_manifest_public(client):
    assert client.get("/static/manifest.json").json()["name"] == "BankWatch"


def test_no_password_blocks_remote(client, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_password", "")
    assert client.get("/", headers={"CF-Connecting-IP": "203.0.113.9"}).status_code == 403


def test_viewer_role(client):
    from bankwatch import users

    pwd = users.add("Lea")
    assert len(pwd) == 19 and pwd.count("-") == 3
    h = basic("lea", pwd)
    assert client.get("/", headers=h).status_code == 200
    assert client.get("/api/transactions", headers=h).status_code == 200
    assert client.get("/setup", headers=h).status_code == 403  # réservé admin
    assert client.get("/callback?code=x&state=y", headers=h).status_code == 403
    assert client.get("/", headers=basic("lea", "mauvais")).status_code == 401
    assert client.get("/setup", headers=basic("admin", "s3cret-pass")).status_code == 200  # admin ok


def test_users_file_hashed_and_private(client, tmp_path):
    from bankwatch import users

    pwd = users.add("lea")
    raw = users.users_path().read_text()
    assert pwd not in raw and "scrypt" not in raw
    assert (users.users_path().stat().st_mode & 0o777) == 0o600
    assert users.verify("lea", pwd) == "viewer"
    assert users.remove("lea") and users.verify("lea", pwd) is None


def test_cannot_shadow_admin(client):
    from bankwatch import users

    with pytest.raises(ValueError):
        users.add("admin")


def test_transactions_filters(client):
    h = basic("admin", "s3cret-pass")
    all_ = client.get("/api/transactions?days=400", headers=h).json()
    assert all_["count"] > 100 and "merchant_key" in all_["items"][0]
    deb = client.get("/api/transactions?days=400&direction=debit", headers=h).json()
    assert all(t["amount"] < 0 for t in deb["items"]) and deb["total"] < 0
    courses = client.get("/api/transactions?days=400&category=courses", headers=h).json()
    assert courses["count"] and all(t["category"] == "courses" for t in courses["items"])
    key = courses["items"][0]["merchant_key"]
    bym = client.get(f"/api/transactions?days=400&merchant_key={key}", headers=h).json()
    assert bym["count"] and all(t["merchant_key"] == key for t in bym["items"])
    ids = ",".join(t["id"] for t in all_["items"][:3])
    assert client.get(f"/api/transactions?ids={ids}", headers=h).json()["count"] == 3
    one_day = all_["items"][0]["date"]
    day = client.get(f"/api/transactions?since={one_day}&until={one_day}", headers=h).json()
    assert all(t["date"] == one_day for t in day["items"])


def test_transaction_detail(client):
    h = basic("admin", "s3cret-pass")
    first = client.get("/api/transactions?days=400&q=carrefour", headers=h).json()["items"][0]
    d = client.get("/api/transaction/" + first["id"], headers=h).json()
    assert d["id"] == first["id"] and d["account"]["bank"] == first["bank"]
    assert d["same_merchant"]["count"] > 5 and len(d["same_merchant"]["recent"]) <= 8
    assert isinstance(d["raw"], dict) and "first_seen" in d
    assert client.get("/api/transaction/nope", headers=h).status_code == 404
    # l'alerte doublon de la démo pointe vers ses transactions
    dup = next(a for a in client.get("/api/alerts", headers=h).json()["items"] if a["kind"] == "duplicate")
    import json as _json

    tx_id = _json.loads(dup["tx_ids"])[0]
    assert any(a["kind"] == "duplicate" for a in client.get("/api/transaction/" + tx_id, headers=h).json()["alerts"])


def test_cumulative_variable_spend_and_projection(client):
    h = basic("admin", "s3cret-pass")
    d = client.get("/api/cumulative", headers=h).json()
    assert len(d["days"]) in (28, 29, 30, 31) and len(d["current"]) == d["today"]
    assert d["projection"][d["today"] - 1] == d["current"][-1]  # l'estimation part du réel
    assert all(a <= b for a, b in zip(d["projection"][d["today"] - 1:], d["projection"][d["today"]:]))  # croissante
    assert d["end_estimate"] >= d["current"][-1]
    assert "logement" in d["excluded"] and "impots_taxes" in d["excluded"]


def test_overdraft_and_stats_endpoints(client):
    h = basic("admin", "s3cret-pass")
    assert client.get("/api/overdraft", headers=h).status_code == 200
    st = client.get("/api/alerts/stats", headers=h).json()
    assert st["total"] >= 1 and st["kinds"] and {"kind", "total", "dismissed", "high"} <= set(st["kinds"][0])


def test_sync_status_and_manual_sync_permissions(client, monkeypatch):
    from bankwatch import users

    h = basic("admin", "s3cret-pass")
    d = client.get("/api/sync", headers=h).json()
    assert d["accounts"] == [] and len(d["next_runs"]) == 3  # comptes DEMO exclus
    pwd = users.add("lea")
    assert client.post("/api/sync/acc-bourso", headers=basic("lea", pwd)).status_code == 403
    monkeypatch.setattr(settings, "provider", "enablebanking")
    assert client.post("/api/sync/inconnu", headers=h).status_code in (400, 404)  # pas de clé EB en test, ou compte inconnu
