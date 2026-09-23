"""Survivor scan: tokens that already pumped, dumped ≥ X%, and are still community-held.

Skips the launch race entirely. Candidates get hours, not milliseconds.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SurvivorSnapshot:
    peak_mcap_usd: float
    mcap_usd: float
    holders_peak: int
    holders_now: int
    deployer_pct_now: float  # % of supply still held by deployer cluster
    top10_pct: float
    hours_since_peak: float
    buys_last_hour: int
    sells_last_hour: int


def survivor_score(s: SurvivorSnapshot) -> tuple[float, dict]:
    """0..1. Hard rejects return 0 with a reason in the snapshot."""
    snap = s.__dict__ | {}
    drawdown = 1 - (s.mcap_usd / s.peak_mcap_usd if s.peak_mcap_usd else 1)
    holder_retention = s.holders_now / s.holders_peak if s.holders_peak else 0
    snap.update(drawdown=drawdown, holder_retention=holder_retention)

    if drawdown < 0.7:
        snap["reject"] = "not_deep_enough"
        return 0.0, snap
    if s.deployer_pct_now > 5:
        snap["reject"] = "deployer_still_holds"
        return 0.0, snap
    if s.top10_pct > 40:
        snap["reject"] = "concentrated"
        return 0.0, snap
    if s.hours_since_peak < 6:
        snap["reject"] = "too_fresh"
        return 0.0, snap

    # community held & drawn down: retention dominates, flow-turn adds
    retention_term = min(1.0, holder_retention / 0.6)  # 60%+ retention = full credit
    flow = s.buys_last_hour - s.sells_last_hour
    flow_term = 0.5 + 0.5 * max(-1.0, min(1.0, flow / 20.0))
    score = 0.7 * retention_term + 0.3 * flow_term
    return round(score, 4), snap
