import json
from datetime import date

from bankwatch.db import Tx
from bankwatch.watch import Watch, detect_watch, load_watches, stats

TODAY = date(2026, 11, 20)
W = Watch(name="saasco", label="Saasco", match=["saasco"], since=date(2026, 11, 3),
          small_charge_max=30, monthly_cap=250, note="Nouveau forfait", subscription_match=["pro sub"])


def tx(i, d, amount, who="SAASCO"):
    return Tx(id=f"a{i}", account_id="indy", booking_date=d, amount=amount, counterparty=who, label=who)


def history():
    h = []
    # octobre : abo 18 + 20 recharges ~10 €
    h.append(tx(0, date(2026, 10, 17), -18, "SAASCO* PRO SUB"))
    h += [tx(i, date(2026, 10, 1 + i), -10.5) for i in range(1, 21)]
    # septembre : abo + 10 recharges
    h.append(tx(100, date(2026, 9, 17), -18, "SAASCO* PRO SUB"))
    h += [tx(100 + i, date(2026, 9, 1 + i), -10.5) for i in range(1, 11)]
    # novembre : nouveau forfait, une seule charge
    h.append(tx(200, date(2026, 11, 4), -185, "SAASCO* PRO SUB"))
    # autre marchand, ne doit pas compter
    h.append(tx(300, date(2026, 11, 5), -10, "NETFLIX"))
    return h


def test_stats_split_subscription_vs_topups():
    s = stats(W, history(), TODAY)
    assert s.month_total == 185 and s.month_small == 0 and s.since_small_count == 0
    oct_ = next(m for m in s.months if m["month"] == "2026-10")
    assert oct_["big"] == 18 and oct_["small_count"] == 20 and oct_["total"] == 228
    assert s.prev_avg_same_day and s.prev_avg_same_day > 100


def test_no_alert_when_plan_holds():
    h = history()
    # sans montant attendu, le 1er débit du nouveau forfait (185 vs 18 avant) est signalé une fois comme changement d'abonnement
    assert [a.kind for a in detect_watch(h, [h[-2]], TODAY, [W])] == ["watch_subscription"]
    w = Watch(**{**W.__dict__, "expected_amount": 185})
    assert detect_watch(h, [h[-2]], TODAY, [w]) == []


def test_topup_after_since_raises_alert():
    h = history()
    extra = tx(400, date(2026, 11, 12), -10.43)
    alerts = detect_watch(h + [extra], [extra], TODAY, [W])
    kinds = [a.kind for a in alerts]
    assert "watch_extra" in kinds
    a = next(a for a in alerts if a.kind == "watch_extra")
    assert "1 recharge(s)" in a.message and a.fingerprint == "watch:extra:a400"


def test_topup_before_since_ignored():
    h = history()
    old = tx(401, date(2026, 11, 2), -10.43)
    assert [a.kind for a in detect_watch(h + [old], [old], TODAY, [W])] == []


def test_monthly_cap():
    h = history() + [tx(500 + i, date(2026, 11, 6 + i), -12) for i in range(6)]  # 185 + 72 = 257 > 250
    alerts = detect_watch(h, [], TODAY, [W])
    assert any(a.kind == "watch_cap" and a.severity == "high" for a in alerts)


def test_load_watches(tmp_path):
    p = tmp_path / "watch.json"
    p.write_text(json.dumps({"saasco": {"match": ["Saasco"], "since": "2026-11-03", "small_charge_max": 30}}))
    [w] = load_watches(p)
    assert w.match == ["saasco"] and w.since == date(2026, 11, 3) and w.monthly_cap is None
    assert load_watches(tmp_path / "absent.json") == []


def test_repo_watch_file_is_valid():
    ws = load_watches()  # le fichier rules/watch.json du dépôt (perso ou exemple) doit se charger
    assert ws and all(w.match and w.label for w in ws)


def test_subscription_amount_change_alert_and_generic_rules_skip():
    from bankwatch.rules import run_all
    from bankwatch.watch import claimed_ids

    w = Watch(name="saasco", label="Saasco", match=["saasco"], since=date(2026, 11, 3),
              small_charge_max=100, subscription_match=["pro sub"], expected_amount=170)
    h = history()
    ultra = tx(600, date(2026, 11, 3), -170, "SAASCO* PRO SUB")
    h.append(ultra)
    # 170 € = montant attendu : pas d'alerte abonnement, pas d'alerte « inhabituel » générique
    assert [a.kind for a in detect_watch(h, [ultra], TODAY, [w]) if a.kind != "watch_cap80"] == []
    generic = run_all(h, [ultra], TODAY, skip_ids=claimed_ids([ultra], [w]))
    assert not any(a.kind in ("unusual_amount", "recurring_amount", "new_merchant") for a in generic)
    # 199 € : écart > 5 % -> alerte abonnement
    other = tx(601, date(2026, 12, 3), -199, "SAASCO* PRO SUB")
    alerts = detect_watch(h + [other], [other], date(2026, 12, 10), [w])
    assert any(a.kind == "watch_subscription" and a.severity == "medium" for a in alerts)
