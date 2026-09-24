"""Verify deployers recorded before the rugcheck-first fix against rugcheck `creator`.
Corrects the wallet when they differ (old signature-walk could pick a random trader on
busy tokens). Bounded per run; marks each token verified so it is checked once.
"""
from __future__ import annotations

import asyncio

import httpx
import structlog

from memebot.collectors.rugcheck import creator_of, fetch_report
from memebot.db import conn

log = structlog.get_logger()


async def run(limit: int = 100) -> dict:
    with conn() as c:
        rows = c.execute(
            """select mint, deployer from tokens
               where meta->>'deployer_source' is null and meta->>'deployer_verified' is null
               order by created_at desc limit %s""",
            (limit,),
        ).fetchall()
    checked = fixed = unknown = 0
    async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"}) as client:
        for r in rows:
            status, report = await fetch_report(client, r["mint"])
            await asyncio.sleep(0.6)
            if status == "error":
                continue  # transient: leave unmarked, retried next run
            creator = creator_of(report) if status == "ok" else None
            with conn() as c:
                if creator is None:
                    unknown += 1
                    c.execute(
                        "update tokens set meta = meta || '{\"deployer_verified\": \"unavailable\"}' where mint=%s",
                        (r["mint"],),
                    )
                elif creator == r["deployer"]:
                    c.execute(
                        "update tokens set meta = meta || '{\"deployer_verified\": \"match\"}' where mint=%s",
                        (r["mint"],),
                    )
                else:
                    fixed += 1
                    c.execute(
                        """update tokens set deployer=%s,
                             meta = meta || jsonb_build_object('deployer_verified','corrected','deployer_old',%s::text)
                           where mint=%s""",
                        (creator, r["deployer"], r["mint"]),
                    )
                    c.execute(
                        """insert into wallets(address, tags) values (%s, '{deployer}')
                           on conflict (address) do update set tags =
                             (select array(select distinct unnest(wallets.tags || '{deployer}')))""",
                        (creator,),
                    )
                c.commit()
            checked += 1
    out = {"checked": checked, "corrected": fixed, "unavailable": unknown}
    log.info("backfill.done", **out)
    return out


if __name__ == "__main__":
    print(asyncio.run(run()))
