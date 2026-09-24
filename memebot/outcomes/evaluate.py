"""Outcome evaluator: for tokens older than 24h (re-check at 72h), pull GeckoTerminal
hourly OHLCV + pool reserve and classify. Free API, ~2 calls per token.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import structlog

from memebot.db import conn
from memebot.outcomes.rules import Candle, classify

log = structlog.get_logger()
GT = "https://api.geckoterminal.com/api/v2"


GT_SLEEP = 2.2  # ~27 req/min against the ~30/min public limit


async def _ohlcv(client: httpx.AsyncClient, pool: str, age_hours: float) -> list[Candle]:
    # hourly candles cover 7 days; older tokens need daily candles so the launch
    # candle (our base price) is still inside the window
    tf, limit = ("hour", 168) if age_hours <= 160 else ("day", 100)
    r = await client.get(f"{GT}/networks/solana/pools/{pool}/ohlcv/{tf}", params={"limit": limit})
    if r.status_code == 404:
        return []
    r.raise_for_status()
    rows = r.json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    rows.sort(key=lambda x: x[0])
    return [Candle(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows]


async def _pool(client: httpx.AsyncClient, pool: str) -> dict:
    r = await client.get(f"{GT}/networks/solana/pools/{pool}")
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json().get("data", {}).get("attributes", {}) or {}


async def run(limit: int = 80) -> int:
    with conn() as c:
        rows = c.execute(
            """select t.mint, t.pool_address, t.created_at, o.evaluated_at, o.peak_mcap_usd as liq_peak
               from tokens t left join token_outcomes o on o.mint = t.mint
               where t.pool_address is not null
                 and t.created_at < now() - interval '24 hours'
                 and (o.mint is null
                      or (o.evaluated_at < t.created_at + interval '72 hours'
                          and t.created_at < now() - interval '72 hours'))
               order by t.created_at limit %s""",
            (limit,),
        ).fetchall()
    if not rows:
        return 0
    n = 0
    backoff = 0
    async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"}) as client:
        for r in rows:
            age_h = (datetime.now(timezone.utc) - r["created_at"]).total_seconds() / 3600
            try:
                candles = await _ohlcv(client, r["pool_address"], age_h)
                await asyncio.sleep(GT_SLEEP)
                attrs = await _pool(client, r["pool_address"])
                await asyncio.sleep(GT_SLEEP)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    backoff += 1
                    log.warning("outcome.rate_limited", backoff=backoff)
                    if backoff > 3:
                        break
                    await asyncio.sleep(60)
                    continue
                log.warning("outcome.fetch_failed", mint=r["mint"], error=str(e))
                continue
            except httpx.HTTPError as e:
                log.warning("outcome.fetch_failed", mint=r["mint"], error=str(e))
                continue
            raw_liq = attrs.get("reserve_in_usd")
            liq_now = None if raw_liq is None else float(raw_liq)  # 0.0 is a real (drained) value
            # v1 proxy for peak liquidity: max(previous stored, now). Improves as we re-evaluate.
            liq_peak = max(float(r["liq_peak"] or 0), liq_now or 0.0) or None
            price_now = float(attrs.get("base_token_price_usd") or 0) or None
            o = classify(candles, liq_now, liq_peak, price_now)
            with conn() as c:
                c.execute(
                    """insert into token_outcomes(mint, peak_mcap_usd, peak_at, peak_multiple, rugged,
                                                  rug_at, rug_reason, evaluated_at)
                       values (%s,%s,%s,%s,%s,%s,%s,now())
                       on conflict (mint) do update set
                         peak_mcap_usd=excluded.peak_mcap_usd, peak_at=excluded.peak_at,
                         peak_multiple=excluded.peak_multiple, rugged=excluded.rugged,
                         rug_at=coalesce(token_outcomes.rug_at, excluded.rug_at),
                         rug_reason=excluded.rug_reason, evaluated_at=now()""",
                    (
                        r["mint"],
                        liq_peak,  # NOTE: column reused as liquidity peak proxy in v1
                        datetime.fromtimestamp(o.peak_ts, tz=timezone.utc) if o.peak_ts else None,
                        o.peak_multiple,
                        o.rugged,
                        datetime.now(timezone.utc) if o.rugged else None,
                        o.rug_reason,
                    ),
                )
                c.commit()
            n += 1
    log.info("outcomes.done", evaluated=n)
    return n


if __name__ == "__main__":
    print(asyncio.run(run()))
