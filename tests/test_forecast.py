from datetime import date

from bankwatch.db import Database, Tx
from bankwatch.forecast import ForecastConfig, View, coverage, detect_forecast, next_due_day, upcoming, view_window
from bankwatch.pipeline import store_alerts

CFG = ForecastConfig(horizon_days=10, margin=50, card_settlements=[
    {"label": "Règlement American Express", "card_bank": "amex", "debit_bank": "boursobank",
     "match": ["american express"], "due_day": 30, "notice_days": 3}])


def setup(tmp_path, bourso_balance=121.54, amex_balance=-149.68):
    db = Database(tmp_path / "f.db")
    for acc, bank, name in (("b1", "boursobank", "Courant"), ("a1", "amex", "Carte"), ("s1", "sg", "SG")):
        db.upsert_account(id=acc, bank=bank, institution_id="X", requisition_id=None, iban=None, name=name, currency="EUR")
    db.set_balance("b1", bourso_balance, None)
    db.set_balance("a1", amex_balance, None)
    db.set_balance("s1", 2000, None)
    # règlement Amex déjà vu une fois sur bourso (sert à choisir le compte débité)
    db.upsert_transactions([Tx(id="t1", account_id="b1", booking_date=date(2026, 10, 30), amount=-474.46,
                               counterparty="American Express -France", label="PRLV", category="epargne_virement")])
    # loyer récurrent sur SG le 2 de chaque mois
    db.upsert_transactions([Tx(id=f"l{m}", account_id="s1", booking_date=date(2026, m, 2), amount=-1150,
                               counterparty="Loyer SCI", label="VIR LOYER", category="logement") for m in (8, 9, 10, 11)])
    return db


def test_next_due_day():
    assert next_due_day(date(2026, 11, 3), 30) == date(2026, 11, 30)
    assert next_due_day(date(2026, 11, 30), 30) == date(2026, 11, 30)
    assert next_due_day(date(2026, 12, 1), 30) == date(2026, 12, 30)
    assert next_due_day(date(2026, 2, 10), 30) == date(2026, 2, 28)


def test_upcoming_card_and_recurring(tmp_path):
    db = setup(tmp_path)
    ups = upcoming(db, date(2026, 11, 25), CFG)
    card = [u for u in ups if u.kind == "card"]
    assert len(card) == 1 and card[0].account_id == "b1" and card[0].amount == 149.68 and card[0].due == date(2026, 11, 30)
    rent = [u for u in ups if u.kind == "recurring"]
    assert rent and rent[0].account_id == "s1" and rent[0].due == date(2026, 12, 2) and rent[0].amount == 1150


def test_outside_horizon_only_card_flagged_beyond(tmp_path):
    db = setup(tmp_path)
    ups = upcoming(db, date(2026, 11, 3), CFG)  # 30/11 est à 27 jours
    assert [u.kind for u in ups] == ["card"] and ups[0].beyond  # le loyer (2/12) n'est pas listé, la carte oui


def test_shortfall_then_covered(tmp_path):
    db = setup(tmp_path)
    today = date(2026, 11, 22)  # J-8 : dans l'horizon, pas encore J-3
    cov = {c.bank: c for c in coverage(db, today, CFG)}
    assert "amex" not in cov  # la carte n'est pas un compte à couvrir
    b = cov["boursobank"]
    assert b.total == 149.68 and b.shortfall == round(149.68 + 50 - 121.54, 2) and not b.ok
    alerts = detect_forecast(db, today, CFG)
    kinds = [a.kind for a in alerts]
    assert kinds == ["forecast_short"] and "78,14 €" in alerts[0].title
    store_alerts(db, alerts)
    # relance le même jour : rien de nouveau (même empreinte)
    assert all(not db.add_alert(a.fingerprint, a.kind, a.severity, a.title, a.message) for a in detect_forecast(db, today, CFG))
    # J-3 toujours à découvert : point d'étape « il manque »
    a3 = detect_forecast(db, date(2026, 11, 27), CFG)
    assert any(a.kind == "forecast_card" and "il manque" in a.title for a in a3)
    # virement fait : couvert -> alerte « OK » une fois
    db.set_balance("b1", 400, None)
    ok = detect_forecast(db, date(2026, 11, 28), CFG)
    assert [a.kind for a in ok] == ["forecast_ok", "forecast_card"]
    assert "✓ OK" in ok[1].title


