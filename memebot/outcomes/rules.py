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

Rules v4 (2026-09-27), after the lineage replay picked tokens that had already stopped:
  dead           not rugged, but no trade later than DEAD_AFTER_S after the first valid
                 candle — it never collapsed only because nobody traded it. Not clean.
  entry_multiple peak / price at the first candle >= ENTRY_DELAY_S after the first one:
                 the move a bot that sees the token one collect cycle late could catch.
                 None when nothing traded by then.

Rules v5 (2026-09-29), after single 5-minute candles printed ~70x the real price
right before trading stopped (paper control +410%, >100x outcomes 2.2%):
  * a high counts only when the NEXT candle's body also reached it
    (confirmed high = min(body_high[i], body_high[i+1])); a lone print is ignored
  * base / entry price stay the v2 body high of their candle; a peak is never below
    them (a token that only fell from its first candle has multiple 1.0)
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


MAX_SANE_MULTIPLE = 1000.0
DEAD_AFTER_S = 3600  # no trade after the first hour → dead
ENTRY_DELAY_S = 15 * 60  # collect cadence (10 min) + processing  # >1000x from graduation inside a week is a data error, not a runner


def _body_high(c: Candle) -> float:
    return max(c.open, c.close)


def confirmed_highs(candles: list[Candle]) -> list[float]:
    """confirmed[i] = min(body_high[i], body_high[i+1]); a single candle confirms itself."""
    bh = [_body_high(c) for c in candles]
    if len(bh) < 2:
        return bh
    return [min(bh[i], bh[i + 1]) for i in range(len(bh) - 1)]


@dataclass(frozen=True)
class Outcome:
    peak_multiple: float | None
    peak_ts: int | None
    rugged: bool
    rug_reason: str
    drawdown_from_peak: float | None
    entry_multiple: float | None = None


def classify(
    candles: list[Candle],
    liquidity_now_usd: float | None,
    liquidity_peak_usd: float | None,
    price_now: float | None = None,
) -> Outcome:
    candles = [c for c in candles if c.volume_usd > 0 and c.open > 0 and c.close > 0]
    if not candles:
        return Outcome(None, None, False, "no_data", None)
    base = _body_high(candles[0])
    conf = confirmed_highs(candles)
    peak_i = max(range(len(conf)), key=conf.__getitem__)
    peak_c, peak = candles[peak_i], conf[peak_i]
    if peak < base:
        peak_c, peak = candles[0], base
    last = price_now if price_now is not None else candles[-1].close
    peak_multiple = peak / base
    if peak_multiple > MAX_SANE_MULTIPLE:
        return Outcome(None, None, False, "no_data", None)
    dd = 1 - (last / peak if peak else 0)

    entry_i = next((i for i, c in enumerate(candles) if c.ts >= candles[0].ts + ENTRY_DELAY_S), None)
    entry_multiple = None
    if entry_i is not None:
        entry_px = _body_high(candles[entry_i])
        entry_multiple = round(max([entry_px, *conf[entry_i:]]) / entry_px, 4)

    reason = "none"
    rugged = False
    if liquidity_now_usd is not None and liquidity_peak_usd and liquidity_peak_usd > 0:
        if liquidity_now_usd < 0.10 * liquidity_peak_usd and dd >= 0.80:
            rugged, reason = True, "lp_pull"
    if not rugged and dd >= 0.95:
        rugged, reason = True, "price_collapse"
    if not rugged and candles[-1].ts < candles[0].ts + DEAD_AFTER_S:
        reason = "dead"
    return Outcome(round(peak_multiple, 4), peak_c.ts, rugged, reason, round(dd, 4), entry_multiple)
