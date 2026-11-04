"""Dashboard web : FastAPI + une page Chart.js (fichier static/index.html)."""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import base64
import hashlib
import threading
import time
from urllib.parse import quote

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from .config import settings
from .db import Database
from .pipeline import build_report
from .rules import recurring_merchants, spend_by_category, _prev_months, _month_start

app = FastAPI(title="BankWatch", docs_url=None, redoc_url=None, openapi_url=None)
STATIC = Path(__file__).parent / "static"

# ----------------------------------------------------------------- authentification
_failures: dict[str, list[float]] = defaultdict(list)
PUBLIC_PATHS = {"/healthz"}
SECURITY_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _client_ip(request: Request) -> str:
    # derrière Cloudflare Tunnel + proxy local : la vraie IP est dans CF-Connecting-IP
    return (request.headers.get("cf-connecting-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


ADMIN_PATHS = ("/setup", "/callback")  # réservé au rôle admin
_auth_cache: dict[str, tuple[str, float]] = {}  # empreinte de l'en-tête -> (rôle, expiration)


def _check_basic(header: str | None) -> str | None:
    """Retourne le rôle de l'utilisateur authentifié, ou None."""
    if not header or not header.lower().startswith("basic "):
        return None
    key = hashlib.sha256(header.encode()).hexdigest()
    hit = _auth_cache.get(key)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return None
    from .users import verify

    role = verify(user, pwd)
    if role:  # scrypt coûte ~50 ms : on garde le résultat 10 min en mémoire
        _auth_cache[key] = (role, time.time() + 600)
    return role


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "") and not request.url.path.startswith("/api/")


def _identify(request: Request) -> tuple[str, str] | None:
    """(user, role) via le cookie de session, sinon via HTTP Basic (curl, scripts)."""
    from . import session

    who = session.verify(request.cookies.get(session.COOKIE))
    if who:
        return who
    header = request.headers.get("authorization")
    role = _check_basic(header)
    if role:
        user = base64.b64decode(header[6:]).decode("utf-8", "replace").partition(":")[0]
        return user, role
    return None


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if not settings.dashboard_password:
        # pas de mot de passe configuré : seul l'accès local direct est autorisé
        local = request.client and request.client.host in ("127.0.0.1", "::1")
        forwarded = "cf-connecting-ip" in request.headers or "x-forwarded-for" in request.headers
        if not local or forwarded:
            return PlainTextResponse("BW_DASHBOARD_PASSWORD non configuré : accès distant refusé.", 403)
        response = await call_next(request)
    else:
        ip = _client_ip(request)
        window = settings.dashboard_lockout_minutes * 60
        now = time.time()
        _failures[ip] = [t for t in _failures[ip] if now - t < window]
        if len(_failures[ip]) >= settings.dashboard_max_failures:
            return PlainTextResponse("Trop de tentatives. Réessaie dans quelques minutes.", 429, headers=SECURITY_HEADERS)
        if path in ("/login", "/logout"):
            request.state.ip = ip
            response = await call_next(request)
        else:
            who = _identify(request)
            if not who:
                if request.headers.get("authorization") or request.cookies.get("bw_session"):
                    _failures[ip].append(now)
                if _wants_html(request):
                    nxt = quote(path + (("?" + request.url.query) if request.url.query else ""))
                    return RedirectResponse(f"/login?next={nxt}", 303, headers=SECURITY_HEADERS)
                return JSONResponse({"error": "authentification requise"}, 401, headers=SECURITY_HEADERS)
            _failures.pop(ip, None)
            request.state.user, request.state.role = who
            if who[1] != "admin" and path.startswith(ADMIN_PATHS):
                return PlainTextResponse("Accès réservé à l'administrateur.", 403, headers=SECURITY_HEADERS)
            response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


LOGIN_HTML = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>BankWatch · Connexion</title>
<link rel="manifest" href="/static/manifest.json"><meta name="theme-color" content="#1a1a19">
<style>
:root{color-scheme:light dark}
body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:#f9f9f7;color:#0b0b0b;display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}
.c{background:#fcfcfb;border:1px solid rgba(11,11,11,.1);border-radius:14px;padding:28px 24px;width:100%;max-width:360px;box-sizing:border-box}
h1{font-size:20px;margin:0 0 4px;font-weight:650}.sub{color:#52514e;font-size:13px;margin:0 0 20px}
label{display:block;font-size:12px;color:#52514e;margin:12px 0 4px}
input{width:100%;box-sizing:border-box;font:inherit;padding:10px 12px;border-radius:9px;border:1px solid rgba(11,11,11,.18);background:#fff;color:inherit}
button{width:100%;margin-top:18px;font:inherit;font-weight:600;padding:11px;border-radius:9px;border:0;background:#0b0b0b;color:#fff;cursor:pointer}
.err{background:#fdecec;color:#8a1c1c;border-radius:8px;padding:8px 12px;font-size:13px;margin-bottom:8px}
@media (prefers-color-scheme:dark){body{background:#0d0d0d;color:#f0efec}.c{background:#1a1a19;border-color:rgba(255,255,255,.1)}
input{background:#0d0d0d;border-color:rgba(255,255,255,.18)}button{background:#f0efec;color:#0b0b0b}.sub,label{color:#c3c2b7}.err{background:#3a1515;color:#f4b4b4}}
</style></head><body><form class="c" method="post" action="/login">
<h1>BankWatch</h1><p class="sub">Connexion au tableau de bord</p>
__ERROR__
<input type="hidden" name="next" value="__NEXT__">
<label for="u">Identifiant</label><input id="u" name="username" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" required autofocus>
<label for="p">Mot de passe</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Se connecter</button></form></body></html>"""


def _safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/"


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None, error: str | None = None):
    from . import session

    if session.verify(request.cookies.get(session.COOKIE)):
        return RedirectResponse(_safe_next(next), 303)
    import html as _html

    err = '<div class="err">Identifiant ou mot de passe incorrect.</div>' if error else ""
    return HTMLResponse(LOGIN_HTML.replace("__ERROR__", err).replace("__NEXT__", _html.escape(_safe_next(next))))


@app.post("/login")
async def login_submit(request: Request):
    from . import session
    from .users import verify as verify_user

    form = await request.form()
    user = str(form.get("username", "")).strip()
    pwd = str(form.get("password", ""))
    nxt = _safe_next(str(form.get("next", "/")))
    role = verify_user(user, pwd) if user and pwd else None
    if not role:
        _failures[getattr(request.state, "ip", "?")].append(time.time())
        return RedirectResponse(f"/login?error=1&next={quote(nxt)}", 303)
    _failures.pop(getattr(request.state, "ip", "?"), None)
    resp = RedirectResponse(nxt, 303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https" \
        or "cf-connecting-ip" in request.headers
    resp.set_cookie(session.COOKIE, session.issue(user.lower(), role), max_age=session.MAX_AGE,
                    httponly=True, secure=secure, samesite="lax", path="/")
    return resp


@app.get("/logout")
@app.post("/logout")
def logout():
    from . import session

    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(session.COOKIE, path="/")
    return resp


@app.get("/api/me")
def me(request: Request):
    return {"user": getattr(request.state, "user", None), "role": getattr(request.state, "role", None)}


@app.get("/static/manifest.json")
def manifest():
    icon = ("data:image/svg+xml;utf8," + quote(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#1a1a19"/>'
        '<path d="M14 42l10-12 8 8 18-20" fill="none" stroke="#2a78d6" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/></svg>'))
    return JSONResponse({"name": "BankWatch", "short_name": "BankWatch", "start_url": "/", "display": "standalone",
                         "background_color": "#1a1a19", "theme_color": "#1a1a19", "lang": "fr",
                         "icons": [{"src": icon, "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]},
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _page(title: str, body: str, ok: bool) -> HTMLResponse:
    color = "#0ca30c" if ok else "#d03b3b"
    html = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>BankWatch</title>
<style>body{{margin:0;font:15px/1.5 system-ui,sans-serif;background:#f9f9f7;color:#0b0b0b;display:grid;place-items:center;min-height:100vh;padding:16px}}
.c{{background:#fcfcfb;border:1px solid rgba(11,11,11,.1);border-radius:12px;padding:24px;max-width:440px}}
h1{{font-size:18px;margin:0 0 8px;color:{color}}}a{{color:#2a78d6}}
@media (prefers-color-scheme:dark){{body{{background:#0d0d0d;color:#f0efec}}.c{{background:#1a1a19;border-color:rgba(255,255,255,.1)}}}}</style>
</head><body><div class="c"><h1>{title}</h1>{body}<p><a href="/">Ouvrir le dashboard</a></p></div></body></html>"""
    return HTMLResponse(html, status_code=200 if ok else 400)


SETUP_HTML = """<p>Choisis le fichier <code>.pem</code> téléchargé à la création de l'application Enable Banking.
Il est envoyé uniquement au Pi (connexion chiffrée) et enregistré avec des droits 600.</p>
<input type="file" id="f" accept=".pem,application/x-pem-file,text/plain">
<p><button id="b" style="font:inherit;padding:8px 16px;border-radius:8px;border:1px solid #c3c2b7;cursor:pointer">Envoyer la clé</button></p>
<p id="r"></p>
<script>
document.getElementById('b').onclick = async () => {
  const f = document.getElementById('f').files[0], r = document.getElementById('r');
  if (!f) { r.textContent = 'Aucun fichier choisi.'; return; }
  if (f.size > 16384) { r.textContent = 'Fichier trop gros pour une clé.'; return; }
  const res = await fetch('/setup/key', { method: 'POST', headers: {'Content-Type': 'text/plain'}, body: await f.text() });
  r.textContent = (await res.json()).message;
};
</script>"""


def _key_path() -> Path:
    return Path(settings.eb_key_path)


@app.get("/setup", response_class=HTMLResponse)
def setup_page():
    if _key_path().exists():
        return _page("Clé déjà installée ✓", "<p>La clé Enable Banking est en place. Pour la remplacer, "
                     "supprime d'abord <code>data/enablebanking.pem</code> sur le Pi.</p>", True)
    return _page("Installer la clé Enable Banking", SETUP_HTML, True)


@app.post("/setup/key")
async def setup_key(request: Request):
    path = _key_path()
    if path.exists():
        return JSONResponse({"ok": False, "message": "Une clé est déjà installée."}, 409)
    body = await request.body()
    if len(body) > 16384:
        return JSONResponse({"ok": False, "message": "Fichier trop gros."}, 413)
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key = load_pem_private_key(body, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("clé non RSA")
    except Exception:
        return JSONResponse({"ok": False, "message": "Ce fichier n'est pas une clé privée RSA au format PEM."}, 400)
    path.parent.mkdir(parents=True, exist_ok=True)
    import os

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(body)
    return {"ok": True, "message": "Clé installée ✓ Tu peux fermer cette page."}


@app.get("/callback", response_class=HTMLResponse)
def callback(code: str | None = None, state: str | None = None, error: str | None = None,
             error_description: str | None = None):
    """Retour de la banque après consentement (Enable Banking)."""
    import html as _html

    if error or not code or not state:
        msg = _html.escape(error_description or error or "paramètres manquants")
        return _page("Liaison refusée ou interrompue", f"<p>{msg}</p><p>Relance <code>bankwatch link</code>.</p>", False)
    from .enablebanking import EnableBankingClient, EnableBankingError
    from .sync import eb_finalize

    try:
        bank, n = eb_finalize(_db(), EnableBankingClient(), code, state)
    except (EnableBankingError, ValueError) as e:
        return _page("Échec de la liaison", f"<p>{_html.escape(str(e))[:400]}</p>", False)
    return _page("Banque liée ✓", f"<p><strong>{_html.escape(bank)}</strong> : {n} compte(s) enregistré(s). "
                 "La première synchro se lance au prochain passage programmé, ou avec <code>bankwatch sync</code>.</p>", True)

EXCLUDE = ("epargne_virement",)
TOP_CATEGORIES = 7  # au-delà, regroupé dans « autre » (palette à 8 couleurs)


def _db() -> Database:
    return Database()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/summary")
def summary():
    db = _db()
    today = date.today()
    txs = db.transactions(since=today - timedelta(days=400))
    m_start = _month_start(today)
    cur = spend_by_category(txs, m_start, today, EXCLUDE)
    (ps, pe) = _prev_months(today, 1)[0]
    prev_same = spend_by_category(txs, ps, min(pe, ps + timedelta(days=today.day - 1)), EXCLUDE)
    prev_full = spend_by_category(txs, ps, pe, EXCLUDE)
    alerts = db.alerts(500)
    open_alerts = [a for a in alerts if a["created_at"][:10] >= (today - timedelta(days=30)).isoformat()]
    accounts = [dict(a) for a in db.accounts(include_ignored=False)]
    from .forecast import load_config as _fc

    card_banks = {cs["card_bank"] for cs in _fc().card_settlements}
    for a in accounts:
        a["is_card"] = a["bank"] in card_banks
    return {
        "today": today.isoformat(),
        "accounts": accounts,
        "total_balance": sum(a["balance"] or 0 for a in accounts if not a["is_card"]),
        "card_owed": round(sum(-(a["balance"] or 0) for a in accounts if a["is_card"] and (a["balance"] or 0) < 0), 2),
        "month_spend": sum(cur.values()),
        "prev_month_same_day": sum(prev_same.values()),
        "prev_month_total": sum(prev_full.values()),
        "month_income": sum(t.amount for t in txs if t.amount > 0 and t.booking_date >= m_start and t.category not in EXCLUDE),
        "alerts_30d": len(open_alerts),
        "alerts_high_30d": sum(1 for a in open_alerts if a["severity"] == "high"),
        "tx_count": len(txs),
    }


@app.get("/api/monthly")
def monthly(months: int = Query(6, ge=1, le=24)):
    """Dépenses par mois et par catégorie (top N + autre)."""
    db = _db()
    today = date.today()
    first = _month_start(today)
    for _ in range(months - 1):
        first = _month_start(first - timedelta(days=1))
    txs = db.transactions(since=first, debits_only=True)
    totals: dict[str, float] = defaultdict(float)
    by_month: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in txs:
        if t.category in EXCLUDE:
            continue
        key = f"{t.booking_date:%Y-%m}"
        by_month[key][t.category or "autre"] += abs(t.amount)
        totals[t.category or "autre"] += abs(t.amount)
    top = [c for c, _ in sorted(totals.items(), key=lambda kv: -kv[1])[:TOP_CATEGORIES]]
    labels = sorted(by_month)
    series = {c: [round(by_month[m].get(c, 0), 2) for m in labels] for c in top}
    other = [round(sum(v for c, v in by_month[m].items() if c not in top), 2) for m in labels]
    if any(other):
        series["autre (regroupé)"] = other
    return {"labels": labels, "series": series}


FIXED_CATEGORIES = ("epargne_virement", "impots_taxes", "logement", "assurances", "banque_frais")


@app.get("/api/cumulative")
def cumulative():
    """Rythme des dépenses COURANTES du mois (hors charges fixes et prélèvements récurrents),
    comparé au mois dernier et à la moyenne des 3 mois précédents, avec projection de fin de mois."""
    db = _db()
    today = date.today()
    txs = db.transactions(since=today - timedelta(days=400), debits_only=True)
    recurring_keys = set(recurring_merchants(txs))
    variable = [t for t in txs if t.category not in FIXED_CATEGORIES and t.merchant_key not in recurring_keys]
    m_start = _month_start(today)
    days_in_month = calendar.monthrange(today.year, today.month)[1]

    def daily(start: date, end: date) -> list[float]:
        n = calendar.monthrange(start.year, start.month)[1]
        acc = [0.0] * 31
        for t in variable:
            if start <= t.booking_date <= end:
                acc[t.booking_date.day - 1] += abs(t.amount)
        out, run = [], 0.0
        for v in acc[:n]:
            run += v
            out.append(round(run, 2))
        return out

    current = daily(m_start, today)[: today.day]
    prevs = [daily(s, e) for s, e in _prev_months(today, 3)]
    prevs = [p for p in prevs if p and p[-1] > 0]
    last_month = prevs[0] if prevs else []
    avg = [round(sum(p[min(i, len(p) - 1)] for p in prevs) / len(prevs), 2) for i in range(31)] if prevs else []
    # Projection : déjà dépensé + ce que les 3 mois précédents dépensaient en moyenne sur le reste du mois
    # (bien plus stable en début de mois qu'une extrapolation linéaire du rythme des premiers jours)
    spent = current[-1] if current else 0.0
    if avg:
        base = avg[min(today.day, len(avg)) - 1]
        proj = lambda d: round(spent + max(0.0, avg[min(d, len(avg)) - 1] - base), 2)
    else:
        pace = spent / today.day if today.day else 0.0
        proj = lambda d: round(pace * d, 2)
    projection = [None] * (today.day - 1) + [proj(d) for d in range(today.day, days_in_month + 1)] if current else []
    return {
        "days": list(range(1, days_in_month + 1)), "today": today.day,
        "current": current, "last_month": last_month, "average_prev3": avg, "projection": projection,
        "end_estimate": proj(days_in_month) if current else 0.0, "avg_end": avg[-1] if avg else None,
        "last_end": last_month[-1] if last_month else None,
        "excluded": list(FIXED_CATEGORIES) + ["prélèvements récurrents"],
    }


@app.get("/api/recurring")
def recurring():
    db = _db()
    today = date.today()
    txs = db.transactions(since=today - timedelta(days=400), debits_only=True)
    rec = recurring_merchants(txs)
    accounts = {a["id"]: a["bank"] for a in db.accounts()}
    out = []
    for key, items in rec.items():
        last = items[-1]
        prev_amounts = [abs(t.amount) for t in items[:-1][-6:]]
        ref = sorted(prev_amounts)[len(prev_amounts) // 2] if prev_amounts else abs(last.amount)
        gaps = [(b.booking_date - a.booking_date).days for a, b in zip(items, items[1:])]
        cadence = sorted(gaps)[len(gaps) // 2] if gaps else None
        next_due = (last.booking_date + timedelta(days=cadence)).isoformat() if cadence else None
        out.append({
            "merchant": last.counterparty or last.label,
            "merchant_key": key,
            "bank": accounts.get(last.account_id, "?"),
            "category": last.category,
            "last_amount": abs(last.amount),
            "usual_amount": ref,
            "delta_pct": round((abs(last.amount) - ref) / ref * 100, 1) if ref else 0,
            "last_date": last.booking_date.isoformat(),
            "cadence_days": cadence,
            "next_due": next_due,
            "count": len(items),
            "yearly": round(abs(last.amount) * (365 / cadence), 0) if cadence else None,
        })
    out.sort(key=lambda r: -r["last_amount"])
    return {"items": out, "monthly_total": round(sum(r["last_amount"] for r in out if r["cadence_days"] and r["cadence_days"] <= 40), 2)}


@app.get("/api/watch")
def watch():
    from .pipeline import _watch_dict, _watch_stats

    return {"items": [_watch_dict(s) for s in _watch_stats(_db(), date.today())]}


@app.get("/api/forecast")
def forecast():
    from .forecast import coverage_dicts, load_config

    cfg = load_config()
    return {"horizon_days": cfg.horizon_days, "margin": cfg.margin, "accounts": coverage_dicts(_db(), date.today())}


@app.get("/api/alerts")
def alerts(limit: int = Query(100, le=500)):
    return {"items": [dict(a) for a in _db().alerts(limit)]}


@app.get("/api/alerts/stats")
def alerts_stats(days: int = Query(30, ge=1, le=365)):
    db = _db()
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = db.conn.execute("SELECT kind, severity, dismissed, COUNT(*) n FROM alerts WHERE created_at >= ? GROUP BY 1,2,3", (since,)).fetchall()
    by_kind: dict[str, dict] = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], {"kind": r["kind"], "total": 0, "dismissed": 0, "high": 0, "medium": 0, "low": 0})
        k["total"] += r["n"]
        k[r["severity"]] += r["n"] if not r["dismissed"] else 0
        if r["dismissed"]:
            k["dismissed"] += r["n"]
    daily = db.conn.execute("SELECT substr(created_at,1,10) d, COUNT(*) n FROM alerts WHERE created_at >= ? AND dismissed=0 GROUP BY 1 ORDER BY 1", (since,)).fetchall()
    return {"days": days, "kinds": sorted(by_kind.values(), key=lambda k: -k["total"]),
            "daily": [{"date": r["d"], "n": r["n"]} for r in daily],
            "total": sum(k["total"] for k in by_kind.values()), "dismissed": sum(k["dismissed"] for k in by_kind.values())}


@app.get("/api/recommendations")
def recommendations():
    from .recommend import recommendation_dicts

    return recommendation_dicts(_db(), date.today())


@app.get("/api/overdraft")
def overdraft():
    from .overdraft import overdraft_dicts

    return {"items": overdraft_dicts(_db(), date.today())}


# ----------------------------------------------------------------- synchronisation
_sync_lock = threading.Lock()


def _next_runs(now: datetime) -> list[str]:
    out = []
    for hhmm in settings.sync_schedule.split(","):
        h, m = (int(x) for x in hhmm.strip().split(":"))
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        out.append(t.isoformat(timespec="minutes"))
    return sorted(out)


@app.get("/api/sync")
def sync_state():
    from .sync import sync_status

    return {"accounts": sync_status(_db()), "next_runs": _next_runs(datetime.now()),
            "running": _sync_lock.locked(), "provider": settings.provider}


@app.post("/api/sync/{account_id:path}")
def sync_one(account_id: str, request: Request):
    """Synchro manuelle d'un compte (admin). Consomme un accès du quota du jour."""
    if getattr(request.state, "role", None) != "admin":
        return JSONResponse({"error": "réservé à l'administrateur"}, 403)
    if settings.provider != "enablebanking":
        return JSONResponse({"error": "synchro manuelle disponible avec Enable Banking uniquement"}, 400)
    if not _sync_lock.acquire(blocking=False):
        return JSONResponse({"error": "une synchronisation est déjà en cours"}, 409)
    try:
        from .enablebanking import EnableBankingClient, EnableBankingError
        from .pipeline import run_checks
        from .sync import eb_sync_account, sync_status

        db = _db()
        acc = db.conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not acc:
            return JSONResponse({"error": "compte inconnu"}, 404)
        try:
            eb = EnableBankingClient()
        except EnableBankingError as e:
            return JSONResponse({"error": str(e)}, 400)
        r, new = eb_sync_account(db, eb, acc)
        alerts = run_checks(db, new, notify=True) if new else []
        st = next(s for s in sync_status(db) if s["account_id"] == account_id)
        return {"ok": r.error is None and not r.skipped, "new": r.new_count, "error": r.error, "skipped": r.skipped,
                "rate_limited": r.rate_limited, "alerts": len(alerts), "status": st}
    finally:
        _sync_lock.release()


def _tx_row(t, accounts: dict) -> dict:
    return {
        "id": t.id, "date": t.booking_date.isoformat(), "value_date": t.value_date.isoformat() if t.value_date else None,
        "bank": accounts.get(t.account_id, "?"), "account_id": t.account_id,
        "counterparty": t.counterparty, "label": t.label, "amount": t.amount, "currency": t.currency,
        "category": t.category, "status": t.status, "merchant_key": t.merchant_key,
    }


@app.get("/api/transactions")
def transactions(days: int = Query(30, ge=1, le=400), account: str | None = None, q: str | None = None,
                 since: date | None = None, until: date | None = None, category: str | None = None,
                 merchant_key: str | None = None, ids: str | None = None, watch: str | None = None,
                 direction: str | None = Query(None, pattern="^(debit|credit)$"),
                 include_ignored: bool = False, limit: int = Query(500, le=2000)):
    """Liste filtrable : sert au tableau et à toutes les fenêtres de détail du dashboard."""
    db = _db()
    today = date.today()
    id_set = {i for i in (ids or "").split(",") if i}
    if id_set:
        since = since or today - timedelta(days=400)
    txs = db.transactions(since=since or today - timedelta(days=days), until=until, account_id=account,
                          include_ignored=include_ignored or bool(id_set))
    accounts = {a["id"]: a["bank"] for a in db.accounts()}
    w = None
    if watch:
        from .watch import load_watches

        w = next((x for x in load_watches() if x.name == watch), None)
    rows = []
    for t in reversed(txs):
        if id_set and t.id not in id_set:
            continue
        if q and q.lower() not in f"{t.counterparty} {t.label}".lower():
            continue
        if category and (t.category or "autre") != category:
            continue
        if merchant_key and t.merchant_key != merchant_key:
            continue
        if direction == "debit" and t.amount >= 0 or direction == "credit" and t.amount < 0:
            continue
        if w and not w.matches(t):
            continue
        rows.append(_tx_row(t, accounts))
    total = round(sum(r["amount"] for r in rows), 2)
    return {"items": rows[:limit], "count": len(rows), "total": total}


@app.get("/api/transaction/{tx_id:path}")
def transaction(tx_id: str):
    db = _db()
    row = db.conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "introuvable"}, 404)
    t = db._row_to_tx(row)
    acc = db.conn.execute("SELECT * FROM accounts WHERE id=?", (t.account_id,)).fetchone()
    accounts = {t.account_id: acc["bank"] if acc else "?"}
    out = _tx_row(t, accounts)
    out["account"] = {"bank": acc["bank"], "name": acc["name"], "iban": acc["iban"]} if acc else None
    out["first_seen"] = row["first_seen"]
    out["raw"] = t.raw.get("_eb") or t.raw  # ce que la banque a envoyé
    # autres opérations du même marchand (contexte), 400 j
    same = [x for x in db.transactions(since=t.booking_date - timedelta(days=400), until=t.booking_date + timedelta(days=400),
                                       include_ignored=True)
            if x.merchant_key == t.merchant_key and x.id != t.id and t.merchant_key]
    out["same_merchant"] = {"count": len(same), "total": round(sum(x.amount for x in same), 2),
                            "recent": [_tx_row(x, accounts | {x.account_id: accounts.get(x.account_id, "?")}) for x in sorted(same, key=lambda x: x.booking_date, reverse=True)[:8]]}
    rel = db.conn.execute("SELECT id, kind, severity, title, created_at FROM alerts WHERE tx_ids LIKE ? AND dismissed=0 ORDER BY created_at DESC",
                          (f'%"{t.id}"%',)).fetchall()
    out["alerts"] = [dict(r) for r in rel]
    return out


@app.get("/api/report")
def report(days: int = Query(7, ge=1, le=90)):
    return JSONResponse(build_report(_db(), days), headers={"Cache-Control": "no-store"})
