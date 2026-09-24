from __future__ import annotations

from dataclasses import dataclass

import httpx

from memebot.lineage.cluster import Edge
from memebot.rpc import RpcError, signatures, transaction  # noqa: F401

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
    source_type: str  # cex | wallet | unknown | hub (too busy to find first inbound)
    slot: int | None = None
    block_time: int | None = None
    first_tx_kinds: list[str] | None = None  # diagnostics when unknown: program:type of first txs


class Tracer:
    """First-inbound-SOL funding walk over plain Helius RPC (cheap credits, free tier).

    Deployer wallets are usually young, so the oldest signature is 1-2 pages away.
    """

    def __init__(self, client: httpx.AsyncClient | None = None, max_pages: int = 3) -> None:
        self._client = client or httpx.AsyncClient(timeout=30)
        self._max_pages = max_pages

    async def first_inbound(self, wallet: str) -> FundingHop:
        sigs, exhausted = await signatures(self._client, wallet, self._max_pages)
        if not exhausted:
            # Busy hub wallet: the oldest page is mid-history. Refusing to guess here is
            # what keeps unrelated deployers from being unioned through a shared hub.
            return FundingHop(wallet, None, 0.0, "hub")
        # Only the very first transactions can be the funding event. Scanning further
        # forward would pick a later top-up and violate "funding precedes launch".
        kinds: list[str] = []
        for s in sigs[-5:][::-1]:  # oldest 5, oldest first
            if s.get("err"):
                continue
            tx = await transaction(self._client, s["signature"])
            if not tx:
                continue
            msg = tx["transaction"]["message"]
            inner = [
                i for grp in (tx.get("meta") or {}).get("innerInstructions", []) for i in grp.get("instructions", [])
            ]
            for ix in list(msg.get("instructions", [])) + inner:
                pid = str(ix.get("programId", ""))[:6]
                ptype = ((ix.get("parsed") or {}).get("type") if isinstance(ix.get("parsed"), dict) else None) or "-"
                if len(kinds) < 12:
                    kinds.append(f"{pid}:{ptype}")
                if ix.get("programId") != SYSTEM_PROGRAM:
                    continue
                p = ix.get("parsed") or {}
                info = p.get("info") or {}
                t = p.get("type")
                if t in ("transfer", "transferWithSeed") and info.get("destination") == wallet:
                    src, amt = info.get("source"), int(info.get("lamports", 0)) / 1e9
                elif t in ("createAccount", "createAccountWithSeed") and info.get("newAccount") == wallet:
                    src, amt = info.get("source"), int(info.get("lamports", 0)) / 1e9
                else:
                    continue
                if not src or src == wallet or amt <= 0:
                    continue
                kind = "cex" if src in KNOWN_CEX else "wallet"
                return FundingHop(wallet, src, amt, kind, tx.get("slot"), tx.get("blockTime"))
        return FundingHop(wallet, None, 0.0, "unknown", first_tx_kinds=kinds)

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
