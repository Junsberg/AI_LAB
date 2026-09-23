from __future__ import annotations

from dataclasses import dataclass

import httpx

from memebot.config import settings
from memebot.lineage.cluster import Edge

# Well-known CEX hot wallets on Solana. Extend from data; a funding hop that lands here
# ends the walk (a CEX is not an "operator").
KNOWN_CEX: dict[str, str] = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "binance",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "binance",
    "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm": "coinbase",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "coinbase",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "bybit",
    "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w": "okx",
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "kraken",
}


@dataclass
class FundingHop:
    wallet: str
    funded_by: str | None
    amount_sol: float
    source_type: str  # cex | wallet | unknown


class Tracer:
    """Walks first-inbound-SOL funding for a wallet using Helius Enhanced Transactions.
    Free tier is enough: ~2 requests per hop, depth ≤ 3.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=20)
        self._base = "https://api.helius.xyz/v0/addresses"

    async def first_inbound(self, wallet: str) -> FundingHop:
        # Oldest transactions first: Helius paginates newest-first, so we walk to the end.
        url = f"{self._base}/{wallet}/transactions"
        params = {"api-key": settings.helius_api_key, "limit": 100}
        before: str | None = None
        oldest: list[dict] = []
        for _ in range(20):  # cap pages; deployer wallets are usually young
            if before:
                params["before"] = before
            r = await self._client.get(url, params=params)
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            oldest = page
            before = page[-1]["signature"]
            if len(page) < 100:
                break
        for tx in reversed(oldest):
            for t in tx.get("nativeTransfers", []):
                if t.get("toUserAccount") == wallet and t.get("amount", 0) > 0:
                    src = t["fromUserAccount"]
                    amt = t["amount"] / 1e9
                    kind = "cex" if src in KNOWN_CEX else "wallet"
                    return FundingHop(wallet, src, amt, kind)
        return FundingHop(wallet, None, 0.0, "unknown")

    async def walk(self, deployer: str, depth: int = 3) -> list[Edge]:
        """Return funded-edges from deployer up to `depth` hops or until a CEX."""
        edges: list[Edge] = []
        cur = deployer
        for _ in range(depth):
            hop = await self.first_inbound(cur)
            if not hop.funded_by:
                break
            edges.append(Edge(src=hop.funded_by, dst=cur, kind="funded", amount_sol=hop.amount_sol))
            if hop.source_type == "cex":
                break
            cur = hop.funded_by
        return edges
