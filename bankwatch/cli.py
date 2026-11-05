"""CLI BankWatch (fournisseur open banking : Enable Banking par défaut).

  bankwatch institutions bourso            # nom exact de la banque chez le fournisseur
  bankwatch link boursobank "Boursorama"   # lien de consentement (retour sur /callback)
  bankwatch accounts                       # liste les comptes liés
  bankwatch sync                           # tire les transactions + contrôles + alertes
  bankwatch import releve.csv --bank indy  # import de relevé (banques non couvertes)
  bankwatch check / report / run / serve / demo / reset / notify-test
"""

from __future__ import annotations

import json
from datetime import date

import typer

from .config import settings
from .db import Database

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Surveillance de comptes bancaires.")


def _client():
    from .sync import provider_client

    return provider_client()


def _provider_ready() -> str | None:
    """Retourne un message d'erreur si le fournisseur n'est pas configuré."""
    if settings.provider == "gocardless":
        if not (settings.gc_secret_id and settings.gc_secret_key):
            return "GoCardless non configuré (BW_GC_SECRET_ID / BW_GC_SECRET_KEY)."
        return None
    if not settings.eb_app_id:
        return "Enable Banking non configuré (BW_EB_APP_ID)."
    from pathlib import Path

    if not Path(settings.eb_key_path).exists():
        return f"Clé Enable Banking absente ({settings.eb_key_path}) : dépose-la via /setup."
    return None


@app.command()
def institutions(query: str = typer.Argument("", help="Filtre sur le nom (ex: bourso, societe, swan)"),
                 country: str = typer.Option("FR")):
    """Liste les banques disponibles chez le fournisseur."""
    c = _client()
    if settings.provider == "gocardless":
        for i in c.find_institution(query) if query else c.institutions():
            typer.echo(f"{i.id:40} {i.name:45} hist={i.transaction_total_days}j")
        return
    for a in (c.find_aspsp(query, country) if query else c.aspsps(country)):
        psu = ",".join(a.get("psu_types", []))
        days = a.get("maximum_consent_validity")
        days = f" consentement max={int(days) // 86400}j" if isinstance(days, (int, float)) else ""
        typer.echo(f"{a['name']:45} {a.get('country', ''):3} {psu}{days}")


@app.command()
def link(bank: str = typer.Argument(..., help="Nom court pour toi : boursobank, sg, indy"),
         institution: str = typer.Argument(..., help="Nom exact (Enable Banking) ou ID (GoCardless)"),
         country: str = typer.Option("FR"),
         business: bool = typer.Option(False, help="Compte professionnel (psu_type=business)")):
    """Affiche le lien de consentement à ouvrir dans ton navigateur."""
    db, c = Database(), _client()
    if settings.provider == "gocardless":
        inst = next((i for i in c.institutions() if i.id == institution), None)
        if not inst:
            typer.secho(f"Institution {institution} introuvable", fg="red")
            raise typer.Exit(1)
        req = c.create_requisition(inst, reference=f"bankwatch-{bank}-{date.today():%Y%m%d}")
        db.save_requisition(req["id"], bank, inst.id, req.get("status"), req["link"])
        url = req["link"]
    else:
        auth = c.start_auth(institution, country, psu_type="business" if business else "personal")
        db.save_requisition(auth["state"], bank, institution, "PENDING", auth["url"])
        url = auth["url"]
    typer.secho(f"\nOuvre ce lien et valide l'accès ({institution}) :\n\n  {url}\n", fg="green")
    if settings.provider != "gocardless":
        typer.echo(f"Après validation, la banque te renvoie sur {settings.eb_redirect_url} qui enregistre les comptes.")


@app.command()
def accounts():
    """Liste les comptes liés (et les enregistre, pour GoCardless)."""
    db = Database()
    if settings.provider == "gocardless":
        from .sync import register_accounts

        try:
            typer.echo(f"{register_accounts(db, _client())} compte(s) enregistré(s)/mis à jour.")
        except Exception as e:
            typer.secho(f"(GoCardless indisponible : {e})", fg="yellow")
    for a in db.accounts():
        bal = f"{a['balance']:.2f} {a['currency']}" if a["balance"] is not None else "—"
        iban = (a["iban"] or "")[:4] + "…" + (a["iban"] or "")[-4:] if a["iban"] else ""
        flag = "  [ignoré]" if a["ignored"] else ""
        typer.echo(f"{a['id'][:8]}  {a['bank']:12} {(a['name'] or '')[:30]:30} {iban:12} "
                   f"solde={bal} sync={(a['last_sync'] or 'jamais')[:16]}{flag}")


