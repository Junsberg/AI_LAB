"""Pure calculations: EMA, fair value gaps, CVD accumulation."""

from __future__ import annotations

from typing import NamedTuple

from .sources import Candle


class Fvg(NamedTuple):
    kind: str  # "bull" | "bear"
    tf: str
    t: int  # open time of the displacement (middle) candle, ms
    lo: float  # unfilled remainder of the gap
    hi: float


def ema(values: list[float], n: int) -> list[float | None]:
    """EMA seeded with the SMA of the first ``n`` values; ``None`` until then."""
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    k = 2 / (n + 1)
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def find_fvgs(
    candles: list[Candle], tf: str, tf_ms: int, now_ms: int, min_gap_pct: float = 0.0005
) -> list[Fvg]:
    """Unfilled three-candle fair value gaps.

    A gap forms from closed candles only (a still-forming bar can repaint). Later candles,
    including the forming one, trim the gap with their wicks; a wick through the far edge
    fills it and drops it.
    """
    zones: list[Fvg] = []
    for i in range(2, len(candles)):
        if candles[i].t + tf_ms > now_ms:
            break
        a, mid, c = candles[i - 2], candles[i - 1], candles[i]
        if c.low > a.high:
            kind, lo, hi = "bull", a.high, c.low
        elif c.high < a.low:
            kind, lo, hi = "bear", c.high, a.low
        else:
            continue
        if (hi - lo) / c.close < min_gap_pct:
            continue
        for later in candles[i + 1 :]:
            if kind == "bull":
                if later.low <= lo:
                    break
                hi = min(hi, later.low)
            else:
                if later.high >= hi:
                    break
                lo = max(lo, later.high)
        else:
            zones.append(Fvg(kind, tf, mid.t, lo, hi))
    return zones


def nearest(zones: list[Fvg], price: float, n: int) -> list[Fvg]:
    """The ``n`` zones closest to ``price`` (distance to the nearer edge)."""

    def dist(z: Fvg) -> float:
        return 0.0 if z.lo <= price <= z.hi else min(abs(z.lo - price), abs(z.hi - price))

    return sorted(sorted(zones, key=dist)[:n], key=lambda z: z.t)


def cumulate(deltas: dict[int, float], hours: list[int]) -> list[float | None]:
    """Running sum over ``hours``; ``None`` before the first hour that has data."""
    out: list[float | None] = []
    total = 0.0
    started = False
    for h in hours:
        if h in deltas:
            started = True
            total += deltas[h]
        out.append(total if started else None)
    return out


def align(series: dict[int, float], hours: list[int]) -> list[float | None]:
    """Values of ``series`` at each of ``hours``; ``None`` where the source has no bar."""
    return [series.get(h) for h in hours]


def premium_pct(price: dict[int, float], ref: dict[int, float], hours: list[int]) -> list[float | None]:
    """``(price / ref - 1) * 100`` per hour; ``None`` unless both sources have the hour."""
    return [
        (price[h] / ref[h] - 1) * 100 if h in price and ref.get(h) else None for h in hours
    ]
