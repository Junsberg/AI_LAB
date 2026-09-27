"""lineage_v0 replay: how often would the paper runner have entered, and roughly how
would those trades have gone, versus a random control group under the same exits.

Purpose is the ENTRY COUNT and a sanity check of the fill model — not tuning. The
entry thresholds were fixed before this report existed (DECISIONS 09-27).

No look-ahead where it can be avoided:
  * cluster score at entry uses only cluster tokens created ≥24h before the candidate
    (their 24h outcome was knowable then); the candidate never scores itself
  * entry clock = meta.seen_at, else created_at + 15 min (first collect cycle)
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

log = structlog.get_logger()
OUT = ROOT / "docs" / "stats" / "replay_lineage_v0.json"
SEEN_FALLBACK_S = 15 * 60
KNOWABLE_AFTER = timedelta(hours=24)
COST_PCT = 3.0  # per side: DEX fee + slippage at 0.5 SOL on a >=$3k pool (same as PaperBroker)
CONTROL_MIN, CONTROL_MAX = 30, 60
SEED = 27


def point_in_time_stats(rows: list[dict]) -> dict[str, tuple[int, int, int]]:
    """rows: mint, cluster_id, created_at, evaluated(bool), rugged(bool), peak_multiple.
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


def entry_clock(created_at: datetime, meta: dict) -> int:
    seen = (meta or {}).get("seen_at")
    if seen:
        try:
            return int(datetime.fromisoformat(seen).timestamp())
        except ValueError:
            pass
    return int(created_at.timestamp()) + SEEN_FALLBACK_S


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
                      coalesce(o.rugged, false) as rugged, o.peak_multiple
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


async def run(params: Params, horizon_h: int = 24, budget_s: float = 18 * 60) -> dict:
    now = datetime.now(timezone.utc)
    horizon_s = horizon_h * 3600
    window_end = now - timedelta(hours=horizon_h) - timedelta(seconds=SEEN_FALLBACK_S)
    all_rows, cands = _load(datetime(2000, 1, 1, tzinfo=timezone.utc), window_end)
    pit = point_in_time_stats(all_rows)
    th = params.thresholds

    selected, pool = [], []
    for r in cands:
        meta = r["meta"] if isinstance(r["meta"], dict) else json.loads(r["meta"] or "{}")
        ev, rg, tx = pit.get(r["mint"], (0, 0, 0))
        score = score_cluster(ClusterStats(ev, rg, tx))
        top10 = ((meta.get("risk") or {}).get("top10_pct"))
        if top10 is None or float(top10) > th.max_top10_holder_pct:
            continue  # unknown concentration fails closed
        row = {"mint": r["mint"], "pool": r["pool_address"], "cluster_id": r["cluster_id"],
               "created_at": r["created_at"].isoformat(), "entry_clock": entry_clock(r["created_at"], meta),
               "cluster_score_pit": round(score, 4), "cluster_evaluated_pit": ev}
        if ev >= th.lineage_min_evaluated and score >= th.lineage_min_score:
            selected.append(row)
        else:
            pool.append(row)
    rng = random.Random(SEED)
    n_ctrl = min(len(pool), CONTROL_MAX, max(CONTROL_MIN, 3 * len(selected)))
    control = rng.sample(pool, n_ctrl)

    deadline = time.monotonic() + budget_s
    done = {"strategy": [], "control": []}
    async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"}) as client:
        try:
            for group, rows in (("strategy", selected), ("control", control)):
                for row in rows:
                    params_q = {"aggregate": 5, "limit": horizon_s // 300 + 4,
                                "before_timestamp": row["entry_clock"] + horizon_s}
                    try:
                        r = await _get(client, f"{GT}/networks/solana/pools/{row['pool']}/ohlcv/minute",
                                       params_q, deadline)
                        await asyncio.sleep(GT_SLEEP)
                        if r.status_code == 404:
                            candles = []
                        else:
                            r.raise_for_status()
                            candles = _parse_ohlcv(r.json())
                    except (httpx.HTTPError, TypeError, ValueError) as e:
                        log.warning("replay.fetch_failed", mint=row["mint"], error=str(e))
                        continue
                    t = simulate(candles, row["entry_clock"], params.exits, params.risk.hard_stop_pct,
                                 COST_PCT, horizon_s, data_until_ts=int(now.timestamp()))
                    d = asdict(t)
                    d.pop("fills")
                    done[group].append(row | d | {"hold_min": round((t.exit_ts - t.entry_ts) / 60)
                                                  if t.exit_ts and t.entry_ts else None})
                    if time.monotonic() >= deadline:
                        raise BudgetExhausted
        except BudgetExhausted:
            log.warning("replay.budget_exhausted", strategy=len(done["strategy"]), control=len(done["control"]))

    return {
        "generated_at": now.isoformat(),
        "strategy": "lineage_v0",
        "thresholds": {"lineage_min_score": th.lineage_min_score, "lineage_min_evaluated": th.lineage_min_evaluated,
                       "max_top10_holder_pct": th.max_top10_holder_pct},
        "exits": params.exits.model_dump(), "hard_stop_pct": params.risk.hard_stop_pct,
        "cost_pct_per_side": COST_PCT, "horizon_h": horizon_h,
        "window": {"candidates": len(cands), "until": window_end.isoformat()},
        "entries_selected": len(selected), "control_sampled": n_ctrl,
        "exits_not_simulated": list(NOT_SIMULATED),
        "gates_unapplied": ["min_liquidity_sol (no SOL/USD at entry)", "max_bundle_pct (no bundle data)"],
        "known_bias": ["cluster membership is current, not point-in-time",
                       "stored outcomes may be the 72h re-evaluation",
                       "entry clock falls back to created_at+15min before meta.seen_at existed (09-27)"],
        "summary": {"strategy": summarize(done["strategy"]), "control": summarize(done["control"])},
        "trades": done,
    }


def main() -> None:
    report = asyncio.run(run(load_params()))
    Path(OUT).write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: report[k] for k in ("entries_selected", "control_sampled", "summary")}, indent=1))


if __name__ == "__main__":
    main()
