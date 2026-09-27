"""Outcome evaluator: for tokens older than 24h (re-check at 72h), pull GeckoTerminal
OHLCV + pool reserve and classify. The first 24h use 5-minute candles (a first-hour
pump sits inside the first hourly candle, i.e. inside the base price), later hours
hourly/daily. Free API, ~2.03 calls per token (pool attributes come 30 at a time
from the `pools/multi` endpoint).

Rate limits: GitHub-hosted runners share egress with other GeckoTerminal users, so
429s arrive well under the documented 30 req/min. A 429 never drops a token: we
sleep (Retry-After, else 60s escalating) and retry the same request, and the run
stops on a wall-clock budget rather than a retry count.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx
import structlog

from memebot.db import conn
from memebot.outcomes.rules import Candle, classify

log = structlog.get_logger()
GT = "https://api.geckoterminal.com/api/v2"


GT_SLEEP = 2.2  # ~27 req/min against the ~30/min public limit
MULTI_BATCH = 30  # GeckoTerminal pools/multi cap
RUN_BUDGET_S = 20 * 60  # workflow timeout is 25 min
RETRY_MIN_S, RETRY_DEFAULT_S, RETRY_MAX_S = 5.0, 60.0, 180.0
# Rows evaluated before this instant were classified by older rules (v1 wick peaks /
# v2 hourly-only first day, which hid first-hour pumps inside the base candle) and are
# re-evaluated once. Bump when classify() changes in a way that alters
# stored values; there is no schema column for a rules version on purpose (no migration).
RULES_CHANGED_AT = "2026-09-27T18:05:00+00:00"  # v3: first 24h from 5-minute candles
FIRST_DAY_S = 24 * 3600


class BudgetExhausted(Exception):
    """Raised when a 429 pause would run past the job's time budget."""


def _retry_after(headers: httpx.Headers, fallback: float) -> float:
    ra = headers.get("Retry-After")
    try:
        pause = float(ra) if ra else fallback
    except ValueError:
        pause = fallback
    return min(max(pause, RETRY_MIN_S), RETRY_MAX_S)


async def _get(
    client: httpx.AsyncClient, url: str, params: dict | None, deadline: float
) -> httpx.Response:
    """GET that waits out 429s (same request retried) until `deadline` (monotonic)."""
    wait = RETRY_DEFAULT_S
    while True:
        r = await client.get(url, params=params)
        if r.status_code != 429:
            return r
        pause = _retry_after(r.headers, wait)
        if time.monotonic() + pause > deadline:
            raise BudgetExhausted
        log.warning("outcome.rate_limited", pause=pause)
        await asyncio.sleep(pause)
        wait = min(wait * 1.5, RETRY_MAX_S)


