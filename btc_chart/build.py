"""Collect data, compute indicators, and write a self-contained ``index.html``."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import sources as src
from .indicators import cumulate, ema, find_fvgs, nearest
from .sources import HOUR, SourceError

EMA_PERIODS = (20, 50, 100, 200)
WARMUP = 400  # extra 1H bars so EMA200 has converged by the first visible candle
FVG_FRAMES = (("4H", "4h", 4 * HOUR, 500), ("1D", "1d", 24 * HOUR, 365))
HERE = Path(__file__).parent

# key, title, colour — order is the order of the panels on the page.
CVD_PANELS = (
    ("binance_spot", "Binance 현물 CVD", "#4d9d86"),
    ("binance_fut", "Binance 선물 CVD", "#b5546f"),
    ("coinbase", "Coinbase 현물 CVD", "#4a65a8"),
    ("okx", "OKX 선물 CVD", "#b98a3c"),
)


def collect_cvd(key: str, hours: int, now_ms: int, out_dir: Path, spot_bars: list[src.Candle]):
    if key == "binance_spot":
        return src.klines_cvd(spot_bars)
    if key == "binance_fut":
        bars = src.binance_klines(src.BINANCE_FUT_KLINES, "BTCUSDT", "1h", min(hours + 2, 1000))
        return src.klines_cvd(bars)
    if key == "coinbase":
        return src.coinbase_cvd(hours, out_dir / "cache" / "coinbase_hourly.json", now_ms)
    if key == "okx":
        return src.okx_cvd()
    raise ValueError(key)


def build(hours: int, out_path: Path, utc_offset: int, skip: set[str], fvg_per_tf: int) -> dict:
    now_ms = int(time.time() * 1000)
    out_dir = out_path.parent

    bars = src.binance_klines(src.BINANCE_SPOT_KLINES, "BTCUSDT", "1h", min(hours + WARMUP, 1000))
    closes = [b.close for b in bars]
    view = bars[-hours:]
    hour_ts = [b.t for b in view]
    emas = {str(n): ema(closes, n)[-hours:] for n in EMA_PERIODS}

    fvgs = []
    last_price = bars[-1].close
    for label, interval, tf_ms, limit in FVG_FRAMES:
        candles = src.binance_klines(src.BINANCE_SPOT_KLINES, "BTCUSDT", interval, limit)
        zones = find_fvgs(candles, label, tf_ms, now_ms)
        fvgs += [z._asdict() for z in nearest(zones, last_price, fvg_per_tf)]

    panels = []
    for key, title, color in CVD_PANELS:
        error, values = None, None
        if key in skip:
            error = "건너뜀"
        else:
            try:
                deltas = collect_cvd(key, hours, now_ms, out_dir, bars)
                values = cumulate(deltas, hour_ts)
            except SourceError as e:
                error = str(e)
                print(f"[{key}] {error}", file=sys.stderr)
        panels.append(
            {"key": key, "title": title, "color": color, "values": values, "error": error}
        )

    tz = timezone(timedelta(hours=utc_offset))
    payload = {
        "generated": datetime.now(tz).strftime("%Y. %m. %d. %H:%M:%S"),
        "tz": f"UTC{utc_offset:+d}",
        "utc_offset": utc_offset,
        "candles": [[b.t, b.open, b.high, b.low, b.close] for b in view],
        "ema": emas,
        "fvg": fvgs,
        "cvd": panels,
    }
    html = (HERE / "template.html").read_text(encoding="utf-8")
    # "</" is escaped so exchange error text can never close the <script> block early.
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    echarts_js = (HERE / "vendor" / "echarts.min.js").read_text(encoding="utf-8")
    html = html.replace("/*__ECHARTS__*/", echarts_js).replace("/*__DATA__*/null", data)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="btc_chart", description="Personal BTC chart builder")
    ap.add_argument("--hours", type=int, default=72, help="visible 1H candles (default 72)")
    ap.add_argument("--out", type=Path, default=HERE / "out" / "index.html")
    ap.add_argument("--utc-offset", type=int, default=9, help="label timezone (default KST +9)")
    ap.add_argument("--skip", default="", help="comma list of CVD keys to skip, e.g. coinbase")
    ap.add_argument("--fvg-per-tf", type=int, default=6, help="FVG zones kept per timeframe")
    args = ap.parse_args(argv)

    payload = build(
        args.hours,
        args.out,
        args.utc_offset,
        {k for k in args.skip.split(",") if k},
        args.fvg_per_tf,
    )
    missing = [p["key"] for p in payload["cvd"] if p["values"] is None]
    print(f"wrote {args.out}" + (f" (no data: {', '.join(missing)})" if missing else ""))


if __name__ == "__main__":
    main()
