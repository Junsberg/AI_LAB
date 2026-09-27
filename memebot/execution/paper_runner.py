"""lineage_v0 paper runner — every 10 minutes from Actions (paper.yml).

1. Decide every freshly collected token once (one `signals` row per mint and params
   version): enter when its deployer cluster scores >= lineage_min_score over
   >= lineage_min_evaluated evaluated tokens and the gates pass. A deterministic 5%
   of the gate-passing tokens that lineage does NOT select open `paper_control`
   positions: same exits, same fills — the baseline the strategy must beat.
2. Re-simulate every open position from its entry on 5-minute candles
   (execution/sim.py, the same code as the replay report) and close it when an exit
   fires or the horizon ends. Stateless: the candles, not stored state, decide.

Risk caps (params `risk`, frozen) are enforced here, in code: max open positions,
max position size, daily realised loss, hard stop. Nothing here can place a real order.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from memebot.config import Params, load_params
from memebot.db import conn
from memebot.execution.sim import NOT_SIMULATED, simulate
from memebot.outcomes.evaluate import (
    GT,
    GT_SLEEP,
    MULTI_BATCH,
    BudgetExhausted,
    _get,
    _parse_ohlcv,
    _pools,
)

log = structlog.get_logger()
STRATEGY = "lineage_v0"
HORIZON_S = 24 * 3600  # same as the replay report
FEE_PCT = 1.0  # DEX fee + priority fee, per side
CONTROL_EVERY = 20  # 1 in 20 gate-passing, non-selected tokens → control (5%)
CONTROL_MAX_OPEN = 10
CONTROL_SIZE_SOL = 0.2
CANDIDATE_WINDOW = timedelta(minutes=60)  # wait this long for a rugcheck snapshot
RECHECK_AFTER = timedelta(minutes=30)  # candle refresh cadence per open position
NO_FILL_AFTER_S = 3600
RUN_BUDGET_S = 7 * 60  # workflow timeout is 9 min


def liquidity_sol(attrs: dict) -> float | None:
    """Quote-side (SOL) reserve from GeckoTerminal pool attributes, or None if unknown."""
    try:
        reserve = float(attrs.get("reserve_in_usd"))
        sol_usd = float(attrs.get("quote_token_price_usd"))
    except (TypeError, ValueError):
        return None
    if reserve <= 0 or sol_usd <= 0:
        return None
    return reserve / 2 / sol_usd


def cost_pct(size_sol: float, liq_sol: float) -> float:
    """Per-side cost: constant-product price impact of `size_sol` on the SOL reserve
    (average fill ≈ Δ/Q above spot) plus fees."""
    return FEE_PCT + 100 * size_sol / (liq_sol + size_sol)


def position_size(score: float, p: Params) -> float:
    """40% of max at the threshold → 100% at score 1.0 (the scorer's sizing curve)."""
    t = p.thresholds.lineage_min_score
    frac = 0.4 + 0.6 * (score - t) / max(1e-9, 1.0 - t)
    return round(p.risk.max_position_sol * min(1.0, max(0.4, frac)), 4)


def is_control(mint: str) -> bool:
    return int(hashlib.sha256(mint.encode()).hexdigest()[:8], 16) % CONTROL_EVERY == 0


def decide(row: dict, attrs: dict, p: Params, open_strategy: int, day_pnl: float) -> tuple[str, str | None]:
    """→ (decision, reason). decision: enter | control | skip | reject | wait."""
    th, r = p.thresholds, p.risk
    top10 = ((row["meta"].get("risk") or {}).get("top10_pct"))
    if top10 is None:
        return ("wait", "risk_pending") if row["age"] < CANDIDATE_WINDOW else ("reject", "risk_unknown")
    if float(top10) > th.max_top10_holder_pct:
        return "reject", "concentrated"
    liq = liquidity_sol(attrs)
    if liq is None:
        return "reject", "liquidity_unknown"
    if liq < th.min_liquidity_sol:
        return "reject", "low_liquidity"
    selected = (row["score"] is not None and row["evaluated"] >= th.lineage_min_evaluated
                and row["score"] >= th.lineage_min_score)
    if not selected:
        return ("control", None) if is_control(row["mint"]) else ("skip", "lineage_below")
    if open_strategy >= r.max_open_positions:
        return "skip", "max_positions"
    if day_pnl <= -r.max_daily_loss_sol:
        return "skip", "daily_loss_cap"
    return "enter", None


def _candidates(p: Params) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """select t.mint, t.pool_address, t.meta, now() - (t.meta->>'seen_at')::timestamptz as age,
                      cs.score, coalesce(cs.tokens_evaluated, 0) as evaluated, w.cluster_id
               from tokens t
               left join wallets w on w.address = t.deployer
               left join cluster_scores cs on cs.cluster_id = w.cluster_id
               where t.meta ? 'seen_at' and t.pool_address is not null
                 and (t.meta->>'seen_at')::timestamptz > now() - %s * interval '1 second'
                 and coalesce(t.meta->>'deployer_kind','') <> 'structural'
                 and not exists (select 1 from signals s where s.mint = t.mint and s.params_version = %s)
               order by t.meta->>'seen_at'""",
            (CANDIDATE_WINDOW.total_seconds() * 2, p.version),
        ).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        r["meta"] = r["meta"] if isinstance(r["meta"], dict) else json.loads(r["meta"] or "{}")
        r["score"] = None if r["score"] is None else float(r["score"])
        out.append(r)
    return out