def test_sg_rent_short(tmp_path):
    db = setup(tmp_path)
    db.set_balance("s1", 500, None)
    db.set_balance("b1", 1000, None)
    alerts = [a for a in detect_forecast(db, date(2026, 11, 28), CFG) if a.kind == "forecast_short"]
    assert len(alerts) == 1 and "Société Générale" in alerts[0].title and "700,00 €" in alerts[0].title


def test_repo_config_loads():
    from bankwatch.forecast import load_config

    cfg = load_config()
    assert cfg.card_settlements and cfg.card_settlements[0]["card_bank"] == "amex"


def test_no_items_no_shortfall(tmp_path):
    db = setup(tmp_path)
    db.set_balance("s1", -125.81, None)
    cov = {c.bank: c for c in coverage(db, date(2026, 11, 3), CFG)}
    assert cov["sg"].items == [] and cov["sg"].shortfall == 0 and cov["sg"].ok


def test_card_beyond_horizon_shown_as_later_without_alert(tmp_path):
    db = setup(tmp_path)
    today = date(2026, 11, 3)  # Amex le 30/11 : hors fenêtre de 10 j
    cov = {c.bank: c for c in coverage(db, today, CFG)}
    b = cov["boursobank"]
    assert b.items == [] and len(b.later) == 1 and b.later[0].beyond and b.later[0].amount == 149.68
    assert b.ok and b.shortfall == 0
    assert b.projected_shortfall == round(149.68 + 50 - 121.54, 2)
    assert detect_forecast(db, today, CFG) == []  # pas d'alerte avant la fenêtre


def test_two_lines_same_day_do_not_loop_forever(tmp_path):
    """Deux prélèvements du même marchand le même jour chaque mois (ex. deux forfaits Bouygues) :
    écarts 0/30/0/30 → la cadence médiane valait 0 et la boucle de report ne finissait jamais."""
    db = setup(tmp_path)
    rows = []
    for i, m in enumerate((6, 7, 8, 9, 10)):
        for j, amt in enumerate((-20.0, -23.99)):
            rows.append(Tx(id=f"bt{i}{j}", account_id="s1", booking_date=date(2026, m, 19), amount=amt,
                           counterparty="Bouygues Telecom", label="PRLV BOUYGUES", category="abonnements"))
    db.upsert_transactions(rows)
    ups = upcoming(db, date(2026, 11, 8), ForecastConfig(horizon_days=14, margin=50, card_settlements=[]))
    bt = [u for u in ups if "Bouygues" in u.label]
    assert len(bt) == 1 and bt[0].due == date(2026, 11, 19) and bt[0].amount == 43.99


