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


def parse_report(r: dict) -> RiskSnapshot:
    top = r.get("topHolders") or []
    top10 = sum(float(h.get("pct") or 0) for h in top[:10]) if top else None
    insiders = sum(float(h.get("pct") or 0) for h in top if h.get("insider")) if top else None
    markets = r.get("markets") or []
    lp_locked = None
    if markets:
        lp = markets[0].get("lp") or {}
        lp_locked = float(lp.get("lpLockedPct") or 0)
    return RiskSnapshot(
        score=r.get("score_normalised") or r.get("score"),
        top10_pct=round(top10, 2) if top10 is not None else None,
        insiders_pct=round(insiders, 2) if insiders is not None else None,
        lp_locked_pct=lp_locked,
        mint_authority=r.get("mintAuthority") is not None,
        freeze_authority=r.get("freezeAuthority") is not None,
        risks=[x.get("name", "") for x in (r.get("risks") or [])],
    )


async def fetch(client: httpx.AsyncClient, mint: str) -> RiskSnapshot | None:
    r = await client.get(f"{RC}/{mint}/report")
    if r.status_code in (404, 400):
        return None
    r.raise_for_status()
    return parse_report(r.json())


async def run(limit: int = 60) -> int:
    with conn() as c:
        rows = c.execute(
            """select mint from tokens where meta->'risk' is null
               order by created_at desc limit %s""",
            (limit,),
        ).fetchall()
    n = 0
    async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"}) as client:
        for r in rows:
            try:
                snap = await fetch(client, r["mint"])
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
                    "update tokens set meta = meta || jsonb_build_object('risk', %s::jsonb) where mint=%s",
                    (json.dumps(payload), r["mint"]),
                )
                c.commit()
            n += 1
    log.info("rugcheck.done", enriched=n)
    return n


if __name__ == "__main__":
    print(asyncio.run(run()))
