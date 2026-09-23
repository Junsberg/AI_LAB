"""Combine signal scores into one decision. Deterministic; no LLM in the loop."""
from __future__ import annotations

from dataclasses import dataclass, field

from memebot.config import Params


@dataclass
class Candidate:
    mint: str
    lineage: float | None  # None = deployer unknown (fresh, no history)
    kol: float
    survivor: float
    narrative: float
    liquidity_sol: float
    top10_pct: float
    bundle_pct: float
    snapshot: dict = field(default_factory=dict)


@dataclass
class Decision:
    decision: str  # enter | skip | reject
    total: float
    reason: str | None = None
    size_sol: float = 0.0


def decide(c: Candidate, p: Params, open_positions: int, daily_pnl_sol: float) -> Decision:
    t, w, r = p.thresholds, p.weights, p.risk

    # hard gates
    if c.liquidity_sol < t.min_liquidity_sol:
        return Decision("reject", 0.0, "low_liquidity")
    if c.top10_pct > t.max_top10_holder_pct:
        return Decision("reject", 0.0, "concentrated")
    if c.bundle_pct > t.max_bundle_pct:
        return Decision("reject", 0.0, "bundled")
    if c.lineage is not None and c.lineage < t.lineage_reject_below:
        return Decision("reject", 0.0, "bad_deployer_cluster")
    if open_positions >= r.max_open_positions:
        return Decision("skip", 0.0, "max_positions")
    if daily_pnl_sol <= -r.max_daily_loss_sol:
        return Decision("skip", 0.0, "daily_loss_cap")

    # Alpha signals: only those that fired (>0) count, so an absent signal does not
    # dilute a strong one. Lineage is a multiplier, not an alpha: it can only shrink.
    alpha = [
        (w.kol_precursor, c.kol),
        (w.survivor, c.survivor),
        (w.narrative, c.narrative),
    ]
    present = [(wt, v) for wt, v in alpha if v > 0 and wt > 0]
    if not present:
        return Decision("skip", 0.0, "no_alpha")
    alpha_score = sum(wt * v for wt, v in present) / sum(wt for wt, _ in present)
    lineage = 0.5 if c.lineage is None else c.lineage  # unknown deployer → neutral
    lineage_mult = 1.0 - w.lineage * 0.5 * (1.0 - lineage)  # w=1: 0.5..1.0
    total = alpha_score * lineage_mult

    if total < t.enter_score:
        return Decision("skip", round(total, 4), "below_threshold")

    # size: scale linearly from threshold→1.0 into 40%→100% of max position
    frac = 0.4 + 0.6 * (total - t.enter_score) / max(1e-9, 1.0 - t.enter_score)
    return Decision("enter", round(total, 4), None, round(r.max_position_sol * frac, 4))
