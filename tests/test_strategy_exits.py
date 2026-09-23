from datetime import datetime, timedelta, timezone

from memebot.config import load_params
from memebot.execution.exits import PositionState, evaluate_exit
from memebot.strategy.scorer import Candidate, decide

P = load_params()
NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def cand(**kw):
    base = dict(mint="m", lineage=0.8, kol=0.8, survivor=0.0, narrative=0.0,
                liquidity_sol=50, top10_pct=20, bundle_pct=5)
    base.update(kw)
    return Candidate(**base)


def test_hard_gates_reject():
    assert decide(cand(liquidity_sol=1), P, 0, 0).reason == "low_liquidity"
    assert decide(cand(bundle_pct=50), P, 0, 0).reason == "bundled"
    assert decide(cand(lineage=0.1), P, 0, 0).reason == "bad_deployer_cluster"
    assert decide(cand(), P, P.risk.max_open_positions, 0).reason == "max_positions"
    assert decide(cand(), P, 0, -P.risk.max_daily_loss_sol).reason == "daily_loss_cap"


def test_enter_and_size_within_cap():
    d = decide(cand(), P, 0, 0)
    assert d.decision == "enter"
    assert 0 < d.size_sol <= P.risk.max_position_sol
    assert decide(cand(lineage=0.3, kol=0.2), P, 0, 0).decision == "skip"
    assert decide(cand(kol=0.0), P, 0, 0).reason == "no_alpha"
    # a bad-but-not-rejected cluster shrinks the score
    assert decide(cand(lineage=0.25), P, 0, 0).total < decide(cand(lineage=0.9), P, 0, 0).total


def pos(**kw):
    base = dict(entry_price=1.0, size_tokens=100, remaining_tokens=100, peak_price=1.0,
                initial_recovered=False, opened_at=NOW, last_buy_at=NOW,
                holders_peak=100, holders_now=100)
    base.update(kw)
    return PositionState(**base)


def test_exit_priority():
    r, hs = P.exits, P.risk.hard_stop_pct
    assert evaluate_exit(pos(), 0.4, NOW, r, hs).reason == "hard_stop"
    assert evaluate_exit(pos(deployer_sold=True), 1.5, NOW, r, hs).reason == "deployer_sell"
    assert evaluate_exit(pos(holders_now=60), 1.5, NOW, r, hs).reason == "holder_drop"
    assert evaluate_exit(pos(), 1.5, NOW + timedelta(hours=2), r, hs).reason == "dead_volume"
    a = evaluate_exit(pos(), 2.1, NOW, r, hs)
    assert a.reason == "take_initial" and a.sell_fraction == r.initial_recover_fraction
    assert evaluate_exit(pos(initial_recovered=True, peak_price=4.0), 2.5, NOW, r, hs).reason == "trailing"
    assert evaluate_exit(pos(initial_recovered=True, peak_price=4.0), 3.5, NOW, r, hs) is None