def test_short_alert_closes_when_covered_and_reopens_if_short_again(tmp_path):
    """Un « il manque X » ne reste pas actif une fois le virement fait ; il se rouvre si le manque revient."""
    from bankwatch.pipeline import close_settled

    db = setup(tmp_path)
    today = date(2026, 11, 22)

    def cycle(day):
        alerts = detect_forecast(db, day, CFG)
        new = store_alerts(db, alerts)
        close_settled(db, alerts)
        return [a.kind for a in new]

    def short_row():
        return db.conn.execute("SELECT * FROM alerts WHERE kind='forecast_short'").fetchone()

    assert cycle(today) == ["forecast_short"]
    assert short_row()["resolved_at"] is None and len(db.alerts(active_only=True)) == 1
    db.mark_sent([short_row()["id"]])
    # virement fait : l'alerte « il manque » se clôt toute seule, « couvert ✓ » est émise
    db.set_balance("b1", 400, None)
    assert cycle(today) == ["forecast_ok"]
    row = short_row()
    assert row["resolved_at"] and row["resolved_by"] == "auto" and row["cleared_at"]
    # « couvert ✓ » est une bonne nouvelle : notifiée sur Discord, mais jamais active (rien à valider)
    ok_row = db.conn.execute("SELECT * FROM alerts WHERE kind='forecast_ok'").fetchone()
    assert ok_row["resolved_by"] == "auto" and ok_row["sent_at"] is None
    assert [r["kind"] for r in db.unsent_alerts()] == ["forecast_ok"]
    assert db.alerts(active_only=True) == []
    assert len(db.alerts()) == 2  # les deux restent dans l'historique
    # même situation au passage suivant : rien ne bouge
    assert cycle(today) == []
    # le solde retombe le même mois : même empreinte, l'alerte est rouverte avec les chiffres du jour et renotifiée
    db.set_balance("b1", 100, None)
    assert cycle(today) == ["forecast_short"]
    row = short_row()
    assert row["resolved_at"] is None and row["cleared_at"] is None and row["sent_at"] is None
    assert "99,68 €" in row["title"]


def test_manual_ok_stays_while_situation_lasts_and_dismissed_never_returns(tmp_path):
    from bankwatch.pipeline import close_settled

    db = setup(tmp_path)
    today = date(2026, 11, 22)

    def cycle():
        alerts = detect_forecast(db, today, CFG)
        new = store_alerts(db, alerts)
        close_settled(db, alerts)
        return new

    cycle()
    aid = db.conn.execute("SELECT id FROM alerts WHERE kind='forecast_short'").fetchone()["id"]
    assert db.resolve_alert(aid, by="lea") and not db.resolve_alert(aid, by="lea")
    # « c'est bon » alors qu'il manque toujours de l'argent : l'alerte reste réglée, pas de renotification
    assert cycle() == [] and db.alerts(active_only=True) == []
    row = db.conn.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
    assert row["resolved_by"] == "lea" and row["cleared_at"] is None
    # rouverte à la main
    assert db.reopen_alert(aid) and len(db.alerts(active_only=True)) == 1
    # écartée comme fausse : ne renaît jamais, même si la situation disparaît puis revient
    db.dismiss_alert(aid)
    db.set_balance("b1", 400, None)
    cycle()
    db.set_balance("b1", 100, None)
    assert [a.kind for a in cycle()] == []
    assert db.conn.execute("SELECT dismissed FROM alerts WHERE id=?", (aid,)).fetchone()["dismissed"] == 1


VIEWS = [View("14j", "rolling", 14, True), View("mois", "month_end", 0, True),
         View("30j", "rolling", 30, False), View("mois+1", "next_month", 0, False)]
CFG_V = ForecastConfig(horizon_days=10, margin=50, card_settlements=CFG.card_settlements, views=VIEWS)


def test_view_windows_month_end_and_december_rollover():
    assert view_window(VIEWS[1], date(2026, 11, 14)) == (date(2026, 11, 14), date(2026, 11, 30))
    assert view_window(VIEWS[1], date(2026, 11, 30)) == (date(2026, 11, 30), date(2026, 11, 30))
    assert view_window(VIEWS[1], date(2028, 2, 10))[1] == date(2028, 2, 29)
    assert view_window(VIEWS[3], date(2026, 11, 14)) == (date(2026, 12, 1), date(2026, 12, 31))
    # décembre → janvier
    assert view_window(VIEWS[3], date(2026, 12, 15)) == (date(2027, 1, 1), date(2027, 1, 31))
    assert view_window(VIEWS[0], date(2026, 12, 25)) == (date(2026, 12, 25), date(2027, 1, 8))


