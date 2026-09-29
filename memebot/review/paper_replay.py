"""Strategy replay (params.thresholds.strategy; lineage_v1 since 09-29): how often would
the paper runner have entered, and roughly how would those trades have gone, versus a
random control group of gate-passing tokens entered at the same moment with the same exits.

Purpose is the ENTRY COUNT and a sanity check — not tuning. The thresholds were fixed
before this report existed (DECISIONS 09-27 lineage_v0, 09-29 lineage_v1).

No look-ahead where it can be avoided:
  * cluster score at entry uses only cluster tokens created ≥24h before the candidate
    (their 24h outcome was knowable then); the candidate never scores itself;
    "first launch" = no earlier token from the same cluster
  * decision clock = launch + alive_decide_after_min; the alive trigger only sees
    candles that closed before it; both groups enter at that same candle
Known remaining bias (reported in the output): cluster *membership* is today's —
lineage discovered later is applied retroactively; stored outcomes may be the 72h
re-evaluation rather than the 24h one.

Gates not applicable to historical rows are listed in `gates_unapplied`, never
silently passed as "ok".
"""
from __future__ import annotations

import asyncio
import json
import random
import statistics
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import structlog

from memebot.config import ROOT, Params, load_params
from memebot.db import conn
from memebot.execution.sim import NOT_SIMULATED, simulate
from memebot.lineage.cluster import ClusterStats, score_cluster
from memebot.outcomes.evaluate import GT, GT_SLEEP, BudgetExhausted, _get, _parse_ohlcv
from memebot.signals.alive import alive

log = structlog.get_logger()
OUT_DIR = ROOT / "docs" / "stats"
KNOWABLE_AFTER = timedelta(hours=24)
COST_PCT = 3.0  # per side: DEX fee + slippage at 0.5 SOL on a >=$3k pool (same as PaperBroker)
SAMPLE_MAX = 240  # candle calls are the budget: GT 429s allow ~5/min on shared runners
SEED = 27


def prior_launches(rows: list[dict]) -> dict[str, int]:
    """{mint: number of same-cluster tokens created strictly before it}."""
    by_cluster: dict[str, list[dict]] = {}
    for r in rows:
        by_cluster.setdefault(r["cluster_id"], []).append(r)
    return {r["mint"]: sum(1 for m in members if m["created_at"] < r["created_at"])
            for members in by_cluster.values() for r in members}


def point_in_time_stats(rows: list[dict]) -> dict[str, tuple[int, int, int]]:
    """rows: mint, cluster_id, created_at, evaluated(bool), rugged(bool: rugged OR dead),
    peak_multiple (the post-entry multiple, rules v4) — same inputs as cluster_scores.
    → {mint: (evaluated, rugged, tenx)} over same-cluster tokens created at least
    KNOWABLE_AFTER before that mint. O(n²) per cluster; clusters are small."""
    by_cluster: dict[str, list[dict]] = {}
    for r in rows:
        by_cluster.setdefault(r["cluster_id"], []).append(r)
    out: dict[str, tuple[int, int, int]] = {}
    for members in by_cluster.values():
        for r in members:
            cutoff = r["created_at"] - KNOWABLE_AFTER
            known = [m for m in members if m["mint"] != r["mint"] and m["created_at"] <= cutoff and m["evaluated"]]
            out[r["mint"]] = (
                len(known),
                sum(1 for m in known if m["rugged"]),
                sum(1 for m in known if (m["peak_multiple"] or 0) >= 10),
            )
    return out


def summarize(trades: list[dict]) -> dict:
    rets = [t["ret"] for t in trades if t["ret"] is not None]
    if not rets:
        return {"n": 0}
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    return {
        "n": len(rets),
        "mean_ret": round(statistics.fmean(rets), 4),
        "stderr": round(statistics.stdev(rets) / len(rets) ** 0.5, 4) if len(rets) > 1 else None,
        "median_ret": round(statistics.median(rets), 4),
        "win_rate": round(sum(r > 0 for r in rets) / len(rets), 3),
        "exit_reasons": reasons,
    }


def _load(window_start: datetime, window_end: datetime) -> tuple[list[dict], list[dict]]:
    with conn() as c:
        all_rows = c.execute(
            """select t.mint, w.cluster_id, t.created_at, (o.mint is not null) as evaluated,
                      (coalesce(o.rugged, false) or o.rug_reason = 'dead') as rugged,
                      o.entry_multiple as peak_multiple
               from tokens t join wallets w on w.address = t.deployer
               left join token_outcomes o on o.mint = t.mint
               where w.cluster_id is not null
                 and coalesce(t.meta->>'deployer_kind','') <> 'structural'"""
        ).fetchall()
        cands = c.execute(
            """select t.mint, t.pool_address, t.created_at, t.meta, w.cluster_id
               from tokens t join wallets w on w.address = t.deployer
               where t.pool_address is not null and w.cluster_id is not null
                 and coalesce(t.meta->>'deployer_kind','') <> 'structural'
                 and t.created_at >= %s and t.created_at < %s
               order by t.created_at""",
            (window_start, window_end),
        ).fetchall()
    return [dict(r) for r in all_rows], [dict(r) for r in cands]


