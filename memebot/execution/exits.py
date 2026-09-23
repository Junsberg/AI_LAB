"""Exit engine — fixed rules only. Evidence (231k-turn Hyperliquid study, 2026):
fixed exit brackets beat every LLM discretionary exit. The LLM tunes these numbers
offline via hypotheses; it never decides an exit live.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from memebot.config import ExitRules


@dataclass
class PositionState:
    entry_price: float
    size_tokens: float
    remaining_tokens: float
    peak_price: float
    initial_recovered: bool
    opened_at: datetime
    last_buy_at: datetime
    holders_peak: int
    holders_now: int
    deployer_sold: bool = False
    kol_sold: bool = False


@dataclass(frozen=True)
class ExitAction:
    reason: str
    sell_fraction: float  # of remaining tokens


def evaluate_exit(
    s: PositionState, price: float, now: datetime, rules: ExitRules, hard_stop_pct: float
) -> ExitAction | None:
    ret_pct = (price / s.entry_price - 1) * 100

    # 1. hard stop always wins
    if ret_pct <= hard_stop_pct:
        return ExitAction("hard_stop", 1.0)

    # 2. adversarial flow → out entirely
    if rules.deployer_sell_exit and s.deployer_sold:
        return ExitAction("deployer_sell", 1.0)
    if rules.kol_sell_exit and s.kol_sold:
        return ExitAction("kol_sell", 1.0)

    # 3. structural decay
    if s.holders_peak and s.holders_now < s.holders_peak * (1 - rules.holder_drop_pct / 100):
        return ExitAction("holder_drop", 1.0)
    if now - s.last_buy_at > timedelta(minutes=rules.volume_dead_minutes):
        return ExitAction("dead_volume", 1.0)

    # 4. take initial at Nx — recover cost, ride the rest for free
    if not s.initial_recovered and price >= s.entry_price * rules.take_initial_at_x:
        return ExitAction("take_initial", rules.initial_recover_fraction)

    # 5. trailing stop only once we're playing with house money
    if s.initial_recovered and s.peak_price > 0:
        if price <= s.peak_price * (1 - rules.trailing_stop_pct / 100):
            return ExitAction("trailing", 1.0)

    return None
