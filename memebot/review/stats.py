"""Aggregate DB state into docs/stats/latest.json + a dated copy.
Runs in GitHub Actions so the reviewer (Claude) reads a file, never the DB.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from memebot.config import ROOT
from memebot.db import conn

OUT = ROOT / "docs" / "stats"

QUERIES: dict[str, str] = {
    "counts": """
        select
          (select count(*) from tokens) tokens,
          (select count(*) from tokens where created_at > now()-interval '24 hours') tokens_24h,
          (select count(*) from wallets where 'deployer'=any(tags)) deployers,
          (select count(*) from wallets where 'deployer'=any(tags) and funded_by is not null) deployers_traced,
          (select count(*) from wallets where 'deployer'=any(tags) and funding_source_type='unknown') deployers_unknown_src,
          (select count(*) from wallet_edges) edges,
          (select count(*) from cluster_scores) clusters,
          (select count(*) from cluster_scores where tokens_total>1) multi_token_clusters,
          (select count(*) from token_outcomes) outcomes,
          (select count(*) from token_outcomes where rugged) rugged,
          (select count(*) from token_outcomes where peak_multiple>=10) tenx,
          (select count(*) from tokens where meta->'risk' is not null) with_risk,
          (select count(*) from signals) signals,
          (select count(*) from positions) positions,
          (select count(*) from hypotheses) hypotheses
    """,
    "risk_distribution": """
        select
          round(percentile_cont(0.5) within group (order by (meta->'risk'->>'top10_pct')::numeric)::numeric,1) top10_p50,
          round(percentile_cont(0.9) within group (order by (meta->'risk'->>'top10_pct')::numeric)::numeric,1) top10_p90,
          round(avg((meta->'risk'->>'insiders_pct')::numeric),1) insiders_avg,
          count(*) filter (where (meta->'risk'->>'lp_locked_pct')::numeric >= 90) lp_locked,
          count(*) filter (where (meta->'risk'->>'freeze_authority')::bool) freeze_on,
          count(*) filter (where (meta->'risk'->>'mint_authority')::bool) mint_on,
          count(*) n
        from tokens where meta->'risk'->>'top10_pct' is not null
    """,
    "tokens_per_hour_24h": """
        select to_char(date_trunc('hour', created_at),'YYYY-MM-DD HH24:00') h, count(*) n
        from tokens where created_at > now()-interval '24 hours' group by 1 order by 1
    """,
    "top_deployers": """
        select deployer, count(*) n, string_agg(coalesce(symbol,'?'), ',' order by created_at) syms,
               to_char(min(created_at),'MM-DD HH24:MI') first_t, to_char(max(created_at),'MM-DD HH24:MI') last_t
        from tokens group by deployer having count(*)>1 order by n desc limit 10
    """,
    "top_clusters": """
        select cs.cluster_id, cs.tokens_total, cs.tokens_evaluated, cs.tokens_rugged, cs.tokens_10x, cs.score,
               count(distinct w.address) deployers
        from cluster_scores cs join wallets w on w.cluster_id=cs.cluster_id and 'deployer'=any(w.tags)
        where cs.tokens_total>1 group by 1,2,3,4,5,6 order by cs.tokens_total desc limit 10
    """,
    "outcomes_summary": """
        select rug_reason, count(*) n, round(avg(peak_multiple),2) avg_peak, round(max(peak_multiple),2) max_peak
        from token_outcomes group by 1 order by n desc
    """,
    "outcome_by_cluster_size": """
        select case when cs.tokens_total=1 then 'single' when cs.tokens_total<=3 then '2-3' else '4+' end bucket,
               count(*) n, round(avg((o.rugged)::int),3) rug_rate, round(avg(o.peak_multiple),2) avg_peak
        from token_outcomes o join tokens t on t.mint=o.mint join wallets w on w.address=t.deployer
        join cluster_scores cs on cs.cluster_id=w.cluster_id group by 1 order by 1
    """,
    "funding_sources": """
        select coalesce(funding_source_type,'untraced') src, count(*) n
        from wallets where 'deployer'=any(tags) group by 1 order by 2 desc
    """,
}


def run() -> Path:
    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat()}
    with conn() as c:
        for k, q in QUERIES.items():
            rows = c.execute(q).fetchall()
            out[k] = [{kk: (float(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, (int, bool)) else v)
                       for kk, v in r.items()} for r in rows]
    OUT.mkdir(parents=True, exist_ok=True)
    latest = OUT / "latest.json"
    latest.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    dated = OUT / f"{datetime.now(timezone.utc):%Y-%m-%d}.json"
    dated.write_text(latest.read_text(encoding="utf-8"), encoding="utf-8")
    return latest


if __name__ == "__main__":
    print(run())
