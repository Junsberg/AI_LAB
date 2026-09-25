"""Rug / outcome classification — pure functions, unit-tested.

Definitions (v1). These drive cluster scores, so they are conservative:
  lp_pull       liquidity now < 10% of its peak AND price < 20% of peak
  price_collapse price now <= 5% of peak within the first 24h and never recovered
  none          otherwise (includes "slow bleed" — not a rug by this definition)
Deployer-dump and authority-abuse need trade/authority data; added in v2.

Price handling (rules v2, 2026-09-25): DEX candles carry glitches — a first-candle
open near zero and single-trade wicks thousands of times above the body — which
produced peak multiples in the billions and mass "price_collapse" verdicts. So:
  * only candles with volume > 0 and positive open/close count
  * base  = max(open, close) of the first valid candle (end-of-first-hour price is
            also the earliest our signals could realistically have bought)
  * peak  = max over candles of max(open, close) — bodies, never wicks
  * peak_multiple > MAX_SANE_MULTIPLE is treated as bad data (no_data), not a 1000x
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume_usd: float


MAX_SANE_MULTIPLE = 1000.0  # >1000x from graduation inside a week is a data error, not a runner


def _body_high(c: Candle) -> float:
    return max(c.open, c.close)


@dataclass(frozen=True)
class Outcome:
    peak_multiple: float | None
    peak_ts: int | None
    rugged: bool
    rug_reason: str
    drawdown_from_peak: float | None


def classify(
    candles: list[Candle],
    liquidity_now_usd: float | None,
    liquidity_peak_usd: float | None,
    price_now: float | None = None,
) -> Outcome:
    candles = [c for c in candles if c.volume_usd > 0 and c.open > 0 and c.close > 0]
    if not candles:
        return Outcome(None, None, False, "no_data", None)
    base = max(candles[0].open, candles[0].close)
    peak_c = max(candles, key=_body_high)
    peak = _body_high(peak_c)
    last = price_now if price_now is not None else candles[-1].close
    peak_multiple = peak / base
    if peak_multiple > MAX_SANE_MULTIPLE:
        return Outcome(None, None, False, "no_data", None)
    dd = 1 - (last / peak if peak else 0)

    reason = "none"
    rugged = False
    if liquidity_now_usd is not None and liquidity_peak_usd and liquidity_peak_usd > 0:
        if liquidity_now_usd < 0.10 * liquidity_peak_usd and dd >= 0.80:
            rugged, reason = True, "lp_pull"
    if not rugged and dd >= 0.95:
        rugged, reason = True, "price_collapse"
    return Outcome(round(peak_multiple, 4), peak_c.ts, rugged, reason, round(dd, 4))
