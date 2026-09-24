"""rugcheck.xyz public API — free, no key. Fills the risk snapshot used by the scorer's
hard gates (top-10 concentration, insider/bundle share, LP lock, mint/freeze authority).
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

import httpx
import structlog

from memebot.db import conn

log = structlog.get_logger()
RC = "https://api.rugcheck.xyz/v1/tokens"


@dataclass(frozen=True)
class RiskSnapshot:
    score: int | None  # rugcheck normalized score (higher = riskier)
    top10_pct: float | None
    insiders_pct: float | None
    lp_locked_pct: float | None
    mint_authority: bool | None
    freeze_authority: bool | None
    risks: list[str]
    raw_top: list[dict]  # first 5 unfiltered holders, for auditing the pool filter


# Owners that hold supply structurally (AMM vaults, bonding curves), not as investors.
POOL_OWNERS = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM authority
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",   # PumpSwap AMM program
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # pump.fun bonding curve program
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # PumpSwap pool authority
}


def _pool_accounts(markets: list[dict]) -> set[str]:
    acc: set[str] = set()
    for m in markets:
        acc.add(str(m.get("pubkey", "")))
        lp = m.get("lp") or {}
        for k in ("lpMint", "quoteMint", "baseMint", "quoteVault", "baseVault"):
            if lp.get(k):
                acc.add(str(lp[k]))
        for k in ("mintA", "mintB", "mintLP", "liquidityA", "liquidityB"):
            if m.get(k):
                acc.add(str(m[k]))
    acc.discard("")
    return acc


def parse_report(r: dict, structural: set[str] | None = None) -> RiskSnapshot:
    markets = r.get("markets") or []
    pool_acc = _pool_accounts(markets) | POOL_OWNERS | (structural or set())
    holders = [
        h for h in (r.get("topHolders") or [])
        if str(h.get("owner", "")) not in pool_acc and str(h.get("address", "")) not in pool_acc
    ]
    top10 = sum(float(h.get("pct") or 0) for h in holders[:10]) if holders else None
    insiders = sum(float(h.get("pct") or 0) for h in holders if h.get("insider")) if holders else None
    lp_locked = None
    if markets:
        lp = markets[0].get("lp") or {}
        lp_locked = float(lp.get("lpLockedPct") or 0)
    return RiskSnapshot(
        score=r["score_normalised"] if r.get("score_normalised") is not None else r.get("score"),
        top10_pct=round(top10, 2) if top10 is not None else None,
        insiders_pct=round(insiders, 2) if insiders is not None else None,
        lp_locked_pct=lp_locked,
        mint_authority=r.get("mintAuthority") is not None,
        freeze_authority=r.get("freezeAuthority") is not None,
        risks=[x.get("name", "") for x in (r.get("risks") or [])],
        raw_top=[
            {"owner": str(h.get("owner", "")), "pct": round(float(h.get("pct") or 0), 2)}
            for h in (r.get("topHolders") or [])[:5]
        ],
    )


async def fetch(client: httpx.AsyncClient, mint: str, structural: set[str] | None = None) -> RiskSnapshot | None:
    r = await client.get(f"{RC}/{mint}/report")
    if r.status_code in (404, 400):
        return None
    r.raise_for_status()
    return parse_report(r.json(), structural)


def structural_owners(min_tokens: int = 4, min_pct: float = 30.0) -> set[str]:
    """Owners that sit in the top-5 with ≥min_pct across ≥min_tokens distinct tokens are
    AMM vaults / launchpad authorities, not investors. Learned from our own data so new
    DEXes are handled without a code change."""
    with conn() as c:
        rows = c.execute(
            """select h->>'owner' owner, count(distinct mint) n
               from tokens, jsonb_array_elements(meta->'risk'->'raw_top') h
               where (h->>'pct')::numeric >= %s and length(h->>'owner') >= 32
               group by 1 having count(distinct mint) >= %s""",
            (min_pct, min_tokens),
        ).fetchall()
    return {r["owner"] for r in rows}


async def run(limit: int = 60) -> int:
    with conn() as c:
        rows = c.execute(
            """select mint from tokens
               where meta->'risk' is null
                  or ((meta->'risk'->>'top10_pct')::numeric > 90
                      and coalesce((meta->'risk'->>'recheck')::int, 0) < 2)
               order by (meta->'risk' is null) desc, created_at desc limit %s""",
            (limit,),
        ).fetchall()
    n = 0
    structural = structural_owners()
    log.info("rugcheck.structural_owners", n=len(structural))
    async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"}) as client:
        for r in rows:
            try:
                snap = await fetch(client, r["mint"], structural)
            except httpx.HTTPError as e:
                log.warning("rugcheck.failed", mint=r["mint"], error=str(e))
                if "429" in str(e):
                    break
                continue
            await asyncio.sleep(0.6)
            if snap is None:
                payload = {"unavailable": True}
            else:
                payload = asdict(snap)
            with conn() as c:
                import json

                c.execute(
                    """update tokens set meta = meta || jsonb_build_object('risk',
                         %s::jsonb || jsonb_build_object('recheck',
                           coalesce((meta->'risk'->>'recheck')::int, -1) + 1))
                       where mint=%s""",
                    (json.dumps(payload), r["mint"]),
                )
                c.commit()
            n += 1
    log.info("rugcheck.done", enriched=n)
    return n


if __name__ == "__main__":
    print(asyncio.run(run()))
