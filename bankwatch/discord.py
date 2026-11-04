"""Envoi d'alertes et de rapports vers Discord (webhook)."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx

from .config import settings
from .rules import eur

BANKS = {"boursobank": "BoursoBank", "sg": "Société Générale", "indy": "Indy", "amex": "American Express"}


def _local(ts: str) -> str:
    """Horodatage UTC ISO -> heure locale du conteneur (TZ=Europe/Paris)."""
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%d/%m %H:%M")
    except ValueError:
        return ts[:16]

COLORS = {"high": 0xE53935, "medium": 0xFB8C00, "low": 0x546E7A, "info": 0x1E88E5, "ok": 0x43A047}
ICONS = {"high": "🚨", "medium": "⚠️", "low": "ℹ️"}


API = "https://discord.com/api/v10"


def _token_from_file(path: str) -> str:
    try:
        for line in open(path, encoding="utf-8"):
            k, _, v = line.strip().partition("=")
            if k == "DISCORD_BOT_TOKEN":
                return v.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return ""


class DiscordNotifier:
    """Envoie via le bot (token + salon) si configuré, sinon via webhook."""

    def __init__(self, webhook_url: str | None = None, username: str | None = None,
                 channel_id: str | None = None, bot_token: str | None = None) -> None:
        self.url = webhook_url if webhook_url is not None else settings.discord_webhook_url
        self.username = username or settings.discord_username
        self.channel_id = channel_id if channel_id is not None else settings.discord_channel_id
        token = bot_token if bot_token is not None else settings.discord_bot_token
        if not token and bot_token is None and settings.discord_token_file:
            token = _token_from_file(settings.discord_token_file)
        self.bot_token = token

    @property
    def mode(self) -> str | None:
        if self.channel_id and self.bot_token:
            return "bot"
        if self.url:
            return "webhook"
        return None

    @property
    def enabled(self) -> bool:
        return self.mode is not None

    def _post(self, payload: dict[str, Any]) -> None:
        if not self.enabled:
            print("[discord] non configuré, message ignoré :", payload.get("content", ""))
            return
        if self.mode == "bot":
            url = f"{API}/channels/{self.channel_id}/messages"
            headers = {"Authorization": f"Bot {self.bot_token}", "User-Agent": "BankWatch (karakuri.studio, 0.1)"}
        else:
            url, headers = self.url, {}
            payload.setdefault("username", self.username)
        for attempt in range(3):
            r = httpx.post(url, json=payload, headers=headers, timeout=20)
            if r.status_code == 429 and attempt < 2:  # rate limit : on attend ce que Discord demande
                time.sleep(float(r.json().get("retry_after", 1)) + 0.2)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Discord ({self.mode}) HTTP {r.status_code}: {r.text[:300]}")
            return

    def send_alerts(self, alerts: list[dict[str, Any]]) -> None:
        """alerts : lignes de la table alerts (dict-like). Groupées par 10 embeds max."""
        if not alerts:
            return
        embeds = []
        for a in alerts:
            sev = a["severity"]
            embeds.append(
                {
                    "title": f"{ICONS.get(sev, '')} {a['title']}",
                    "description": a["message"],
                    "color": COLORS.get(sev, COLORS["info"]),
                    "footer": {"text": f"{a['kind']} · {_local(a['created_at'])}"},
                }
            )
        for i in range(0, len(embeds), 10):
            chunk = embeds[i : i + 10]
            n_high = sum(1 for a in alerts[i : i + 10] if a["severity"] == "high")
            content = f"**BankWatch** — {len(chunk)} alerte(s)" + (f", dont {n_high} critique(s)" if n_high else "")
            self._post({"content": content, "embeds": chunk})

    def send_report(self, report: dict[str, Any]) -> None:
        """Rapport hebdo / mensuel : soldes, dépenses par catégorie, top marchands."""
        def bal(v: float) -> str:
            return ("-" if v < 0 else "") + eur(v)

        lines_acc = "\n".join(
            f"• {BANKS.get(a['bank'], a['bank'])} — {a['name']} : **{bal(a['balance'])}**"
            if a.get("balance") is not None else f"• {BANKS.get(a['bank'], a['bank'])} — {a['name']} : solde inconnu"
            for a in report["accounts"]
        ) or "aucun compte"
        cats = sorted(report["by_category"].items(), key=lambda kv: -kv[1])
        lines_cat = "\n".join(f"• {c} : {v:,.0f} €".replace(",", " ") for c, v in cats[:10]) or "—"
        tops = "\n".join(f"• {m} : {v:,.0f} €".replace(",", " ") for m, v in report["top_merchants"][:8]) or "—"
        delta = report.get("delta_vs_prev_pct")
        trend = "" if delta is None else (f" ({'+' if delta >= 0 else ''}{delta:.0f} % vs période précédente)")
        embed = {
            "title": f"📊 {report['title']}",
            "color": COLORS["info"],
            "fields": [
                {"name": "Soldes", "value": lines_acc, "inline": False},
                {"name": f"Dépenses : {report['total_spend']:,.0f} €{trend}".replace(",", " "), "value": lines_cat, "inline": True},
                {"name": "Top marchands", "value": tops, "inline": True},
                {"name": "Entrées", "value": f"{report['total_income']:,.0f} €".replace(",", " "), "inline": False},
            ],
            "footer": {"text": f"BankWatch · {datetime.now():%d/%m/%Y %H:%M}"},
        }
        for w in report.get("watch", []):
            trend = ""
            prev = [m for m in w["months"][:-1] if m["total"] > 0]
            if prev:
                trend = f" (mois dernier : {eur(prev[-1]['total'])})"
            small = (f"\n• Recharges ce mois : {w['month_small_count']} pour {eur(w['month_small'])}"
                     if w["month_small_count"] else "\n• Aucune recharge ce mois ✓")
            cap = f"\n• Plafond : {eur(w['monthly_cap'])}" if w.get("monthly_cap") else ""
            embed["fields"].append({"name": f"🔎 {w['label']}",
                                    "value": f"• Mois en cours : **{eur(w['month_total'])}**{trend}{small}{cap}",
                                    "inline": False})
        fc = [c for c in report.get("forecast", []) if c["items"]]
        if fc:
            lines = []
            for c in fc:
                st = "✓" if c["ok"] else f"⚠️ manque {eur(c['shortfall'])}"
                lines.append(f"• {BANKS.get(c['bank'], c['bank'])} : {eur(c['total'])} à venir, solde {eur(c['balance'])} — {st}")
                lines += [f"   · {i['label']} {'≈ ' if i['estimate'] else ''}{eur(i['amount'])} le {i['due'][8:10]}/{i['due'][5:7]}" for i in c["items"][:4]]
            embed["fields"].append({"name": "📅 Prélèvements à venir (10 j)", "value": "\n".join(lines)[:1024], "inline": False})
        if report.get("open_alerts"):
            embed["fields"].append({"name": "Alertes sur la période", "value": str(report["open_alerts"]), "inline": False})
        self._post({"embeds": [embed]})

    def send_text(self, text: str) -> None:
        self._post({"content": text})
