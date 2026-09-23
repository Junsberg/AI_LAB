"""Paper broker: fills at observed price with a fixed slippage/fee haircut.
Same interface as the live broker so strategy code does not branch on mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Fill:
    side: str
    sol_amount: float
    token_amount: float
    price: float
    ts: datetime
    sig: str | None = None


class PaperBroker:
    def __init__(self, slippage_pct: float = 2.0, fee_pct: float = 1.0) -> None:
        self.haircut = 1 + (slippage_pct + fee_pct) / 100

    async def buy(self, mint: str, sol_amount: float, price: float) -> Fill:
        eff = price * self.haircut
        return Fill("buy", sol_amount, sol_amount / eff, eff, datetime.now(timezone.utc))

    async def sell(self, mint: str, token_amount: float, price: float) -> Fill:
        eff = price / self.haircut
        return Fill("sell", token_amount * eff, token_amount, eff, datetime.now(timezone.utc))
