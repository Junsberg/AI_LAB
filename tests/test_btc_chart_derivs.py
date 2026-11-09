import io
import json
import shutil
import subprocess
import zipfile

import pytest

from btc_chart import derivs

MIN = 60_000
NODE = shutil.which("node")


def test_parse_metrics_csv_utc_and_btc_amount():
    csv = ("create_time,symbol,sum_open_interest,sum_open_interest_value,x\n"
           "2026-11-06 00:00:00,BTCUSDT,94230.5,7183678283.6,1\n"
           "2026-11-06 00:05:00,BTCUSDT,94300.0,7190000000.0,1\n")
    rows = derivs.parse_metrics_csv(csv)
    assert rows == [[1793923200000, 94230.5], [1793923200000 + 5 * MIN, 94300.0]]
    assert derivs.parse_metrics_csv("") == []


def test_ohlc_buckets_are_utc_aligned_and_ordered():
    pts = [[0, 10], [5 * MIN, 12], [10 * MIN, 9], [15 * MIN, 11], [20 * MIN, 13]]
    assert derivs.ohlc(pts, 15 * MIN) == [[0, 10, 12, 9, 9], [15 * MIN, 11, 13, 11, 13]]
    assert derivs.ohlc(list(reversed(pts)), 15 * MIN) == derivs.ohlc(pts, 15 * MIN)
    assert derivs.ohlc([], MIN) == []


def _zip_day(day: str, n: int = 288, base: float = 100.0) -> bytes:
    lines = ["create_time,symbol,sum_open_interest,sum_open_interest_value"]
    t0 = int(__import__("datetime").datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=__import__("datetime").timezone.utc).timestamp())
    for i in range(n):
        ts = __import__("datetime").datetime.fromtimestamp(t0 + i * 300, __import__("datetime").timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{ts},BTCUSDT,{base + i},0")
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(f"BTCUSDT-metrics-{day}.csv", "\n".join(lines))
    return b.getvalue()


def test_oi_history_caches_complete_days_skips_missing_and_fills_the_gap_from_live(tmp_path):
    calls = []

    def fetch(day):
        calls.append(day)
        if day == "2026-11-05":
            return None                      # not published (or 404)
        return _zip_day(day, n=288 if day != "2026-11-06" else 100)   # the 06th is incomplete

    live_calls = []

    def live(since):
        live_calls.append(since)
        return [[since + 5 * MIN, 999.0]]

    now = 1794225600000   # 2026-11-09 12:00 UTC
    rows = derivs.oi_5m(4, tmp_path, now, fetch=fetch, live=live)
    assert sorted(calls) == ["2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"]
    cached = sorted(p.name for p in (tmp_path / "oi5m").iterdir())
    assert cached == ["2026-11-07.json", "2026-11-08.json"]            # only complete days are cached
    assert live_calls == [rows[-2][0]] and rows[-1][1] == 999.0       # live starts after the newest archive row
    calls.clear()
    derivs.oi_5m(4, tmp_path, now, fetch=fetch, live=live)
    assert sorted(calls) == ["2026-11-05", "2026-11-06"]               # cached days are not downloaded again


def test_oi_candles_have_all_timeframes_and_rounded_values(tmp_path):
    now = 1794225600000
    out = derivs.oi_candles(3, tmp_path, now, fetch=lambda d: _zip_day(d), live=lambda s: [])
    assert set(out) == {"15m", "1h", "4h", "1d"}
    t, o, h, lo, c = out["1h"][0]
    assert t % (60 * MIN) == 0 and o == 100.0 and h == 111.0 and lo == 100.0 and c == 111.0   # 12 five-minute points per hour


def test_average_funding_uses_available_exchanges_and_keeps_the_newest():
    out = derivs.average_funding({"binance": {1: 0.01, 2: 0.02, 3: 0.03}, "bybit": {2: 0.04, 3: 0.01}, "okx": {3: 0.02}}, keep=2)
    assert out["exchanges"] == ["binance", "bybit", "okx"]
    assert out["rows"] == [[2, 0.03, 2], [3, 0.02, 3]]


def test_funding_survives_a_failing_exchange():
    def boom():
        raise RuntimeError("HTTP 403")

    out = derivs.funding({"binance": lambda: {1: 0.01}, "bybit": boom, "okx": lambda: {}})
    assert out["exchanges"] == ["binance"] and out["rows"] == [[1, 0.01, 1]]
    assert derivs.funding({"bybit": boom}) == {}


def test_collect_isolates_failures_and_honours_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(derivs, "oi_candles", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("archive down")))
    monkeypatch.setattr(derivs, "funding", lambda: {"rows": [[1, 0.01, 1]], "exchanges": ["binance"]})
    out = derivs.collect(tmp_path, 0)
    assert "oi" not in out and "archive down" in out["errors"]["oi"] and out["funding"]["rows"]
    assert "funding" not in derivs.collect(tmp_path, 0, skip={"funding", "oi"})


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_js_ohlc_matches_python():
    import re
    from pathlib import Path

    html = (Path(derivs.__file__).parent / "template.html").read_text(encoding="utf-8")
    pure = html[html.index("/*PURE*/"):html.index("/*END PURE*/")]
    pts = [[i * 5 * MIN, 100 + (i * 37) % 11] for i in range(80)]
    for tf in (15 * MIN, 60 * MIN):
        script = pure + f"\nconsole.log(JSON.stringify([...ohlcFrom({json.dumps(pts)}, {tf})]))"
        got = json.loads(subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, check=True).stdout)
        want = derivs.ohlc(pts, tf)
        assert [[g[0]] + g[1] for g in got] == [[w[0], w[1], w[2], w[3], w[4]] for w in want]
    assert re.search(r"function ohlcFrom", pure)
