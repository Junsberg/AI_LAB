from __future__ import annotations

from dataclasses import dataclass

import httpx

from memebot.config import settings
from memebot.lineage.cluster import Edge

# Well-known CEX hot wallets on Solana. A funding hop that lands here ends the walk
# (a CEX is not an operator). Extend from data as clusters surface new ones.
KNOWN_CEX: dict[str, str] = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "binance",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "binance",
    "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm": "coinbase",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "coinbase",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "bybit",
    "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w": "okx",
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "kraken",
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE": "bitget",
    "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ": "mexc",
}
SYSTEM_PROGRAM = "11111111111111111111111111111111"


@dataclass
class FundingHop:
    wallet: str
    funded_by: str | None
    amount_sol: float
    source_type: str  # cex | wallet | unknown
    slot: int | None = None
    block_time: int | None = None


class Tracer:
    """First-inbound-SOL funding walk over plain Helius RPC (cheap credits, free tier).

    Deployer wallets are usually young, so the oldest signature is 1-2 pages away.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=30)

    async def _rpc(self, method: str, params: list) -> dict | list | None:
        r = await self._client.post(
            settings.helius_rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        r.raise_for_status()
        return r.json().get("result")

    async def _oldest_signatures(self, wallet: str, max_pages: int = 3) -> list[dict]:
        before: str | None = None
        page: list[dict] = []
        for _ in range(max_pages):
            opts: dict = {"limit": 1000}
            if before:
                opts["before"] = before
            res = await self._rpc("getSignaturesForAddress", [wallet, opts]) or []
            if not res:
                break
            page = res
            before = res[-1]["signature"]
            if len(res) < 1000:
                break
        return page[-25:][::-1]  # oldest 25, oldest first

    async def first_inbound(self, wallet: str) -> FundingHop:
        for s in await self._oldest_signatures(wallet):
            if s.get("err"):
                continue
            tx = await self._rpc(
                "getTransaction",
                [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            )
            if not tx:
                continue
            msg = tx["transaction"]["message"]
            for ix in msg.get("instructions", []):
                if ix.get("programId") != SYSTEM_PROGRAM:
                    continue
                p = ix.get("parsed") or {}
                info = p.get("info") or {}
                if p.get("type") in ("transfer", "transferWithSeed") and info.get("destination") == wallet:
                    src = info.get("source")
                    amt = int(info.get("lamports", 0)) / 1e9
                    if not src or amt <= 0:
                        continue
                    kind = "cex" if src in KNOWN_CEX else "wallet"
                    return FundingHop(wallet, src, amt, kind, tx.get("slot"), tx.get("blockTime"))
        return FundingHop(wallet, None, 0.0, "unknown")

    async def walk(self, deployer: str, depth: int = 3) -> tuple[list[Edge], list[FundingHop]]:
        """Funded-edges from deployer up to `depth` hops or until a CEX / unknown."""
        edges: list[Edge] = []
        hops: list[FundingHop] = []
        cur = deployer
        for _ in range(depth):
            hop = await self.first_inbound(cur)
            hops.append(hop)
            if not hop.funded_by:
                break
            edges.append(Edge(src=hop.funded_by, dst=cur, kind="funded", amount_sol=hop.amount_sol))
            if hop.source_type == "cex":
                break
            cur = hop.funded_by
        return edges, hops
