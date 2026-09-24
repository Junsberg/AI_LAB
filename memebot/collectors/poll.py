"""Polling collector — runs anywhere with a cron (GitHub Actions free tier), no long-lived
process, no local PC. Sources are all free/no-key except Helius (free tier):

  GeckoTerminal  /networks/solana/new_pools   → newly migrated tokens + pool address
  Helius RPC     getSignaturesForAddress(mint) → oldest tx = mint creation → deployer

Bonding-curve-stage tokens are deliberately skipped: lineage and survivor signals
work on the post-migration universe, which is also where the noise is lowest.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime

import httpx
import structlog

from memebot.db import conn
from memebot.rpc import RpcError, signatures, transaction

log = structlog.get_logger()
GT = "https://api.geckoterminal.com/api/v2"
PUMP_DEXES = {"pumpswap", "pump-fun", "pumpfun"}
# Graduated pools only. GeckoTerminal also lists bonding-curve stage "pools"
# (pump-fun, meteora-dbc, raydium-launchlab, moonshot, bags-fm): ~10k/day, mostly dead
# within minutes, and outside the strategy (post-migration survivors + lineage).
UNIVERSE = {"pumpswap", "meteora-damm-v2"}
MIN_RESERVE_USD = 3000.0


@dataclass(frozen=True)
class NewPool:
    mint: str
    pool: str
    dex: str
    symbol: str | None
    name: str | None
    created_at: datetime


async def fetch_new_pools(client: httpx.AsyncClient, pages: int = 6) -> list[NewPool]:
    out: list[NewPool] = []
    for page in range(1, pages + 1):
        r = await client.get(f"{GT}/networks/solana/new_pools", params={"page": page})
        if r.status_code == 429:
            log.warning("geckoterminal.rate_limited", page=page)
            break
        r.raise_for_status()
        for p in r.json().get("data", []):
            a = p["attributes"]
            rel = p.get("relationships", {})
            base = rel.get("base_token", {}).get("data", {}).get("id", "")
            dex = rel.get("dex", {}).get("data", {}).get("id", "")
            mint = base.split("_", 1)[-1] if base else ""
            if not mint or mint.startswith("So1111"):
                continue
            if dex not in UNIVERSE:
                continue
            if float(a.get("reserve_in_usd") or 0) < MIN_RESERVE_USD:
                continue
            sym = (a.get("name") or "").split(" / ")[0] or None
            out.append(
                NewPool(
                    mint=mint,
                    pool=a["address"],
                    dex=dex,
                    symbol=sym,
                    name=None,
                    created_at=datetime.fromisoformat(
                        a["pool_created_at"].replace("Z", "+00:00")
                    ),
                )
            )
        await asyncio.sleep(1.2)  # GT free: ~30 req/min
    return out


async def find_deployer(client: httpx.AsyncClient, mint: str, max_pages: int = 30) -> tuple[str | None, int | None]:
    """Oldest signature for the mint → first signer. Returns (None, None) when the
    history is longer than `max_pages`×1000 — a guessed deployer poisons lineage, so
    we would rather leave the token out than store the wrong wallet."""
    sigs, exhausted = await signatures(client, mint, max_pages)
    if not sigs or not exhausted:
        return None, None
    oldest = sigs[-1]
    tx = await transaction(client, oldest["signature"])
    if not tx:
        return None, None
    keys = tx["transaction"]["message"]["accountKeys"]
    signer = next((k["pubkey"] for k in keys if k.get("signer")), None)
    return signer, tx.get("slot")


async def rugcheck_creator(client: httpx.AsyncClient, mint: str) -> str | None:
    """Creator from the rugcheck report (one call). Kept for backfill; poll uses
    fetch_report directly so the same report also yields the risk snapshot."""
    from memebot.collectors.rugcheck import creator_of, fetch_report

    status, report = await fetch_report(client, mint)
    return creator_of(report) if status == "ok" else None


async def run_once(max_new: int = 60) -> int:
    async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"}) as client:
        pools = await fetch_new_pools(client)
        with conn() as c:
            known = {
                r["mint"]
                for r in c.execute(
                    "select mint from tokens where mint = any(%s)", ([p.mint for p in pools],)
                ).fetchall()
            }
        fresh = [p for p in pools if p.mint not in known][:max_new]
        inserted = 0
        from dataclasses import asdict

        from memebot.collectors.rugcheck import creator_of, fetch_report, parse_report, structural_owners

        structural = structural_owners()
        for p in fresh:
            slot = None
            status, report = await fetch_report(client, p.mint)
            await asyncio.sleep(0.6)  # rugcheck pacing (same as the snapshot step)
            deployer = creator_of(report) if status == "ok" else None
            risk = None
            if status == "ok" and report.get("topHolders"):  # empty holders = too fresh; snapshot later
                try:
                    risk = asdict(parse_report(report, structural)) | {"attempts": 1, "recheck": 0}
                except (AttributeError, TypeError, ValueError) as e:
                    log.warning("rugcheck.bad_shape", mint=p.mint, error=str(e))
            source = "rugcheck"
            if not deployer:
                try:
                    deployer, slot = await find_deployer(client, p.mint)
                    source = "rpc"
                except (httpx.HTTPError, RpcError) as e:
                    log.warning("deployer.lookup_failed", mint=p.mint, error=str(e))
                    continue
            if not deployer:
                log.info("deployer.unresolved", mint=p.mint)
                continue
            kind = "structural" if deployer in structural else "wallet"
            platform = "pumpfun" if p.dex in PUMP_DEXES else p.dex or "other"
            with conn() as c:
                meta = {"deployer_source": source, "dex": p.dex, "stage": "graduated", "deployer_kind": kind}
                if risk:
                    meta["risk"] = risk
                c.execute(
                    """insert into tokens(mint, symbol, deployer, launch_platform, created_at,
                                          migrated_at, pool_address, first_seen_slot, meta)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                       on conflict (mint) do nothing""",
                    (p.mint, p.symbol, deployer, platform, p.created_at, p.created_at, p.pool, slot, json.dumps(meta)),
                )
                if kind == "wallet":
                    c.execute(
                        """insert into wallets(address, tags) values (%s, '{deployer}')
                           on conflict (address) do update set tags =
                             (select array(select distinct unnest(wallets.tags || '{deployer}')))""",
                        (deployer,),
                    )
                c.commit()
            inserted += 1
        log.info("poll.done", seen=len(pools), new=len(fresh), inserted=inserted)
        return inserted


if __name__ == "__main__":
    asyncio.run(run_once())
