"""KOL reverse-map: find wallets that consistently buy BEFORE a call / volume spike.

No Telegram account needed. Two call sources:
  1. public channel pages scraped from https://t.me/s/<channel>  (signals/calls.py)
  2. on-chain "call moment" inferred from a buyer-count / volume spike

Given (mint, called_at) pairs and the trade tape, a wallet earns precursor credit for
each call it bought within `window_before` minutes ahead of. Credit is normalized by
how many calls it *could* have front-run, so spray-and-pray wallets are penalized.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Trade:
    wallet: str
    mint: str
    side: str
    ts: datetime
    sol_amount: float


@dataclass(frozen=True)
class Call:
    mint: str
    called_at: datetime


@dataclass
class PrecursorStats:
    hits: int = 0  # bought within window before a call
    buys: int = 0  # total distinct mints bought
    lead_minutes: list[float] | None = None

    @property
    def precision(self) -> float:
        return self.hits / self.buys if self.buys else 0.0


def precursor_wallets(
    trades: list[Trade],
    calls: list[Call],
    window_before: timedelta = timedelta(minutes=15),
    min_hits: int = 3,
    min_precision: float = 0.3,
) -> dict[str, PrecursorStats]:
    """Return wallets that look like they know calls are coming."""
    calls_by_mint: dict[str, list[datetime]] = defaultdict(list)
    for c in calls:
        calls_by_mint[c.mint].append(c.called_at)

    first_buy: dict[tuple[str, str], datetime] = {}
    for t in trades:
        if t.side != "buy":
            continue
        k = (t.wallet, t.mint)
        if k not in first_buy or t.ts < first_buy[k]:
            first_buy[k] = t.ts

    stats: dict[str, PrecursorStats] = defaultdict(lambda: PrecursorStats(lead_minutes=[]))
    for (wallet, mint), ts in first_buy.items():
        s = stats[wallet]
        s.buys += 1
        for called_at in calls_by_mint.get(mint, []):
            lead = called_at - ts
            if timedelta(0) < lead <= window_before:
                s.hits += 1
                s.lead_minutes.append(lead.total_seconds() / 60)  # type: ignore[union-attr]
                break

    return {
        w: s for w, s in stats.items() if s.hits >= min_hits and s.precision >= min_precision
    }


def kol_score(mint_buyers_recent: set[str], precursors: dict[str, PrecursorStats]) -> float:
    """Score a live candidate: fraction of precursor-weight present among recent buyers."""
    if not precursors:
        return 0.0
    present = [precursors[w].precision for w in mint_buyers_recent if w in precursors]
    if not present:
        return 0.0
    # diminishing returns: 1 strong precursor ≈ 0.6, 3 ≈ 0.9
    total = sum(present)
    return 1.0 - (1.0 / (1.0 + 1.5 * total))
