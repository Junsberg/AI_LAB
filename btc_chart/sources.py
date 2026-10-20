"""Exchange fetchers (stdlib only, so the chart runs anywhere Python does).

Every CVD source returns ``{hour_open_ms: net_taker_buy_usd}``. Price candles come from
Binance spot via the ``data-api.binance.vision`` market-data mirror.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

HOUR = 3_600_000
_UA = {"User-Agent": "btc-chart/0.1"}

BINANCE_SPOT_KLINES = "https://data-api.binance.vision/api/v3/klines"
BINANCE_FUT_KLINES = "https://fapi.binance.com/fapi/v1/klines"
COINBASE_TRADES = "https://api.exchange.coinbase.com/products/BTC-USD/trades"
OKX_TAKER_VOLUME = "https://www.okx.com/api/v5/rubik/stat/taker-volume"


class SourceError(RuntimeError):
    """A source could not be fetched (geo-block, rate limit, bad payload)."""


class Candle(NamedTuple):
    t: int  # open time, ms
    open: float
    high: float
    low: float
    close: float
    qv: float  # quote volume
    tbq: float  # taker-buy quote volume


def _http_error(e: urllib.error.HTTPError, url: str) -> str:
    raw = e.read().decode("utf-8", "replace")
    try:
        msg = json.loads(raw).get("msg") or raw
    except (ValueError, AttributeError):
        msg = raw
    msg = " ".join(str(msg).split())[:90]
    return f"HTTP {e.code} {urllib.parse.urlsplit(url).netloc}: {msg}"


def http_json(url: str, params: dict[str, Any] | None = None, retries: int = 4) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise SourceError(_http_error(e, url)) from e
            last = e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
        time.sleep(2**attempt)
    raise SourceError(f"{urllib.parse.urlsplit(url).netloc}: {last}")


def binance_klines(url: str, symbol: str, interval: str, limit: int) -> list[Candle]:
    rows = http_json(url, {"symbol": symbol, "interval": interval, "limit": limit})
    return [
        Candle(
            int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7]), float(r[10])
        )
        for r in rows
    ]


def klines_cvd(candles: list[Candle]) -> dict[int, float]:
    """Net taker-buy USD per bar: buyers' quote volume minus sellers' (total - buy)."""
    return {c.t: 2 * c.tbq - c.qv for c in candles}


def okx_cvd() -> dict[int, float]:
    """OKX BTC contracts, hourly. Row layout per OKX docs: [ts, sellVol, buyVol]."""
    res = http_json(OKX_TAKER_VOLUME, {"ccy": "BTC", "instType": "CONTRACTS", "period": "1H"})
    if res.get("code") != "0":
        raise SourceError(f"okx: {res.get('msg')}")
    return {int(ts): float(buy) - float(sell) for ts, sell, buy in res["data"]}


def _iso_ms(s: str) -> int:
    return int(datetime.fromisoformat(s).timestamp() * 1000)


def _load_cache(path: Path) -> dict[int, float]:
    try:
        return {int(k): v for k, v in json.loads(path.read_text()).items()}
    except (OSError, ValueError):
        return {}


def coinbase_cvd(
    hours: int, cache_path: Path, now_ms: int, pause: float = 0.12
) -> dict[int, float]:
    """Coinbase BTC-USD net taker-buy USD per hour, aggregated from raw trades.

    Coinbase's ``side`` is the *maker* side, so a ``sell`` print is a taker buy.
    Completed hours are cached on disk; only the missing tail is fetched on later runs.
    """
    cur_hour = now_ms // HOUR * HOUR
    want_start = cur_hour - hours * HOUR
    cache = _load_cache(cache_path)
    start = want_start
    while start in cache and start < cur_hour:
        start += HOUR

    deltas: dict[int, float] = defaultdict(float)
    after: int | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 1000}
        if after is not None:
            params["after"] = after
        page = http_json(COINBASE_TRADES, params)
        if not page:
            break
        oldest = page[0]
        for tr in page:
            t_ms = _iso_ms(tr["time"])
            if t_ms >= start:
                usd = float(tr["size"]) * float(tr["price"])
                deltas[t_ms // HOUR * HOUR] += usd if tr["side"] == "sell" else -usd
            if tr["trade_id"] < oldest["trade_id"]:
                oldest = tr
        pages += 1
        if pages % 50 == 0:
            print(f"  coinbase: {pages} pages, back to {oldest['time']}", file=sys.stderr)
        if _iso_ms(oldest["time"]) < start:
            break
        after = oldest["trade_id"]
        time.sleep(pause)

    for h, d in deltas.items():
        if h < cur_hour:
            cache[h] = d
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({str(h): v for h, v in sorted(cache.items())}))
    out = {h: v for h, v in cache.items() if h >= want_start}
    if cur_hour in deltas:
        out[cur_hour] = deltas[cur_hour]
    return out
