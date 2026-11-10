"""Order-size CVD from Binance USDT-M BTCUSDT aggregated trades.

Binance publishes every aggregated trade as a daily file on its public archive (yesterday and older). Each
trade is classified by its USD size ($100-1k, 1k-10k, 10k-100k, 100k-1M, 1M+), aggregated per hour and
cached per day (the raw files are 17 MB each and are not kept). The days since the archive's newest file come
from the live ``aggTrades`` endpoint, paged by id with a time budget per build, so the build never stalls: the
cursor persists and the chart catches up over the next builds (or run ``python -m btc_chart.flow`` once).
An aggregated trade merges the fills of one taker order at one price, so its size is a good proxy for order size.
"""

from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import sources as src

ARCHIVE = "https://data.binance.vision/data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-{day}.zip"
AGG_TRADES = "https://fapi.binance.com/fapi/v1/aggTrades"
HOUR = 3_600_000
EDGES = [100.0, 1_000.0, 10_000.0, 100_000.0, 1_000_000.0]   # USD; below the first edge only counts in "all"
LABELS = ["$100 – $1k", "$1k – $10k", "$10k – $100k", "$100k – $1M", "$1M +"]
BIG = LABELS[2:]       # the buckets the page shows: orders large enough to move the market
DAYS = 90


def size_bucket(usd: float) -> int:
    """-1 for tiny orders, 0..4 for the size buckets (the last one is open-ended)."""
    return bisect_right(EDGES, usd) - 1


def _hour() -> dict[str, Any]:
    return {"all": [0.0, 0.0], "b": [[0.0, 0.0] for _ in LABELS]}


def add_trade(hours: dict[int, dict[str, Any]], ts: int, price: float, qty: float, buyer_maker: bool) -> None:
    usd = price * qty
    h = hours.get(ts - ts % HOUR)
    if h is None:
        h = hours[ts - ts % HOUR] = _hour()
    side = 1 if buyer_maker else 0           # buyer is the maker -> the taker sold
    h["all"][side] += usd
    b = size_bucket(usd)
    if b >= 0:
        h["b"][b][side] += usd


def aggregate_csv(lines: Iterable[str]) -> tuple[dict[int, dict[str, Any]], int]:
    """Archive CSV lines -> ({hour_ms: bucket sums}, last agg_trade_id)."""
    hours: dict[int, dict[str, Any]] = {}
    last = 0
    for ln in lines:
        c = ln.split(",")
        if not c[0][:1].isdigit():             # header
            continue
        add_trade(hours, int(c[5]), float(c[1]), float(c[2]), c[6].strip() == "true")
        last = int(c[0])
    return hours, last


# ---------------------------------------------------------------- cache & archive


def _download(day: str) -> bytes | None:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(urllib.request.Request(ARCHIVE.format(day=day), headers=src._UA), timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise


def load_day(day: str, cache: Path, fetch: Callable[[str], bytes | None] = _download) -> dict[str, Any] | None:
    f = cache / f"{day}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    raw = fetch(day)
    if raw is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(raw))
    with z.open(z.namelist()[0]) as fh:
        hours, last = aggregate_csv(io.TextIOWrapper(fh, encoding="utf-8"))
    out = {"hours": {str(k): v for k, v in hours.items()}, "last_id": last}
    cache.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(out), encoding="utf-8")
    return out


# ---------------------------------------------------------------- live catch-up


def _rest(from_id: int) -> list[dict]:
    return src.http_json(AGG_TRADES, {"symbol": "BTCUSDT", "fromId": from_id, "limit": 1000})


def catch_up(live: dict[str, Any], start_id: int, now_ms: int, budget_sec: float, rest: Callable[[int], list[dict]] = _rest,
             sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> int:
    """Page aggTrades from ``live['cursor']`` (or ``start_id``) into ``live['hours']`` until now or the budget ends."""
    hours = {int(k): v for k, v in live["hours"].items()}
    cursor = live.get("cursor") or start_id
    deadline = clock() + budget_sec
    calls = 0
    while clock() < deadline:
        rows = rest(cursor)
        calls += 1
        if not rows:
            break
        for r in rows:
            add_trade(hours, int(r["T"]), float(r["p"]), float(r["q"]), bool(r["m"]))
        cursor = int(rows[-1]["a"]) + 1
        if len(rows) < 1000 or int(rows[-1]["T"]) >= now_ms - 2000:
            break
        sleep(0.5)   # aggTrades weighs 20: stay under the 2400/min limit
    live["hours"] = {str(k): v for k, v in hours.items()}
    live["cursor"] = cursor
    return calls


# ---------------------------------------------------------------- the chart payload


def build_payload(hours: dict[int, dict[str, Any]], now_ms: int, days: int = DAYS) -> dict[str, Any]:
    end = now_ms - now_ms % HOUR
    ts = [end - i * HOUR for i in range(days * 24 - 1, -1, -1)]
    ts = [t for t in ts if t in hours]
    if len(ts) < 24:
        return {}

    def cum(get: Callable[[dict[str, Any]], float]) -> list[float]:
        out, acc = [], 0.0
        for t in ts:
            acc += get(hours[t])
            out.append(round(acc / 1e6, 3))   # USD millions, cumulative from the first hour; the page re-bases it to the visible range
        return out

    series = {LABELS[i]: cum(lambda h, i=i: h["b"][i][0] - h["b"][i][1]) for i in (2, 3, 4)}
    return {"asof": ts[-1] + HOUR, "t": ts, "series": series, "labels": list(BIG)}


def collect(cache_dir: Path, now_ms: int, budget_sec: float = 240.0, days: int = DAYS, *,
            fetch: Callable[[str], bytes | None] = _download, rest: Callable[[int], list[dict]] = _rest,
            sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    cache = cache_dir / "flow"
    today = datetime.fromtimestamp(now_ms / 1000, timezone.utc).date()
    day_list = [(today - timedelta(days=i)).isoformat() for i in range(days, 0, -1)]
    merged: dict[int, dict[str, Any]] = {}
    last_id = 0
    for day in day_list:
        d = load_day(day, cache, fetch)
        if not d:
            continue
        last_id = max(last_id, d["last_id"])
        merged.update({int(k): v for k, v in d["hours"].items()})
    lf = cache / "live.json"
    try:
        live = json.loads(lf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        live = {"hours": {}, "cursor": 0}
    if last_id:
        calls = catch_up(live, last_id + 1, now_ms, budget_sec, rest, sleep) if now_ms else 0
        cache.mkdir(parents=True, exist_ok=True)
        lf.write_text(json.dumps(live), encoding="utf-8")
        print(f"[flow] live catch-up: {calls} calls, cursor {live['cursor']}", file=sys.stderr)
    for k, v in live["hours"].items():
        merged.setdefault(int(k), v)                     # the archive wins where both exist
    keep = {t for t in merged if t >= now_ms - (days + 1) * 24 * HOUR}
    return build_payload({t: merged[t] for t in keep}, now_ms, days)


if __name__ == "__main__":   # one-off: python -m btc_chart.flow  -> catch up fully (long budget) and report
    here = Path(__file__).parent / "out"
    now = int(time.time() * 1000)
    res = collect(here / "cache", now, budget_sec=3600)
    print("hours:", len(res.get("t", [])), "asof:", res.get("asof"))
