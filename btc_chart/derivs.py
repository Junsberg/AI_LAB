"""Open-interest candles and an exchange-averaged funding rate, the way TradingView shows them.

* OI candles: Binance publishes 5-minute open interest as daily files on its public data archive
  (data.binance.vision, ``futures/um/daily/metrics``). The build downloads the last ``days`` days once
  (completed days are cached on disk), tops up the newest ~41 h from the live API and aggregates the 5 m
  points into open/high/low/close per chart timeframe. The page extends the newest candles itself.
* Funding: Binance, Bybit and OKX BTC-USDT perpetual settlement history, averaged per 8 h settlement.
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
BINANCE_FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"
BYBIT_FUNDING = "https://api.bybit.com/v5/market/funding/history"
OKX_FUNDING = "https://www.okx.com/api/v5/public/funding-rate-history"

MIN = 60_000
OI_TFS = {"15m": 15 * MIN, "1h": 60 * MIN, "4h": 240 * MIN, "1d": 1440 * MIN}
KEEP = 1000            # candles kept per timeframe (the page loads 1000 per timeframe)
FUNDING_ROWS = 450     # settlements kept (~150 days)
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


# ---------------------------------------------------------------- funding


def _bybit() -> dict[int, float]:
    out: dict[int, float] = {}
    end = None
    for _ in range(4):
        p: dict[str, Any] = {"category": "linear", "symbol": "BTCUSDT", "limit": 200}
        if end:
            p["endTime"] = end
        lst = (src.http_json(BYBIT_FUNDING, p).get("result") or {}).get("list") or []
        if not lst:
            break
        for r in lst:
            out[int(r["fundingRateTimestamp"]) // MIN * MIN] = float(r["fundingRate"]) * 100
        end = min(int(r["fundingRateTimestamp"]) for r in lst) - 1
    return out


def _okx() -> dict[int, float]:
    out: dict[int, float] = {}
    after = None
    for _ in range(5):
        p: dict[str, Any] = {"instId": "BTC-USDT-SWAP", "limit": 100}
        if after:
            p["after"] = after
        lst = src.http_json(OKX_FUNDING, p).get("data") or []
        if not lst:
            break
        for r in lst:
            out[int(r["fundingTime"]) // MIN * MIN] = float(r["realizedRate"] or r["fundingRate"]) * 100
        after = min(int(r["fundingTime"]) for r in lst)
    return out


def _binance() -> dict[int, float]:
    res = src.http_json(BINANCE_FUNDING, {"symbol": "BTCUSDT", "limit": 1000})
    return {int(r["fundingTime"]) // MIN * MIN: float(r["fundingRate"]) * 100 for r in res}


def average_funding(series: dict[str, dict[int, float]], keep: int = FUNDING_ROWS) -> dict[str, Any]:
    """Mean rate (%) per settlement time over the exchanges that have it; newest ``keep`` settlements."""
    times = sorted({t for s in series.values() for t in s})
    rows = []
    for t in times:
        vals = [s[t] for s in series.values() if t in s]
        rows.append([t, round(sum(vals) / len(vals), 5), len(vals)])
    return {"rows": rows[-keep:], "exchanges": sorted(series)}


def funding(fetchers: dict[str, Callable[[], dict[int, float]]] | None = None) -> dict[str, Any]:
    series: dict[str, dict[int, float]] = {}
    for name, fn in (fetchers or {"binance": _binance, "bybit": _bybit, "okx": _okx}).items():
        try:
            data = fn()
            if data:
                series[name] = data
        except Exception as e:   # one exchange down only thins the average
            print(f"[derivs:funding:{name}] {type(e).__name__}: {str(e)[:100]}", file=sys.stderr)
    return average_funding(series) if series else {}


# ---------------------------------------------------------------- entry point


def collect(cache_dir: Path, now_ms: int, skip: set[str] | None = None, days: int = 200) -> dict[str, Any]:
    out: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, fn in (("oi", lambda: oi_candles(days, cache_dir, now_ms)), ("funding", funding)):
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