@app.command()
def ignore(match: str = typer.Argument(..., help="Début de l'id (cf. `bankwatch accounts`) ou morceau du nom"),
           undo: bool = typer.Option(False, "--undo", help="Réintégrer le compte")):
    """Exclut un compte de l'analyse (ex : carte qui reprend les opérations du compte courant)."""
    db = Database()
    m = match.lower()
    hits = [a for a in db.accounts() if a["id"].lower().startswith(m) or m in (a["name"] or "").lower()]
    if len(hits) != 1:
        typer.secho(f"{len(hits)} compte(s) correspondent à « {match} » : précise.", fg="red")
        for a in hits:
            typer.echo(f"  {a['id'][:8]}  {a['bank']} {a['name']}")
        raise typer.Exit(1)
    db.set_ignored(hits[0]["id"], not undo)
    typer.echo(f"{hits[0]['bank']} « {hits[0]['name']} » {'réintégré' if undo else 'ignoré'}.")


@app.command()
def sync(lookback: int = typer.Option(None, help="Jours d'historique à retirer (défaut: auto)"),
         no_notify: bool = typer.Option(False, help="Ne pas envoyer sur Discord")):
    """Synchronise les transactions puis lance les contrôles."""
    from .pipeline import run_checks
    from .sync import sync_accounts

    if msg := _provider_ready():
        typer.secho(msg, fg="red")
        raise typer.Exit(1)
    db = Database()
    results, new = sync_accounts(db, lookback)
    for r in results:
        typer.echo(f"[{r.bank}] {r.account_id[:8]}… {_status(r)}")
    alerts = run_checks(db, new, notify=not no_notify)
    _print_alerts(alerts)
    _notify_subscribers(results, new)


@app.command("import")
def import_cmd(path: str = typer.Argument(..., help="Fichier CSV ou OFX exporté de l'espace client"),
               bank: str = typer.Option(..., help="Nom court : indy, boursobank, sg"),
               name: str = typer.Option(None, help="Nom du compte affiché"),
               no_notify: bool = typer.Option(False, help="Ne pas envoyer sur Discord")):
    """Importe un relevé puis lance les contrôles sur les opérations récentes."""
    from pathlib import Path as _P

    from .importer import import_file
    from .pipeline import run_checks

    db = Database()
    try:
        n_rows, n_new = import_file(db, _P(path), bank, name)
    except ValueError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1)
    typer.echo(f"{n_rows} ligne(s) lue(s), {n_new} nouvelle(s) transaction(s).")
    _print_alerts(run_checks(db, None, notify=not no_notify))


@app.command()
def check(no_notify: bool = typer.Option(False, help="Ne pas envoyer sur Discord")):
    """Relance les règles sur les transactions récentes (sans resynchroniser)."""
    from .pipeline import run_checks

    alerts = run_checks(Database(), None, notify=not no_notify)
    _print_alerts(alerts)


@app.command()
def report(days: int = typer.Option(7, help="Période en jours"),
           dry_run: bool = typer.Option(False, help="Afficher sans envoyer")):
    """Envoie le rapport périodique sur Discord."""
    from .pipeline import build_report, send_report

    db = Database()
    r = build_report(db, days) if dry_run else send_report(db, days)
    typer.echo(json.dumps({k: v for k, v in r.items() if k != "accounts"}, indent=2, ensure_ascii=False, default=str))


@app.command()
def run():
    """Tâche quotidienne (cron) : sync + contrôles + rapport hebdo le jour configuré."""
    from .pipeline import run_checks, send_report, should_send_weekly
    from .sync import sync_accounts

    if msg := _provider_ready():
        typer.echo(msg + " Rien à synchroniser.")
        return
    db = Database()
    if not [a for a in db.accounts() if a["institution_id"] not in ("DEMO", "CSV")]:
        typer.echo("Aucun compte lié : lance `bankwatch link`.")
        return
    results, new = sync_accounts(db)
    errors = [r for r in results if r.error and not r.rate_limited]  # un 429 n'est pas une perte d'accès
    for r in results:
        typer.echo(f"[{r.bank}] {r.account_id[:8]}… {_status(r)}")
    alerts = run_checks(db, new)
    _print_alerts(alerts)
    _notify_subscribers(results, new)
    if should_send_weekly():
        send_report(db, 7)
        typer.echo("Rapport hebdo envoyé.")
    if errors:
        from .discord import DiscordNotifier

        DiscordNotifier().send_text(
            "⚠️ BankWatch : erreur de synchronisation sur "
            + ", ".join(e.bank for e in errors)
            + ". Consentement expiré ? Relance `bankwatch link`."
        )
        raise typer.Exit(2)


@app.command()
def serve(host: str = typer.Option(None), port: int = typer.Option(None)):
    """Lance le dashboard web."""
    import uvicorn

    uvicorn.run("bankwatch.dashboard:app", host=host or settings.dashboard_host,
                port=port or settings.dashboard_port, reload=False)