async def run(params: Params, horizon_h: int = 24, budget_s: float = 40 * 60) -> dict:
    now = datetime.now(timezone.utc)
    th = params.thresholds
    horizon_s = horizon_h * 3600
    decide_s = th.alive_decide_after_min * 60
    window_end = now - timedelta(seconds=horizon_s + decide_s)
    all_rows, cands = _load(datetime(2000, 1, 1, tzinfo=timezone.utc), window_end)
    pit = point_in_time_stats(all_rows)
    prior = prior_launches(all_rows)

    eligible = []
    for r in cands:
        meta = r["meta"] if isinstance(r["meta"], dict) else json.loads(r["meta"] or "{}")
        top10 = ((meta.get("risk") or {}).get("top10_pct"))
        if top10 is None or float(top10) > th.max_top10_holder_pct:
            continue  # unknown concentration fails closed
        ev, rg, tx = pit.get(r["mint"], (0, 0, 0))
        score = score_cluster(ClusterStats(ev, rg, tx))
        excluded = None
        if ev and score < th.lineage_reject_below:
            excluded = "bad_lineage"
        elif th.reject_first_launch and prior.get(r["mint"], 0) == 0:
            excluded = "first_launch"
        eligible.append({"mint": r["mint"], "pool": r["pool_address"], "cluster_id": r["cluster_id"],
                         "created_at": r["created_at"].isoformat(),
                         "decide_ts": int(r["created_at"].timestamp()) + decide_s,
                         "cluster_score_pit": round(score, 4), "cluster_evaluated_pit": ev,
                         "prior_launches": prior.get(r["mint"], 0), "lineage_excluded": excluded})
    sample = random.Random(SEED).sample(eligible, min(SAMPLE_MAX, len(eligible)))

    deadline = time.monotonic() + budget_s
    done: dict[str, list] = {"strategy": [], "control": []}
    reasons: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"}) as client:
        try:
            for row in sample:
                launch = row["decide_ts"] - decide_s
                q = {"aggregate": 5, "limit": (decide_s + horizon_s) // 300 + 8,
                     "before_timestamp": row["decide_ts"] + horizon_s}
                try:
                    r = await _get(client, f"{GT}/networks/solana/pools/{row['pool']}/ohlcv/minute", q, deadline)
                    await asyncio.sleep(GT_SLEEP)
                    if r.status_code == 404:
                        candles = []
                    else:
                        r.raise_for_status()
                        candles = [c for c in _parse_ohlcv(r.json()) if c.ts >= launch]
                except (httpx.HTTPError, TypeError, ValueError) as e:
                    log.warning("replay.fetch_failed", mint=row["mint"], error=str(e))
                    continue
                ok, why, m = alive(candles, row["decide_ts"], th)
                why = row["lineage_excluded"] or why
                reasons[why or "enter"] = reasons.get(why or "enter", 0) + 1
                entry_slot = row["decide_ts"] // 300 * 300
                t = simulate(candles, entry_slot, params.exits, params.risk.hard_stop_pct,
                             COST_PCT, horizon_s, data_until_ts=int(now.timestamp()))
                d = asdict(t)
                d.pop("fills")
                rec = row | d | {"alive": m, "decision": why or "enter",
                                 "hold_min": round((t.exit_ts - t.entry_ts) / 60) if t.exit_ts and t.entry_ts else None}
                done["control"].append(rec)
                if why is None:
                    done["strategy"].append(rec)
                if time.monotonic() >= deadline:
                    raise BudgetExhausted
        except BudgetExhausted:
            log.warning("replay.budget_exhausted", evaluated=len(done["control"]), sample=len(sample))

    return {
        "generated_at": now.isoformat(),
        "strategy": th.strategy,
        "thresholds": {k: v for k, v in th.model_dump().items()
                       if k.startswith("alive_") or k in ("reject_first_launch", "lineage_reject_below",
                                                          "max_top10_holder_pct")},
        "exits": params.exits.model_dump(), "hard_stop_pct": params.risk.hard_stop_pct,
        "cost_pct_per_side": COST_PCT, "horizon_h": horizon_h,
        "window": {"candidates": len(cands), "eligible": len(eligible), "sampled": len(sample),
                   "evaluated": len(done["control"]), "until": window_end.isoformat()},
        "decisions": reasons,
        "exits_not_simulated": list(NOT_SIMULATED),
        "gates_unapplied": ["min_liquidity_sol (no SOL/USD at decision time)", "max_bundle_pct (no bundle data)"],
        "known_bias": ["cluster membership is current, not point-in-time",
                       "control = every sampled gate-passer (strategy is a subset of it)"],
        "summary": {"strategy": summarize(done["strategy"]), "control": summarize(done["control"])},
        "trades": done,
    }


def main() -> None:
    params = load_params()
    report = asyncio.run(run(params))
    Path(OUT_DIR / f"replay_{params.thresholds.strategy}.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: report[k] for k in ("window", "decisions", "summary")}, indent=1))


if __name__ == "__main__":
    main()
