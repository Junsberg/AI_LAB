"""Candle-replay exit simulator — pure, shared by the replay report and the paper runner.

Only price/volume exits are observable without a trade tape: hard stop, take-initial,
trailing stop, dead volume, plus a time horizon. Holder-drop, deployer-sell and KOL-sell
exits are *not simulated* and are reported as such, never silently treated as "no exit".

Fill conventions (conservative, and consistent with outcome rules v5):
  * prices come from candle bodies (open/close), never wicks — DEX wicks are glitches
  * entry = first candle starting at/after the entry time, at its body HIGH
  * a high only counts once the next candle's body confirms it (min of the two):
    09-28 lone 5-minute prints ~70x the real price made +3,400% "exits"
  * a closing mark (dead volume / horizon) is min(last close, median of the last
    three closes) — the price nobody traded after is not a price we could sell at
  * within one candle the adverse move is assumed first: stop checks run before
    take-profit, and the trailing stop uses the peak from *previous* candles
  * a stop fills at its level, or at the candle open when the candle opens through it
  * targets fill at the target level, never above it
  * no trades for `volume_dead_minutes` (a candle gap; GT omits empty intervals) →
    exit at the last traded close
  * costs: `cost_pct` is charged on the way in and again on the way out
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from memebot.config import ExitRules
from memebot.outcomes.rules import Candle

NOT_SIMULATED = ("holder_drop", "deployer_sell", "kol_sell")


@dataclass
class Trade:
    entry_ts: int | None
    entry_px: float | None
    exit_ts: int | None = None
    exit_reason: str = "no_entry"
    ret: float | None = None  # net return on cost, e.g. -0.53 or +1.2
    fills: list[tuple[int, str, float, float]] = field(default_factory=list)  # ts, reason, fraction, px


def _lo(c: Candle) -> float:
    return min(c.open, c.close)


def _hi(c: Candle) -> float:
    return max(c.open, c.close)


def simulate(
    candles: list[Candle],
    entry_ts: int,
    rules: ExitRules,
    hard_stop_pct: float,
    cost_pct: float,
    horizon_s: int,
    data_until_ts: int | None = None,
) -> Trade:
    """`data_until_ts`: when the candle series was fetched (default: the horizon end).
    A series that stops trading well before it counts as dead volume, not a horizon exit."""
    cs = [c for c in candles if c.volume_usd > 0 and c.open > 0 and c.close > 0]
    cs.sort(key=lambda c: c.ts)
    after = [c for c in cs if c.ts >= entry_ts]
    if not after:
        return Trade(None, None)
    first = after[0]
    entry = _hi(first)
    cost = cost_pct / 100
    t = Trade(first.ts, entry)
    remaining, proceeds = 1.0, 0.0
    recovered = False
    peak = entry
    stop_px = entry * (1 + hard_stop_pct / 100)
    take_px = entry * rules.take_initial_at_x
    end_ts = first.ts + horizon_s
    dead_s = rules.volume_dead_minutes * 60

    def sell(ts: int, reason: str, frac: float, px: float) -> None:
        nonlocal remaining, proceeds
        frac = min(frac, remaining)
        proceeds += frac * px * (1 - cost)
        remaining -= frac
        t.fills.append((ts, reason, round(frac, 4), px))

    def mark(i: int) -> float:
        closes = [c.close for c in after[max(0, i - 2) : i + 1]]
        return min(after[i].close, median(closes))

    last = 0
    for i in range(1, len(after)):
        c, prev = after[i], after[i - 1]
        if c.ts > end_ts:
            break
        if c.ts - prev.ts > dead_s:
            sell(prev.ts, "dead_volume", remaining, mark(i - 1))
            break
        lo = _lo(c)
        if lo <= stop_px:
            sell(c.ts, "hard_stop", remaining, min(stop_px, c.open))
            break
        if recovered:
            trail_px = peak * (1 - rules.trailing_stop_pct / 100)
            if lo <= trail_px:
                sell(c.ts, "trailing", remaining, min(trail_px, c.open))
                break
        confirmed = min(_hi(prev), _hi(c))
        if not recovered and confirmed >= take_px:
            sell(c.ts, "take_initial", rules.initial_recover_fraction, take_px)
            recovered = True
        peak = max(peak, confirmed)
        last = i
    if remaining > 1e-9:
        until = min(end_ts, data_until_ts if data_until_ts is not None else end_ts)
        dead = until - after[last].ts > dead_s
        sell(after[last].ts, "dead_volume" if dead else "horizon", remaining, mark(last))
    t.exit_ts, t.exit_reason = t.fills[-1][0], t.fills[-1][1]
    t.ret = round(proceeds / (entry * (1 + cost)) - 1, 4)
    return t