@app.command()
def recategorize():
    """Réapplique les règles de catégorisation sur toute la base (après modif de rules/categories.json)."""
    from .sync import recategorize_all

    typer.echo(f"{recategorize_all(Database())} transaction(s) recatégorisée(s).")


user_app = typer.Typer(help="Accès au dashboard (en plus de l'admin du .env).", no_args_is_help=True)
app.add_typer(user_app, name="user")


@user_app.command("add")
def user_add(name: str, role: str = typer.Option("viewer", help="viewer (lecture seule) ou admin"),
             reset: bool = typer.Option(False, help="Régénérer le mot de passe d'un compte existant")):
    """Crée un accès et affiche son mot de passe (une seule fois)."""
    from . import users

    if name.strip().lower() in users.load() and not reset:
        typer.secho(f"« {name} » existe déjà (--reset pour changer le mot de passe).", fg="red")
        raise typer.Exit(1)
    try:
        pwd = users.add(name, role)
    except ValueError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1)
    typer.echo(f"Accès « {name.strip().lower()} » ({role}) — mot de passe : {pwd}")
    typer.echo("Il n'est stocké que haché : note-le maintenant.")


@user_app.command("list")
def user_list():
    from . import users

    typer.echo(f"{settings.dashboard_user:15} admin (.env)")
    for n, u in sorted(users.load().items()):
        typer.echo(f"{n:15} {u.get('role', 'viewer')}")


@user_app.command("remove")
def user_remove(name: str):
    from . import users

    typer.echo("Supprimé." if users.remove(name) else "Introuvable.")


token_app = typer.Typer(help="Jetons d'API pour les autres applications (lecture seule, Bearer).", no_args_is_help=True)
app.add_typer(token_app, name="token")


@token_app.command("create")
def token_create(name: str = typer.Argument(..., help="Nom de l'application (ex. budget)")):
    """Crée ou remplace le jeton d'une application et l'affiche (une seule fois)."""
    from . import tokens

    try:
        tok = tokens.create(name)
    except ValueError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1)
    typer.echo(f"Jeton « {name.strip().lower()} » : {tok}")
    typer.echo("Stocké haché uniquement : copie-le maintenant dans la configuration de l'application.")


@token_app.command("list")
def token_list():
    from . import tokens

    for n, t in sorted(tokens.list_tokens().items()):
        typer.echo(f"{n:15} créé le {t.get('created', '?')}")


@token_app.command("revoke")
def token_revoke(name: str):
    from . import tokens

    typer.echo("Révoqué." if tokens.revoke(name) else "Introuvable.")


@app.command("webhook-test")
def webhook_test():
    """Envoie un événement de test à chaque abonné de rules/webhooks.json."""
    from .webhooks import emit, load_subscribers

    subs = [{**s, "events": ["*"]} for s in load_subscribers()]  # le ping ignore le filtre d'événements
    if not subs:
        typer.echo("Aucun abonné dans rules/webhooks.json.")
        return
    for r in emit("ping", {"message": "test BankWatch"}, subs):
        typer.echo(f"{r['name']:15} {'OK' if r['ok'] else 'ÉCHEC'} {r.get('status') or ''} {r.get('error') or ''}")


def _notify_subscribers(results, new) -> None:
    """Prévient les applis abonnées (budget…) qu'une synchro vient d'avoir lieu. Jamais bloquant."""
    from .webhooks import emit_sync_completed, load_subscribers

    if not load_subscribers():
        return
    try:
        for r in emit_sync_completed(results, len(new)):
            if not r["ok"]:
                typer.secho(f"webhook {r['name']} : {r.get('error')}", fg="yellow")
    except Exception as e:  # noqa: BLE001 — un webhook cassé ne doit pas faire échouer la synchro
        typer.secho(f"webhooks : {e}", fg="yellow")


@app.command()
def alerts(limit: int = typer.Option(20), all: bool = typer.Option(False, "--all", help="Inclure les alertes écartées")):
    """Liste les dernières alertes avec leur numéro."""
    for a in Database().alerts(limit, include_dismissed=all):
        flag = " (écartée)" if a["dismissed"] else ""
        typer.echo(f"#{a['id']:<4} [{a['severity']:6}] {a['created_at'][:10]}  {a['title']}{flag}")
        typer.echo("      " + a["message"].replace("**", "").replace("\n", " / ")[:140])


@app.command()
def dedupe():
    """Nettoie les opérations « en attente » devenues comptabilisées sous une autre référence."""
    removed, dismissed = Database().dedupe_pending()
    typer.echo(f"{removed} doublon(s) en attente supprimé(s), {dismissed} alerte(s) écartée(s).")


@app.command()
def dismiss(alert_id: int = typer.Argument(..., help="Numéro affiché par `bankwatch alerts`")):
    """Écarte une fausse alerte : elle disparaît du dashboard et ne sera pas recréée."""
    typer.echo("Alerte écartée." if Database().dismiss_alert(alert_id) else "Numéro inconnu.")


