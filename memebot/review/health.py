"""Hourly data-quality health check → docs/stats/health.json.
Exit code 2 when a CRITICAL check fires (the workflow then opens/updates a GitHub issue).
Thresholds are deliberately loose; the point is to catch a broken pipeline within an
hour, not to tune signals.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from memebot.config import ROOT
from memebot.db import conn

OUT = ROOT / "docs" / "stats" / "health.json"


def _one(c, q: str) -> dict:
    return c.execute(q).fetchone()


def run() -> dict:
    with conn() as c:
        m = _one(
            c,
            """select
              (select count(*) from tokens where created_at > now()-interval '70 minutes') tokens_70m,
              (select count(*) from tokens where created_at > now()-interval '24 hours') tokens_24h,
              (select count(*) from tokens where meta->'risk' is null and created_at < now()-interval '40 minutes') risk_missing,
              (select count(*) from tokens where created_at < now()-interval '40 minutes') risk_eligible,
              (select count(*) from tokens where (meta->'risk'->>'top10_pct')::numeric > 90) top10_over90,
              (select count(*) from tokens where meta->'risk'->>'top10_pct' is not null) top10_n,
              (select count(*) from wallets where 'deployer'=any(tags)) deployers,
              (select count(*) from wallets where 'deployer'=any(tags) and funded_by is not null) traced,
              (select count(*) from wallets where 'deployer'=any(tags) and funding_source_type in ('unknown','hub')) untraceable,
              (select count(*) from wallets where 'deployer'=any(tags) and funded_by is null and funding_source_type is null and first_seen < now()-interval '3 hours') trace_backlog,
              (select count(*) from tokens t left join token_outcomes o on o.mint=t.mint
                 where t.created_at < now()-interval '25 hours' and o.mint is null) outcome_backlog,
              (select count(*) from token_outcomes) outcomes,
              (select count(*) from token_outcomes where rug_reason='no_data') outcomes_no_data,
              (select count(*) from tokens where meta->>'deployer_source' is null and meta->>'deployer_verified' is null) backfill_pending,
              (select count(*) from tokens where meta->>'deployer_verified'='corrected') deployers_corrected,
              (select count(*) from tokens where meta->>'stage' = 'graduated'
                 and meta->>'dex' not in ('pumpswap','meteora-damm-v2')) out_of_universe,
              (select count(*) from wallets w join (select deployer, min(created_at) fl from tokens group by 1) t on t.deployer=w.address
                 where w.funded_at is not null and w.funded_at > t.fl) funded_after_launch,
              (select count(*) from tokens t where coalesce(t.meta->>'deployer_kind','') <> 'structural' and t.deployer in
                 (select h->>'owner' from tokens, jsonb_array_elements(meta->'risk'->'raw_top') h
                  where (h->>'pct')::numeric >= 30 group by 1 having count(distinct mint) >= 4)) structural_deployer_unmarked,
              (select count(*) from wallet_edges where amount_sol > 10000) edge_huge,
              (select count(*) from tokens where created_at > now() or created_at < '2026-09-01') bad_timestamps,
              (select count(*) from wallet_edges where src = dst) self_edges,
              (select coalesce(max(cnt),0) from (select cluster_id, count(*) cnt from wallets where cluster_id is not null group by 1) x) max_cluster_wallets
            """,
        )
    m = {k: (int(v) if v is not None else 0) for k, v in m.items()}
    checks: list[dict] = []

    def chk(name: str, level: str, bad: bool, detail: str) -> None:
        checks.append({"name": name, "level": level, "ok": not bad, "detail": detail})

    chk("collect_alive", "critical", m["tokens_70m"] == 0, f"tokens last 70m = {m['tokens_70m']}")
    chk("collect_flood", "warn", m["tokens_70m"] > 150, f"tokens last 70m = {m['tokens_70m']} (>150 suggests bonding-stage leak)")
    rm = m["risk_missing"] / m["risk_eligible"] if m["risk_eligible"] else 0
    chk("risk_coverage", "warn", rm > 0.2, f"risk missing {m['risk_missing']}/{m['risk_eligible']} ({rm:.0%})")
    t90 = m["top10_over90"] / m["top10_n"] if m["top10_n"] else 0
    chk("holder_concentration_sane", "warn", t90 > 0.10, f"top10>90%: {m['top10_over90']}/{m['top10_n']} ({t90:.0%}) → pool filter gap")
    ut = m["untraceable"] / m["deployers"] if m["deployers"] else 0
    chk("trace_quality", "warn", ut > 0.4, f"unknown+hub {m['untraceable']}/{m['deployers']} ({ut:.0%})")
    chk("trace_backlog", "warn", m["trace_backlog"] > 200, f"deployers untraced >3h: {m['trace_backlog']}")
    chk("outcome_backlog", "warn", m["outcome_backlog"] > 300, f"tokens >25h without outcome: {m['outcome_backlog']}")
    nd = m["outcomes_no_data"] / m["outcomes"] if m["outcomes"] else 0
    chk("outcome_data_quality", "warn", m["outcomes"] >= 20 and nd > 0.3, f"no_data outcomes {m['outcomes_no_data']}/{m['outcomes']} ({nd:.0%})")
    chk("universe_clean", "warn", m["out_of_universe"] > 0, f"out-of-universe tokens: {m['out_of_universe']}")
    # value-level invariants: these are impossible if the data is right
    chk("funding_precedes_launch", "warn", m["funded_after_launch"] > 0, f"deployers funded after their first launch: {m['funded_after_launch']} (auto-retraced by enrich)")
    chk("structural_deployers_marked", "warn", m["structural_deployer_unmarked"] > 0, f"tokens whose deployer is a pool/launchpad account, unmarked: {m['structural_deployer_unmarked']}")
    chk("edge_amounts_sane", "warn", m["edge_huge"] > 3, f"funding edges > 10k SOL: {m['edge_huge']}")
    chk("timestamps_sane", "critical", m["bad_timestamps"] > 0, f"tokens with impossible created_at: {m['bad_timestamps']}")
    chk("no_self_edges", "critical", m["self_edges"] > 0, f"self edges: {m['self_edges']}")
    chk("cluster_size_sane", "warn", m["max_cluster_wallets"] > 40, f"largest cluster = {m['max_cluster_wallets']} wallets (exchange acting as glue?)")

    critical = [x for x in checks if x["level"] == "critical" and not x["ok"]]
    warns = [x for x in checks if x["level"] == "warn" and not x["ok"]]
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "critical" if critical else ("warn" if warns else "ok"),
        "metrics": m,
        "checks": checks,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    o = run()
    print(json.dumps({"status": o["status"], "failing": [c["name"] for c in o["checks"] if not c["ok"]]}))
    sys.exit(2 if o["status"] == "critical" else 0)
