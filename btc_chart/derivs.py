"""Open-interest candles (and the stablecoin supply series) for the page.

* OI candles: Binance publishes 5-minute open interest as daily files on its public data archive
  (data.binance.vision, ``futures/um/daily/metrics``). The build downloads the last ``days`` days once
  (completed days are cached on disk), tops up the newest ~41 h from the live API and aggregates the 5 m
  points into open/high/low/close per chart timeframe. The page extends the newest candles itself.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import sources as src

ARCHIVE = "https://data.binance.vision/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-{day}.zip"
BINANCE_OI = "https://fapi.binance.com/futures/data/openInterestHist"
LLAMA_STABLE_CHART = "https://stablecoins.llama.fi/stablecoincharts/all"

MIN = 60_000
OI_TFS = {"15m": 15 * MIN, "1h": 60 * MIN, "4h": 240 * MIN, "1d": 1440 * MIN}
KEEP = 1000            # candles kept per timeframe (the page loads 1000 per timeframe)
DAY_ROWS = 280         # a day with fewer 5 m rows than this is incomplete and not cached


# ---------------------------------------------------------------- OI


def parse_metrics_csv(text: str) -> list[list[float]]:
    """Daily metrics CSV -> [[t_ms, open_interest_btc], ...] (create_time is UTC)."""
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return []
    head = lines[0].split(",")
    ti, oi = head.index("create_time"), head.index("sum_open_interest")
    out = []
    for ln in lines[1:]:
        c = ln.split(",")
        t = datetime.strptime(c[ti], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        out.append([int(t.timestamp() * 1000), float(c[oi])])
    return out


def ohlc(rows: list[list[float]], tf_ms: int) -> list[list[float]]:
    """5 m points -> [[bucket_open_ms, o, h, l, c], ...] on UTC-aligned buckets (like the exchange klines)."""
    out: dict[int, list[float]] = {}
    for t, v in sorted(rows):
        b = t - t % tf_ms
        if b not in out:
            out[b] = [b, v, v, v, v]
        else:
            r = out[b]
            r[2], r[3], r[4] = max(r[2], v), min(r[3], v), v
    return [out[b] for b in sorted(out)]


def _day_rows(day: str, cache: Path, fetch: Callable[[str], bytes | None]) -> list[list[float]]:
    f = cache / f"{day}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    raw = fetch(day)
    if raw is None:
        return []
    z = zipfile.ZipFile(io.BytesIO(raw))
    rows = parse_metrics_csv(z.read(z.namelist()[0]).decode())
    if len(rows) >= DAY_ROWS:
        cache.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(rows), encoding="utf-8")
    return rows


def _download_day(day: str) -> bytes | None:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(urllib.request.Request(ARCHIVE.format(day=day), headers=src._UA), timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):   # not published yet
            return None
        raise


def oi_5m(days: int, cache_dir: Path, now_ms: int, fetch: Callable[[str], bytes | None] = _download_day,
          live: Callable[[int | None], list[list[float]]] | None = None) -> list[list[float]]:
    today = datetime.fromtimestamp(now_ms / 1000, timezone.utc).date()
    ds = [(today - timedelta(days=i)).isoformat() for i in range(days, 0, -1)]
    with ThreadPoolExecutor(8) as ex:
        parts = list(ex.map(lambda d: _day_rows(d, cache_dir / "oi5m", fetch), ds))
    rows = {int(t): v for p in parts for t, v in p}
    since = max(rows) if rows else None
    for t, v in (live or _live_5m)(since):   # the live API fills everything after the newest archive day
        rows[int(t)] = v
    return [[t, rows[t]] for t in sorted(rows)]


def _live_5m(since: int | None = None) -> list[list[float]]:
    """5 m open interest from ``since`` (exclusive) to now, 500 rows per call (the API reaches back 30 days)."""
    out: list[list[float]] = []
    start = since
    for _ in range(12):
        p: dict[str, Any] = {"symbol": "BTCUSDT", "period": "5m", "limit": 500}
        if start:
            p["startTime"] = start + 1
        res = src.http_json(BINANCE_OI, p)
        rows = [[int(r["timestamp"]), float(r["sumOpenInterest"])] for r in res]
        out += rows
        if len(rows) < 500 or not start:
            break
        start = rows[-1][0]
    return out


def oi_candles(days: int, cache_dir: Path, now_ms: int, **kw: Any) -> dict[str, list[list[float]]]:
    rows = oi_5m(days, cache_dir, now_ms, **kw)
    return {tf: [[r[0]] + [round(x, 1) for x in r[1:]] for r in ohlc(rows, ms)[-KEEP:]] for tf, ms in OI_TFS.items()}


# ---------------------------------------------------------------- stablecoin liquidity


def parse_stable_chart(rows: list[dict], keep: int = 1100) -> list[list[float]]:
    """DefiLlama daily totals -> [[day_ms, USD-pegged stablecoin supply in billions], ...] (newest ``keep`` days)."""
    out = []
    for r in rows:
        v = (r.get("totalCirculatingUSD") or {}).get("peggedUSD")
        if v:
            out.append([int(r["date"]) * 1000, round(v / 1e9, 2)])
    return out[-keep:]


def stable_supply() -> list[list[float]]:
    return parse_stable_chart(src.http_json(LLAMA_STABLE_CHART))


# ---------------------------------------------------------------- entry point


def collect(cache_dir: Path, now_ms: int, skip: set[str] | None = None, days: int = 200) -> dict[str, Any]:
    out: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, fn in (("oi", lambda: oi_candles(days, cache_dir, now_ms)), ("stable", stable_supply)):
        if skip and name in skip:
            continue
        try:
            data = fn()
            if data:
                out[name] = data
            else:
                errors[name] = "no data"
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {str(e)[:120]}"
            print(f"[derivs:{name}] {errors[name]}", file=sys.stderr)
    out["errors"] = errors
    return out
