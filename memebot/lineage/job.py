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
from memebot.rpc import RpcError

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
            except (httpx.HTTPError, RpcError) as e:
                # Transport / provider error: not the wallet's fault, do not burn an attempt.
                log.warning("trace.failed", wallet=addr, error=str(e))
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
                _bump_attempts(c, addr)
                c.commit()
            traced += 1
    log.info("trace.done", traced=traced)
    return traced


def _bump_attempts(c, addr: str) -> None:
    c.execute(
        """update wallets set meta = meta || jsonb_build_object('trace_attempts',
           coalesce((meta->>'trace_attempts')::int,0)+1) where address=%s""",
        (addr,),
    )


def repair_invalid_traces() -> dict:
    """Two value-level invariants, enforced every run:
    1. a deployer's first inbound funding must precede its earliest token launch;
       violations are stale results from the pre-cap tracer → reset and re-trace.
    2. a structural account (launchpad authority / AMM vault, learned from holder data)
       is not a person → never a deployer for lineage purposes."""
    from memebot.collectors.rugcheck import structural_owners

    with conn() as c:
        bad = c.execute(
            """select w.address from wallets w
               join (select deployer, min(created_at) first_launch from tokens group by 1) t
                 on t.deployer = w.address
               where w.funded_at is not null and w.funded_at > t.first_launch"""
        ).fetchall()
        for r in bad:
            c.execute("delete from wallet_edges where dst=%s and kind='funded'", (r["address"],))
            # second violation → the parser cannot see this wallet's real funding; stop guessing
            c.execute(
                """update wallets set funded_by=null, funded_at=null, cluster_id=null,
                       meta = (meta - 'trace_attempts') || jsonb_build_object('invalid_trace',
                              coalesce((meta->>'invalid_trace')::int,0)+1),
                       funding_source_type = case when coalesce((meta->>'invalid_trace')::int,0) >= 1
                                                  then 'unknown' else null end
                   where address=%s""",
                (r["address"],),
            )
        structural = structural_owners()
        marked = 0
        if structural:
            marked = c.execute(
                """update tokens set meta = meta || '{"deployer_kind":"structural"}'
                   where deployer = any(%s) and coalesce(meta->>'deployer_kind','') <> 'structural'""",
                (list(structural),),
            ).rowcount
            c.execute(
                """update wallets set tags = array_remove(tags,'deployer'), cluster_id=null
                   where address = any(%s) and 'deployer' = any(tags)""",
                (list(structural),),
            )
        c.commit()
    return {"retraced": len(bad), "structural_deployers_marked": marked}


def prune_orphans() -> dict:
    """After deployer corrections: drop the 'deployer' tag from wallets no token names
    any more, and delete funding edges not reachable (walking src←dst) from a current
    deployer, so stale chains cannot union into real clusters."""
    with conn() as c:
        untagged = c.execute(
            """update wallets set tags = array_remove(tags, 'deployer'), cluster_id = null
               where 'deployer' = any(tags)
                 and address not in (select deployer from tokens
                                     where coalesce(meta->>'deployer_kind','') <> 'structural')"""
        ).rowcount
        pruned = c.execute(
            """with recursive reach(addr) as (
                 select distinct deployer from tokens
                 union
                 select e.src from wallet_edges e join reach r on e.dst = r.addr
               )
               delete from wallet_edges e
               where e.dst not in (select addr from reach)"""
        ).rowcount
        c.commit()
    return {"untagged": untagged, "edges_pruned": pruned}


def rebuild_clusters() -> int:
    """Recompute cluster ids over all edges. CEX wallets are excluded from union so that
    every Binance-funded deployer does not collapse into one giant cluster."""
    with conn() as c:
        rows = c.execute("select src, dst, kind, amount_sol from wallet_edges").fetchall()
        # Funding sources that are themselves too busy to trace are exchanges / services:
        # they fund unrelated people, so they must not act as cluster glue. A launch-farm
        # funder has a short history and stays in the union.
        hubs = {
            r["address"]
            for r in c.execute(
                "select address from wallets where funding_source_type = 'hub'"
            ).fetchall()
        }
    glue_blocked = KNOWN_CEX.keys() | hubs
    edges = [
        Edge(r["src"], r["dst"], r["kind"], float(r["amount_sol"] or 0))
        for r in rows
        if r["src"] not in glue_blocked and r["dst"] not in KNOWN_CEX
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
        # cluster ids are content hashes: a membership change mints a new id, so drop
        # score rows no wallet references any more
        c.execute("delete from cluster_scores where cluster_id not in (select distinct cluster_id from wallets where cluster_id is not null)")
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
                 and coalesce(t.meta->>'deployer_kind','') <> 'structural'
               group by w.cluster_id"""
        ).fetchall()
        for r in rows:
            # Score only on evaluated tokens; unevaluated ones are neither clean nor rugged.
            st = ClusterStats(tokens_total=r["evaluated"], tokens_rugged=r["rugged"], tokens_10x=r["tenx"])
            c.execute(
                """insert into cluster_scores(cluster_id, tokens_total, tokens_evaluated, tokens_rugged, tokens_10x, score, updated_at)
                   values (%s,%s,%s,%s,%s,%s,now())
                   on conflict (cluster_id) do update set
                     tokens_total=excluded.tokens_total, tokens_evaluated=excluded.tokens_evaluated,
                     tokens_rugged=excluded.tokens_rugged, tokens_10x=excluded.tokens_10x,
                     score=excluded.score, updated_at=now()""",
                (r["cluster_id"], r["total"], r["evaluated"], r["rugged"], r["tenx"], round(score_cluster(st), 4)),
            )
        c.commit()
    return len(rows)


async def run() -> dict:
    repaired = repair_invalid_traces()
    pruned = prune_orphans()
    traced = await trace_pending()
    clustered = rebuild_clusters()
    scored = refresh_cluster_scores()
    out = {**repaired, **pruned, "traced": traced, "clustered_wallets": clustered, "clusters_scored": scored}
    log.info("lineage.job.done", **out)
    return out


if __name__ == "__main__":
    print(asyncio.run(run()))
