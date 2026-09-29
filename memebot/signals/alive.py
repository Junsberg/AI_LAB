"""Alive-after-launch signal (lineage_v1 entry trigger, 09-29).

Rules v4 showed ~half of graduated tokens stop trading within an hour and that the
launch-minute pump is out of reach. So the trigger asks one question at decision
time (launch + alive_decide_after_min): is this token still a live market?

  * trading in each of the last `alive_recent_candles` 5-minute slots
  * >= alive_min_volume_usd_30m traded over the last 30 minutes
  * last close >= alive_min_price_vs_peak x the confirmed peak so far (not collapsing)

Only candles that CLOSED before the decision time are used (no look-ahead).
Pure function; the paper runner and the replay report share it.
"""
from __future__ import annotations

from memebot.config import Thresholds
from memebot.outcomes.rules import Candle, confirmed_highs

SLOT_S = 300


def alive(candles: list[Candle], decide_ts: int, th: Thresholds) -> tuple[bool, str | None, dict]:
    """→ (enter?, reject reason, metrics for the signal snapshot)."""
    closed = [c for c in candles if c.volume_usd > 0 and c.open > 0 and c.close > 0
              and c.ts + SLOT_S <= decide_ts]
    closed.sort(key=lambda c: c.ts)
    if not closed:
        return False, "not_trading", {"candles": 0}
    last_slot = decide_ts // SLOT_S * SLOT_S - SLOT_S
    wanted = {last_slot - k * SLOT_S for k in range(th.alive_recent_candles)}
    have = {c.ts // SLOT_S * SLOT_S for c in closed}
    vol_30m = sum(c.volume_usd for c in closed if c.ts >= decide_ts - 1800)
    peak = max([max(closed[0].open, closed[0].close), *confirmed_highs(closed)])
    ratio = closed[-1].close / peak if peak else 0.0
    m = {"candles": len(closed), "recent_slots_traded": len(wanted & have),
         "vol_usd_30m": round(vol_30m, 2), "price_vs_peak": round(ratio, 4)}
    if not wanted <= have:
        return False, "not_trading", m
    if vol_30m < th.alive_min_volume_usd_30m:
        return False, "low_volume", m
    if ratio < th.alive_min_price_vs_peak:
        return False, "collapsed", m
    return True, None, m