def _parse_ohlcv(payload: dict) -> list[Candle]:
    rows = (payload or {}).get("data", {}).get("attributes", {}).get("ohlcv_list", []) or []
    rows = [x for x in rows if isinstance(x, list) and len(x) >= 6 and all(v is not None for v in x[:6])]
    rows.sort(key=lambda x: x[0])
    return [Candle(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows]


async def _ohlcv(client: httpx.AsyncClient, pool: str, age_hours: float, deadline: float) -> list[Candle]:
    # hourly candles cover 7 days; older tokens need daily candles so the launch
    # candle (our base price) is still inside the window
    tf, limit = ("hour", 168) if age_hours <= 160 else ("day", 100)
    r = await _get(client, f"{GT}/networks/solana/pools/{pool}/ohlcv/{tf}", {"limit": limit}, deadline)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return _parse_ohlcv(r.json())


async def _ohlcv_first_day(
    client: httpx.AsyncClient, pool: str, created_at: datetime, deadline: float
) -> list[Candle]:
    """5-minute candles for the pool's first FIRST_DAY_S. Anchored with before_timestamp,
    so the window is the launch day whatever the token's age (limit alone would slide)."""
    params = {
        "aggregate": 5,
        "before_timestamp": int(created_at.timestamp()) + FIRST_DAY_S,
        "limit": FIRST_DAY_S // 300,
    }
    r = await _get(client, f"{GT}/networks/solana/pools/{pool}/ohlcv/minute", params, deadline)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return _parse_ohlcv(r.json())


def merge_candles(fine: list[Candle], coarse: list[Candle], cutoff_ts: int) -> list[Candle]:
    """5-minute candles before `cutoff_ts`, hourly/daily after. Without fine data the
    coarse series is returned as is (pre-v3 behaviour), never an empty list."""
    fine = [c for c in fine if c.ts < cutoff_ts]
    if not fine:
        return coarse
    return fine + [c for c in coarse if c.ts >= cutoff_ts]


def parse_multi(payload: dict) -> dict[str, dict]:
    """pools/multi → {pool_address: attributes}. Unknown pools are simply absent."""
    out: dict[str, dict] = {}
    for d in (payload or {}).get("data", []) or []:
        a = (d or {}).get("attributes") or {}
        addr = a.get("address")
        if isinstance(addr, str) and addr:
            out[addr] = a
    return out


async def _pools(client: httpx.AsyncClient, pools: list[str], deadline: float) -> dict[str, dict]:
    """Attributes for up to MULTI_BATCH pools in one call. Pools the batch endpoint
    leaves out (delisted, or a shape we did not expect) fall back to single lookups,
    so the worst case is the old one-call-per-pool behaviour, never a silent skip."""
    found: dict[str, dict] = {}
    try:
        r = await _get(client, f"{GT}/networks/solana/pools/multi/{','.join(pools)}", None, deadline)
        if r.status_code != 404:
            r.raise_for_status()
            found = parse_multi(r.json())
        await asyncio.sleep(GT_SLEEP)
    except (httpx.HTTPError, TypeError, ValueError) as e:  # HTTPError covers timeouts too
        log.warning("outcome.multi_failed", n=len(pools), error=str(e))
    for pool in pools:
        if pool in found:
            continue
        try:
            r = await _get(client, f"{GT}/networks/solana/pools/{pool}", None, deadline)
            await asyncio.sleep(GT_SLEEP)
            if r.status_code == 404:
                found[pool] = {}  # delisted pool: classify() gets no liquidity/price, as before
                continue
            r.raise_for_status()
            found[pool] = r.json().get("data", {}).get("attributes", {}) or {}
        except (httpx.HTTPError, TypeError, ValueError) as e:
            # One bad pool must not sink the other 29: leave it out so its token is skipped.
            log.warning("outcome.pool_failed", pool=pool, error=str(e))
    return found


def _num(v) -> float:
    try:
        return max(float(v), 0.0) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def liquidity_peak(stored, seen, now) -> float | None:
    """Peak-liquidity proxy: max(previous stored, reserve when collected, now).
    Without the collect-time floor a pool drained before its first evaluation had
    peak == now and could never be an lp_pull (2 of 462 rugs on 09-27)."""
    return max(_num(stored), _num(seen), _num(now)) or None


def _upsert(mint: str, liq_peak: float | None, o) -> None:
    with conn() as c:
        c.execute(
            """insert into token_outcomes(mint, peak_mcap_usd, peak_at, peak_multiple, rugged,
                                          rug_at, rug_reason, evaluated_at)
               values (%s,%s,%s,%s,%s,%s,%s,now())
               on conflict (mint) do update set
                 peak_mcap_usd=excluded.peak_mcap_usd, peak_at=excluded.peak_at,
                 peak_multiple=excluded.peak_multiple, rugged=excluded.rugged,
                 rug_at=case when excluded.rugged
                             then coalesce(token_outcomes.rug_at, excluded.rug_at) end,
                 rug_reason=excluded.rug_reason, evaluated_at=now()""",
            (
                mint,
                liq_peak,  # NOTE: column reused as liquidity peak proxy in v1
                datetime.fromtimestamp(o.peak_ts, tz=timezone.utc) if o.peak_ts else None,
                o.peak_multiple,
                o.rugged,
                datetime.now(timezone.utc) if o.rugged else None,
                o.rug_reason,
            ),
        )
        c.commit()


def pending_rows(limit: int) -> list[dict]:
    """Tokens due for (re-)evaluation: never evaluated first, then oldest."""
    with conn() as c:
        return c.execute(
            """select t.mint, t.pool_address, t.created_at, o.evaluated_at, o.peak_mcap_usd as liq_peak,
                      t.meta->>'reserve_usd_at_seen' as liq_seen
               from tokens t left join token_outcomes o on o.mint = t.mint
               where t.pool_address is not null
                 and t.created_at < now() - interval '24 hours'
                 and (o.mint is null
                      or o.evaluated_at < %s::timestamptz
                      or (o.evaluated_at < t.created_at + interval '72 hours'
                          and t.created_at < now() - interval '72 hours'))
               order by (o.mint is null) desc, t.created_at limit %s""",
            (RULES_CHANGED_AT, limit),
        ).fetchall()


async def run(limit: int = 400, budget_s: float = RUN_BUDGET_S) -> int:
    rows = pending_rows(limit)
    if not rows:
        return 0
    n = 0
    deadline = time.monotonic() + budget_s
    async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"}) as client:
        try:
            for start in range(0, len(rows), MULTI_BATCH):
                batch = rows[start : start + MULTI_BATCH]
                try:
                    attrs_by_pool = await _pools(client, [r["pool_address"] for r in batch], deadline)
                except (httpx.HTTPError, TypeError, ValueError) as e:
                    log.warning("outcome.pools_failed", n=len(batch), error=str(e))
                    continue
                for r in batch:
                    if time.monotonic() >= deadline:
                        raise BudgetExhausted  # stop cleanly, not via the workflow's kill
                    if r["pool_address"] not in attrs_by_pool:
                        continue  # pool lookup failed above; next run retries it
                    age_h = (datetime.now(timezone.utc) - r["created_at"]).total_seconds() / 3600
                    try:
                        coarse = await _ohlcv(client, r["pool_address"], age_h, deadline)
                        await asyncio.sleep(GT_SLEEP)
                        fine = await _ohlcv_first_day(client, r["pool_address"], r["created_at"], deadline)
                        await asyncio.sleep(GT_SLEEP)
                        cutoff = int(r["created_at"].timestamp()) + FIRST_DAY_S
                        candles = merge_candles(fine, coarse, cutoff)
                    except (httpx.HTTPError, TypeError, ValueError) as e:
                        log.warning("outcome.fetch_failed", mint=r["mint"], error=str(e))
                        continue
                    attrs = attrs_by_pool.get(r["pool_address"], {})
                    raw_liq = attrs.get("reserve_in_usd")
                    liq_now = None if raw_liq is None else float(raw_liq)  # 0.0 is a real (drained) value
                    liq_peak = liquidity_peak(r["liq_peak"], r["liq_seen"], liq_now)
                    price_now = float(attrs.get("base_token_price_usd") or 0) or None
                    _upsert(r["mint"], liq_peak, classify(candles, liq_now, liq_peak, price_now))
                    n += 1
        except BudgetExhausted:
            log.warning("outcomes.budget_exhausted", evaluated=n, pending=len(rows) - n)
    log.info("outcomes.done", evaluated=n, pending=len(rows) - n)
    return n


if __name__ == "__main__":
    print(asyncio.run(run()))