def _book() -> tuple[int, int, float]:
    with conn() as c:
        r = c.execute(
            """select count(*) filter (where mode = 'paper' and closed_at is null) as open_s,
                      count(*) filter (where mode = 'paper_control' and closed_at is null) as open_c,
                      coalesce(sum(pnl_sol) filter (where mode = 'paper'
                               and closed_at >= date_trunc('day', now())), 0) as day_pnl
               from positions"""
        ).fetchone()
    return int(r["open_s"]), int(r["open_c"]), float(r["day_pnl"])


def _record(row: dict, attrs: dict, decision: str, reason: str | None, p: Params, now: datetime) -> None:
    liq = liquidity_sol(attrs)
    snap = {"strategy": STRATEGY, "cluster_id": row["cluster_id"], "cluster_score": row["score"],
            "cluster_evaluated": row["evaluated"], "liquidity_sol": liq,
            "top10_pct": (row["meta"].get("risk") or {}).get("top10_pct"),
            "price_usd": attrs.get("base_token_price_usd"), "control": decision == "control",
            "gates_unapplied": ["max_bundle_pct"], "exits_not_simulated": list(NOT_SIMULATED)}
    with conn() as c:
        sig = c.execute(
            """insert into signals(mint, params_version, lineage_score, total_score, decision, reject_reason, snapshot)
               values (%s, %s, %s, %s, %s, %s, %s::jsonb)
               on conflict (mint, params_version) do nothing returning id""",
            (row["mint"], p.version, row["score"], row["score"] or 0,
             "enter" if decision in ("enter", "control") else decision, reason, json.dumps(snap)),
        ).fetchone()
        if sig and decision in ("enter", "control"):
            size = position_size(row["score"], p) if decision == "enter" else CONTROL_SIZE_SOL
            price = float(attrs.get("base_token_price_usd") or 0)
            cpct = cost_pct(size, liq)
            entry_candle = int(now.timestamp()) // 300 * 300
            c.execute(
                """insert into positions(mint, signal_id, mode, opened_at, entry_sol, entry_price,
                                         size_tokens, remaining_tokens, meta)
                   values (%s, %s, %s, %s, %s, %s, 0, 0, %s::jsonb)
                   on conflict do nothing""",
                (row["mint"], sig["id"], "paper" if decision == "enter" else "paper_control", now, size, price,
                 json.dumps({"pool": row["pool_address"], "entry_candle_ts": entry_candle, "cost_pct": cpct,
                             "strategy": STRATEGY, "provisional_price": True})),
            )
        c.commit()


def _open_positions() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """select id, mint, mode, opened_at, entry_sol, meta from positions
               where closed_at is null and mode in ('paper', 'paper_control')
                 and coalesce((meta->>'checked_at')::timestamptz, 'epoch') < now() - %s * interval '1 second'
               order by coalesce((meta->>'checked_at')::timestamptz, 'epoch')""",
            (RECHECK_AFTER.total_seconds(),),
        ).fetchall()
    return [dict(r) | {"meta": r["meta"] if isinstance(r["meta"], dict) else json.loads(r["meta"] or "{}")}
            for r in rows]