@app.command()
def forecast():
    """Prélèvements à venir et couverture par le solde de chaque compte."""
    from .forecast import coverage, load_config

    cfg = load_config()
    typer.echo(f"Horizon {cfg.horizon_days} j, marge {cfg.margin:.0f} €")
    for c in coverage(Database(), date.today(), cfg):
        st = "OK" if c.ok else f"IL MANQUE {c.shortfall:.2f} €"
        typer.secho(f"\n{c.bank:12} {c.name[:28]:28} solde {c.balance:9.2f} €  à venir {c.total:8.2f} €  → {st}",
                    fg="green" if c.ok else "red")
        for i in c.items:
            typer.echo(f"    {i.due:%d/%m}  {'≈' if i.estimate else ' '}{i.amount:8.2f} €  {i.label}")
        for i in c.later:
            typer.echo(f"    {i.due:%d/%m}  ≈{i.amount:8.2f} €  {i.label}  (hors fenêtre, à prévoir"
                       + (f" : il manquera {c.projected_shortfall:.2f} € au solde actuel)" if c.projected_shortfall else ")"))


@app.command()
def watch():
    """Affiche le suivi des marchands déclarés dans rules/watch.json."""
    from .pipeline import _watch_stats

    stats = _watch_stats(Database(), date.today())
    if not stats:
        typer.echo("Aucun marchand suivi (rules/watch.json).")
    for s in stats:
        typer.secho(f"\n{s.label}", bold=True)
        if s.note:
            typer.echo(f"  {s.note}")
        for m in s.months:
            bar = "█" * int(m["total"] / 10)
            typer.echo(f"  {m['month']}  {m['total']:8.2f} €  ({m['small_count']} recharge(s) = {m['small']:.2f} €)  {bar}")
        if s.since:
            typer.echo(f"  Depuis le {s.since:%d/%m/%Y} : {s.since_small_count} recharge(s), {s.since_small_total:.2f} €")


@app.command("notify-test")
def notify_test():
    """Envoie un message de test sur Discord (vérifie la config bot/webhook)."""
    from .discord import COLORS, DiscordNotifier

    n = DiscordNotifier()
    if not n.enabled:
        typer.secho("Discord non configuré (BW_DISCORD_CHANNEL_ID + token, ou BW_DISCORD_WEBHOOK_URL).", fg="red")
        raise typer.Exit(1)
    n._post({"embeds": [{"title": "✅ BankWatch connecté",
                         "description": "Les alertes bancaires arriveront dans ce salon.",
                         "color": COLORS["ok"]}]})
    typer.echo(f"Message envoyé (mode {n.mode}).")


@app.command()
def reset(yes: bool = typer.Option(False, "--yes", help="Confirmer sans question"),
          keep_links: bool = typer.Option(True, help="Garder les réquisitions (banques liées)")):
    """Vide transactions, alertes et comptes (ex: après la démo, avant les vraies banques)."""
    if not yes:
        typer.confirm("Effacer toutes les transactions et alertes ?", abort=True)
    db = Database()
    tables = ["transactions", "alerts", "accounts", "meta"] + ([] if keep_links else ["requisitions"])
    for t in tables:
        db.conn.execute(f"DELETE FROM {t}")
    db.conn.execute("DELETE FROM requisitions WHERE institution_id = 'DEMO'")
    db.conn.commit()
    typer.echo("Base vidée. Lance `bankwatch accounts` puis `bankwatch sync`.")


@app.command()
def demo(reset: bool = typer.Option(True, help="Vider la base avant"),
         notify: bool = typer.Option(False, help="Envoyer les alertes sur Discord")):
    """Charge 6 mois de transactions synthétiques (3 banques) avec des anomalies pour tester."""
    from .demo import load_demo

    n_tx, alerts = load_demo(Database(), reset=reset, notify=notify)
    typer.echo(f"{n_tx} transactions chargées.")
    _print_alerts(alerts)


def _status(r) -> str:
    if r.skipped:
        return f"sauté : {r.skipped}"
    if r.error:
        return ("QUOTA BANQUE atteint (429), réessai demain" if r.rate_limited else f"ERREUR {r.error}")
    return f"{r.new_count} nouvelle(s)"


def _print_alerts(alerts) -> None:
    if not alerts:
        typer.echo("Aucune nouvelle alerte.")
        return
    typer.echo(f"\n{len(alerts)} alerte(s) :")
    for a in alerts:
        color = {"high": "red", "medium": "yellow", "low": "white"}[a.severity]
        typer.secho(f"  [{a.severity.upper():6}] {a.title}", fg=color)
        for line in a.message.replace("**", "").splitlines():
            typer.echo(f"           {line}")


if __name__ == "__main__":
    app()
