"""Rug / outcome classification — pure functions, unit-tested.

Definitions (v1). These drive cluster scores, so they are conservative:
  lp_pull       liquidity now < 10% of its peak AND price < 20% of peak
  price_collapse price now <= 5% of peak within the first 24h and never recovered
  none          otherwise (includes "slow bleed" — not a rug by this definition)
Deployer-dump and authority-abuse need trade/authority data; added in v2.
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
    if not candles:
        return Outcome(None, None, False, "no_data", None)
    base = candles[0].open or candles[0].close
    if base <= 0:
        return Outcome(None, None, False, "no_data", None)
    peak_c = max(candles, key=lambda c: c.high)
    peak = peak_c.high
    last = price_now if price_now is not None else candles[-1].close
    peak_multiple = peak / base
    dd = 1 - (last / peak if peak else 0)

    reason = "none"
    rugged = False
    if liquidity_now_usd is not None and liquidity_peak_usd and liquidity_peak_usd > 0:
        if liquidity_now_usd < 0.10 * liquidity_peak_usd and dd >= 0.80:
            rugged, reason = True, "lp_pull"
    if not rugged and dd >= 0.95:
        rugged, reason = True, "price_collapse"
    return Outcome(round(peak_multiple, 4), peak_c.ts, rugged, reason, round(dd, 4))