def test_month_end_view_includes_card_and_next_month_finds_rent_in_january(tmp_path):
    db = setup(tmp_path)
    today = date(2026, 11, 14)
    month = [u for u in upcoming(db, today, CFG_V, VIEWS[1]) if not u.beyond]
    assert [u.kind for u in month] == ["card"]  # Amex le 30/11 ; loyer le 2/12 hors vue
    nxt = upcoming(db, today, CFG_V, VIEWS[3])
    assert any(u.kind == "recurring" and u.due == date(2026, 12, 2) for u in nxt)
    # depuis décembre, « mois+1 » = janvier : loyer le 2/01, règlement Amex le 30/01
    jan = upcoming(db, date(2026, 12, 15), CFG_V, VIEWS[3])
    assert {u.due for u in jan} == {date(2027, 1, 2), date(2027, 1, 30)}


def test_views_parsed_and_legacy_fallback(tmp_path):
    from bankwatch.forecast import load_config

    p = tmp_path / "f.json"
    p.write_text('{"horizon_days": 7, "views": [{"name": "x", "type": "month_end", "alerts": false}]}')
    cfg = load_config(p)
    assert [(v.name, v.type, v.alerts) for v in cfg.views] == [("x", "month_end", False)]
    p.write_text('{"horizon_days": 7}')
    legacy = load_config(p).active_views()
    assert len(legacy) == 1 and legacy[0].days == 7 and legacy[0].alerts and legacy[0].name == ""


def test_one_short_alert_per_account_across_views_and_silent_view(tmp_path):
    db = setup(tmp_path)
    today = date(2026, 11, 22)  # Amex 30/11 : dans « 14j » et « mois »
    alerts = [a for a in detect_forecast(db, today, CFG_V) if a.kind == "forecast_short"]
    # deux vues à alertes, même compte, même manque : une seule alerte (la première vue déclarée)
    assert len(alerts) == 1 and "(vue 14j)" in alerts[0].title
    # l'empreinte ne dépend pas de la vue : c'est celle du mode historique, l'alerte déjà émise continue
    legacy = [a for a in detect_forecast(db, today, CFG) if a.kind == "forecast_short"]
    assert alerts[0].fingerprint == legacy[0].fingerprint == "forecast:short:b1:2026-11"
    # la vue la plus exigeante l'emporte, quel que soit l'ordre : le loyer SG (2/12) n'est que dans « 14j »
    db.set_balance("s1", 500, None)
    rev = ForecastConfig(margin=50, card_settlements=CFG.card_settlements, views=[VIEWS[1], VIEWS[0]])
    by_bank = {a.title.split(" :")[0]: a for a in detect_forecast(db, today, rev) if a.kind == "forecast_short"}
    assert len(by_bank) == 2 and "(vue mois)" in by_bank["BoursoBank"].title
    assert "(vue 14j)" in by_bank["Société Générale"].title and "700,00 €" in by_bank["Société Générale"].title
    # une vue sans alerte ne crée rien, même avec un manque
    silent = ForecastConfig(margin=50, card_settlements=CFG.card_settlements, views=[View("30j", "rolling", 30, False)])
    assert detect_forecast(db, today, silent) == []
    assert any(not c.ok for c in coverage(db, today, silent, silent.views[0]))  # le manque reste visible


def test_short_title_gives_first_uncovered_debit_not_first_due(tmp_path):
    """« il manque X avant le … » : la date est celle du premier prélèvement que le solde ne couvre plus."""
    db = Database(tmp_path / "f.db")
    db.upsert_account(id="f1", bank="fortuneo", institution_id="X", requisition_id=None, iban=None, name="F", currency="EUR")
    db.set_balance("f1", 400, None)
    for m in (7, 8, 9, 10):  # mutuelle 100 € le 15, loyer 700 € le 26 : le solde couvre le 15, pas le 26
        db.upsert_transactions([
            Tx(id=f"m{m}", account_id="f1", booking_date=date(2026, m, 15), amount=-100,
               counterparty="Mutuelle Sante", label="PRLV MUTUELLE", category="sante"),
            Tx(id=f"r{m}", account_id="f1", booking_date=date(2026, m, 26), amount=-700,
               counterparty="Loyer SCI", label="PRLV LOYER", category="logement")])
    cfg = ForecastConfig(horizon_days=14, margin=50)
    today = date(2026, 11, 14)
    c = coverage(db, today, cfg)[0]
    assert c.shortfall == 450 and c.first_uncovered.due == date(2026, 11, 26)
    short = [x for x in detect_forecast(db, today, cfg) if x.kind == "forecast_short"]
    assert len(short) == 1 and "avant le 26/11" in short[0].title and "15/11" not in short[0].title
    db.set_balance("f1", 900, None)
    assert coverage(db, today, cfg)[0].first_uncovered is None


