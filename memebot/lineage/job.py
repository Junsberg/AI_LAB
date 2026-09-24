"""Lineage job: trace untraced deployers, rebuild clusters, refresh cluster scores.
Runs hourly on GitHub Actions. Bounded per run to stay inside Helius free credits.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import structlog

from memebot.db import conn
from memebot.lineage.cluster import ClusterStats, Edge, build_clusters, score_cluster
from memebot.lineage.tracer import KNOWN_CEX, Tracer

log = structlog.get_logger()


async def trace_pending(limit: int = 40) -> int:
    with conn() as c:
        rows = c.execute(
            """select address from wallets
               where 'deployer' = any(tags) and funded_by is null
                 and coalesce((meta->>'trace_attempts')::int, 0) < 3
               order by first_seen desc limit %s""",
            (limit,),
        ).fetchall()
    if not rows:
        return 0
    traced = 0
    async with httpx.AsyncClient(timeout=30) as client:
        tracer = Tracer(client)
        for r in rows:
            addr = r["address"]
            try:
                edges, hops = await tracer.walk(addr)
            except httpx.HTTPError as e:
                log.warning("trace.failed", wallet=addr, error=str(e))
                with conn() as c:
                    c.execute(
                        """update wallets set meta = meta || jsonb_build_object('trace_attempts',
                           coalesce((meta->>'trace_attempts')::int,0)+1) where address=%s""",
                        (addr,),
                    )
                    c.commit()
                continue
            with conn() as c:
                for h in hops:
                    funded_at = (
                        datetime.fromtimestamp(h.block_time, tz=timezone.utc) if h.block_time else None
                    )
                    c.execute(
                        """insert into wallets(address, funded_by, funded_at, funding_source_type)
                           values (%s,%s,%s,%s)
                           on conflict (address) do update set
                             funded_by = coalesce(wallets.funded_by, excluded.funded_by),
                             funded_at = coalesce(wallets.funded_at, excluded.funded_at),
                             funding_source_type = coalesce(wallets.funding_source_type, excluded.funding_source_type)""",
                        (h.wallet, h.funded_by, funded_at, h.source_type),
                    )
                    if h.funded_by and h.funded_by in KNOWN_CEX:
                        c.execute(
                            """insert into wallets(address, tags, meta) values (%s, '{cex}', %s)
                               on conflict (address) do nothing""",
                            (h.funded_by, '{"exchange": "%s"}' % KNOWN_CEX[h.funded_by]),
                        )
                for e in edges:
                    c.execute(
                        """insert into wallet_edges(src, dst, kind, amount_sol) values (%s,%s,%s,%s)
                           on conflict do nothing""",
                        (e.src, e.dst, e.kind, e.amount_sol),
                    )
                # mark attempted even when unknown, so we don't loop on dead wallets
                c.execute(
                    """update wallets set meta = meta || jsonb_build_object('trace_attempts',
                       coalesce((meta->>'trace_attempts')::int,0)+1) where address=%s""",
                    (addr,),
                )
                c.commit()
            traced += 1
    log.info("trace.done", traced=traced)
    return traced


def rebuild_clusters() -> int:
    """Recompute cluster ids over all edges. CEX wallets are excluded from union so that
    every Binance-funded deployer does not collapse into one giant cluster."""
    with conn() as c:
        rows = c.execute("select src, dst, kind, amount_sol from wallet_edges").fetchall()
    edges = [
        Edge(r["src"], r["dst"], r["kind"], float(r["amount_sol"] or 0))
        for r in rows
        if r["src"] not in KNOWN_CEX and r["dst"] not in KNOWN_CEX
    ]
    mapping = build_clusters(edges)
    with conn() as c:
        for addr, cid in mapping.items():
            c.execute("update wallets set cluster_id=%s where address=%s", (cid, addr))
        # singleton deployers: cluster = own address hash, so scoring still works
        c.execute(
            """update wallets set cluster_id = left(encode(sha256(address::bytea),'hex'),16)
               where cluster_id is null and 'deployer' = any(tags)"""
        )
        c.commit()
    return len(mapping)


def refresh_cluster_scores() -> int:
    with conn() as c:
        rows = c.execute(
            """select w.cluster_id,
                      count(t.mint) as total,
                      count(o.mint) as evaluated,
                      count(*) filter (where o.rugged) as rugged,
                      count(*) filter (where o.peak_multiple >= 10) as tenx
               from tokens t
               join wallets w on w.address = t.deployer
               left join token_outcomes o on o.mint = t.mint
               where w.cluster_id is not null
               group by w.cluster_id"""
        ).fetchall()
        for r in rows:
            # Score only on evaluated tokens; unevaluated ones are neither clean nor rugged.
            st = ClusterStats(tokens_total=r["evaluated"], tokens_rugged=r["rugged"], tokens_10x=r["tenx"])
            c.execute(
                """insert into cluster_scores(cluster_id, tokens_total, tokens_rugged, tokens_10x, score, updated_at)
                   values (%s,%s,%s,%s,%s,now())
                   on conflict (cluster_id) do update set
                     tokens_total=excluded.tokens_total, tokens_rugged=excluded.tokens_rugged,
                     tokens_10x=excluded.tokens_10x, score=excluded.score, updated_at=now()""",
                (r["cluster_id"], r["total"], r["rugged"], r["tenx"], round(score_cluster(st), 4)),
            )
        c.commit()
    return len(rows)


async def run() -> dict:
    traced = await trace_pending()
    clustered = rebuild_clusters()
    scored = refresh_cluster_scores()
    out = {"traced": traced, "clustered_wallets": clustered, "clusters_scored": scored}
    log.info("lineage.job.done", **out)
    return out


if __name__ == "__main__":
    print(asyncio.run(run()))
