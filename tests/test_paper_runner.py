from datetime import timedelta

from memebot.config import load_params
from memebot.execution import paper_runner as pr
from memebot.outcomes.rules import Candle

P = load_params()
ATTRS = {"reserve_in_usd": "20000", "quote_token_price_usd": "200", "base_token_price_usd": "0.001"}  # 50 SOL
NOW = 3600 * 100


def row(score=0.6, tokens_total=5, top10=10.0, age_min=35, mint="M"):
    return {"mint": mint, "score": score, "tokens_total": tokens_total, "evaluated": tokens_total,
            "age": timedelta(minutes=age_min), "meta": {"risk": {"top10_pct": top10}} if top10 is not None else {}}


def live(n=6, v=1500.0):
    return [Candle(NOW - k * 300, 1.0, 1.1, 0.9, 1.05, v) for k in range(n, 0, -1)]


def test_liquidity_and_cost():
    assert pr.liquidity_sol(ATTRS) == 50.0
    assert pr.liquidity_sol({"reserve_in_usd": "1"}) is None
    assert round(pr.cost_pct(0.5, 49.5), 4) == pr.FEE_PCT + 1.0


def test_size_is_within_the_frozen_cap():
    assert 0 < pr.position_size(P) <= P.risk.max_position_sol


def d(r, a=ATTRS, cs=None, o=0, pnl=0.0):
    return pr.decide(r, a, live() if cs is None else cs, P, NOW, o, pnl)[:2]


def test_decide_order_gates_lineage_alive_caps():
    assert d(row()) == ("enter", None)
    assert d(row(top10=None)) == ("wait", "risk_pending")
    assert d(row(top10=None, age_min=90)) == ("reject", "risk_unknown")
    assert d(row(top10=80)) == ("reject", "concentrated")
    assert d(row(), {"reserve_in_usd": "3000", "quote_token_price_usd": "200"}) == ("reject", "low_liquidity")
    assert d(row(), {}) == ("reject", "liquidity_unknown")
    assert d(row(score=0.1)) == ("reject", "bad_lineage")
    assert d(row(tokens_total=1)) == ("reject", "first_launch")
    assert d(row(), cs=[]) == ("skip", "not_trading")
    assert d(row(), cs=live(v=10.0)) == ("skip", "low_volume")
    assert d(row(), o=P.risk.max_open_positions) == ("skip", "max_positions")
    assert d(row(), pnl=-P.risk.max_daily_loss_sol) == ("skip", "daily_loss_cap")


def test_unknown_cluster_is_not_excluded_but_first_launch_is():
    assert pr.lineage_excluded(row(score=None, tokens_total=3), P) is None
    assert pr.lineage_excluded(row(score=None, tokens_total=0), P) == "first_launch"


def test_control_is_deterministic_and_about_5pct():
    assert pr.is_control("abc") == pr.is_control("abc")
    share = sum(pr.is_control(f"x{i}") for i in range(20000)) / 20000
    assert 0.04 < share < 0.06
