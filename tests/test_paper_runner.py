from datetime import timedelta

from memebot.config import load_params
from memebot.execution import paper_runner as pr

P = load_params()
ATTRS = {"reserve_in_usd": "20000", "quote_token_price_usd": "200", "base_token_price_usd": "0.001"}  # 50 SOL


def row(score=0.95, evaluated=12, top10=10.0, age_min=5, mint="M"):
    return {"mint": mint, "score": score, "evaluated": evaluated, "age": timedelta(minutes=age_min),
            "meta": {"risk": {"top10_pct": top10}} if top10 is not None else {}}


def test_liquidity_and_cost():
    assert pr.liquidity_sol(ATTRS) == 50.0
    assert pr.liquidity_sol({"reserve_in_usd": "1"}) is None
    assert round(pr.cost_pct(0.5, 49.5), 4) == pr.FEE_PCT + 1.0


def test_size_curve_is_capped_by_risk_block():
    assert pr.position_size(0.9, P) == round(P.risk.max_position_sol * 0.4, 4)
    assert pr.position_size(1.0, P) == P.risk.max_position_sol
    assert pr.position_size(5.0, P) == P.risk.max_position_sol  # never above the frozen cap


def test_decide_gates_in_order():
    assert pr.decide(row(), ATTRS, P, 0, 0.0) == ("enter", None)
    assert pr.decide(row(top10=None), ATTRS, P, 0, 0.0) == ("wait", "risk_pending")
    assert pr.decide(row(top10=None, age_min=90), ATTRS, P, 0, 0.0) == ("reject", "risk_unknown")
    assert pr.decide(row(top10=80), ATTRS, P, 0, 0.0) == ("reject", "concentrated")
    assert pr.decide(row(), {"reserve_in_usd": "3000", "quote_token_price_usd": "200"}, P, 0, 0.0) == (
        "reject", "low_liquidity")
    assert pr.decide(row(), {}, P, 0, 0.0) == ("reject", "liquidity_unknown")
    assert pr.decide(row(), ATTRS, P, P.risk.max_open_positions, 0.0) == ("skip", "max_positions")
    assert pr.decide(row(), ATTRS, P, 0, -P.risk.max_daily_loss_sol) == ("skip", "daily_loss_cap")


def test_not_selected_goes_to_control_deterministically():
    ctrl = next(f"m{i}" for i in range(1000) if pr.is_control(f"m{i}"))
    plain = next(f"m{i}" for i in range(1000) if not pr.is_control(f"m{i}"))
    assert pr.decide(row(score=0.5, mint=ctrl), ATTRS, P, 0, 0.0) == ("control", None)
    assert pr.decide(row(score=0.95, evaluated=3, mint=plain), ATTRS, P, 0, 0.0) == ("skip", "lineage_below")
    assert pr.decide(row(score=None, evaluated=0, mint=plain), ATTRS, P, 0, 0.0) == ("skip", "lineage_below")
    share = sum(pr.is_control(f"x{i}") for i in range(20000)) / 20000
    assert 0.04 < share < 0.06