def test_active_short_alert_follows_todays_figures_without_renotifying(tmp_path):
    from bankwatch.pipeline import close_settled

    db = setup(tmp_path)
    today = date(2026, 11, 22)

    def cycle():
        alerts = detect_forecast(db, today, CFG)
        new = store_alerts(db, alerts)
        close_settled(db, alerts)
        return new

    def row():
        return db.conn.execute("SELECT * FROM alerts WHERE kind='forecast_short'").fetchone()

    assert len(cycle()) == 1 and "78,14 €" in row()["title"]
    db.mark_sent([row()["id"]])
    db.set_balance("b1", 100, None)  # le manque passe de 78,14 € à 99,68 € sans avoir disparu entre-temps
    assert cycle() == []  # pas une nouvelle alerte : rien n'est renvoyé sur Discord
    r = row()
    assert "99,68 €" in r["title"] and r["amount"] == 99.68 and r["sent_at"] and r["resolved_at"] is None
    assert db.unsent_alerts() == []


def test_views_dicts_for_dashboard(tmp_path):
    from bankwatch.forecast import views_dicts

    db = setup(tmp_path)
    vs = views_dicts(db, date(2026, 11, 22), CFG_V)
    assert [v["name"] for v in vs] == ["14j", "mois", "30j", "mois+1"]
    assert [v["default"] for v in vs] == [True, False, False, False]
    assert [v["alerts"] for v in vs] == [True, True, False, False]
    assert (vs[1]["start"], vs[1]["end"]) == ("2026-11-22", "2026-11-30")
    b = next(a for a in vs[0]["accounts"] if a["bank"] == "boursobank")
    assert not b["ok"] and b["uncovered_from"] == "2026-11-30"
    legacy = views_dicts(db, date(2026, 11, 22), CFG)
    assert len(legacy) == 1 and legacy[0]["name"] == "10 j" and legacy[0]["default"]


def test_recommendations_ignore_non_alert_views(tmp_path):
    from bankwatch.recommend import analyse

    db = setup(tmp_path)
    today = date(2026, 11, 3)  # Amex hors de « 14j » ; le loyer SG (2/12) n'est que dans la vue sans alerte
    db.set_balance("s1", 100, None)
    cfg_quiet = ForecastConfig(margin=50, card_settlements=CFG.card_settlements,
                               views=[View("14j", "rolling", 14, True), View("mois+1", "next_month", 0, False)])
    import bankwatch.recommend as rec

    rec.load_config = lambda: cfg_quiet
    try:
        needs, _, _ = analyse(db, today)
    finally:
        from bankwatch.forecast import load_config

        rec.load_config = load_config
    assert all(n.bank != "sg" for n in needs)


def test_event_alerts_are_not_closed_automatically(tmp_path):
    """Seules les alertes d'état se clôturent seules : un doublon reste actif tant qu'on n'a pas dit « c'est bon »."""
    from bankwatch.pipeline import close_settled

    db = setup(tmp_path)
    db.add_alert("dup:x:y", "duplicate", "high", "Paiement en doublon probable", "…")
    close_settled(db, [])
    assert [a["kind"] for a in db.alerts(active_only=True)] == ["duplicate"]