def _apply(pos: dict, trade, now: datetime) -> str:
    """Write the simulated state of one position. Returns open | closed | waiting."""
    meta = pos["meta"] | {"checked_at": now.isoformat()}
    entry_sol = float(pos["entry_sol"])
    start = pos["meta"]["entry_candle_ts"]
    with conn() as c:
        if trade.entry_ts is None:
            if now.timestamp() - start > NO_FILL_AFTER_S:
                c.execute("update positions set closed_at = now(), exit_reason = 'no_fill', pnl_sol = 0, "
                          "meta = %s::jsonb where id = %s", (json.dumps(meta), pos["id"]))
                c.commit()
                return "closed"
            c.execute("update positions set meta = %s::jsonb where id = %s", (json.dumps(meta), pos["id"]))
            c.commit()
            return "waiting"
        cost = pos["meta"]["cost_pct"] / 100
        final = trade.exit_reason != "horizon" or now.timestamp() >= start + HORIZON_S
        # while open, the simulator's closing "horizon" sell is only a mark, not a fill
        real = trade.fills if final else [f for f in trade.fills if f[1] != "horizon"]
        realized = 0.0
        for ts, reason, frac, px in real:
            sol = entry_sol * frac * px * (1 - cost) / (trade.entry_px * (1 + cost))
            realized += sol
            c.execute(
                """insert into fills(position_id, ts, side, sol_amount, token_amount, price, reason)
                   select %s, to_timestamp(%s), 'sell', %s, %s, %s, %s
                   where not exists (select 1 from fills where position_id = %s and reason = %s)""",
                (pos["id"], ts, sol, frac, px, reason, pos["id"], reason),
            )
        sold = sum(f[2] for f in real)
        meta["provisional_price"] = False
        c.execute(
            """update positions set entry_price = %s, size_tokens = 1, remaining_tokens = %s,
                      realized_sol = %s, meta = %s::jsonb,
                      closed_at = case when %s then to_timestamp(%s) end,
                      exit_reason = case when %s then %s end,
                      pnl_sol = case when %s then %s end
               where id = %s""",
            (trade.entry_px, max(0.0, 1 - sold), realized, json.dumps(meta),
             final, trade.exit_ts, final, trade.exit_reason, final, entry_sol * (trade.ret or 0), pos["id"]),
        )
        c.commit()
    return "closed" if final else "open"


async def run(p: Params | None = None, budget_s: float = RUN_BUDGET_S) -> dict:
    p = p or load_params()
    now = datetime.now(timezone.utc)
    deadline = time.monotonic() + budget_s
    stats = {"decided": {}, "positions": {}}
    async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"}) as client:
        try:
            cands = _candidates(p)
            open_s, open_c, day_pnl = _book()
            for i in range(0, len(cands), MULTI_BATCH):
                batch = cands[i : i + MULTI_BATCH]
                attrs = await _pools(client, [r["pool_address"] for r in batch], deadline)
                for row in batch:
                    if row["pool_address"] not in attrs:
                        continue  # lookup failed; next run retries while the window lasts
                    a = attrs[row["pool_address"]]
                    decision, reason = decide(row, a, p, open_s, day_pnl)
                    if decision == "control" and open_c >= CONTROL_MAX_OPEN:
                        decision, reason = "skip", "control_full"
                    stats["decided"][decision] = stats["decided"].get(decision, 0) + 1
                    if decision == "wait":
                        continue
                    _record(row, a, decision, reason, p, now)
                    open_s += decision == "enter"
                    open_c += decision == "control"
            for pos in _open_positions():
                age = int(now.timestamp()) - pos["meta"]["entry_candle_ts"]
                q = {"aggregate": 5, "limit": min(1000, age // 300 + 4)}
                r = await _get(client, f"{GT}/networks/solana/pools/{pos['meta']['pool']}/ohlcv/minute", q, deadline)
                await asyncio.sleep(GT_SLEEP)
                if r.status_code == 404:
                    candles = []
                else:
                    r.raise_for_status()
                    candles = _parse_ohlcv(r.json())
                trade = simulate(candles, pos["meta"]["entry_candle_ts"], p.exits, p.risk.hard_stop_pct,
                                 pos["meta"]["cost_pct"], HORIZON_S, data_until_ts=int(now.timestamp()))
                state = _apply(pos, trade, now)
                stats["positions"][state] = stats["positions"].get(state, 0) + 1
                if time.monotonic() >= deadline:
                    raise BudgetExhausted
        except BudgetExhausted:
            log.warning("paper.budget_exhausted", **stats)
    log.info("paper.done", **stats)
    return stats


if __name__ == "__main__":
    print(asyncio.run(run()))
