import pytest

from btc_chart.indicators import align, cumulate, ema, find_fvgs, nearest, premium_pct
import subprocess

from btc_chart.publish import publish
from btc_chart.sources import HOUR, Candle, SourceError, klines_cvd, parse_coinbase_candles, parse_okx_taker_volume

TF = 4 * HOUR


def c(i, o, h, lo, cl, qv=100.0, tbq=50.0):
    return Candle(i * TF, o, h, lo, cl, qv, tbq)


def test_ema_seed_and_recurrence():
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == 2.0  # SMA seed
    assert out[3] == 4 * 0.5 + 2.0 * 0.5  # k = 2/(3+1)
    assert ema([1, 2], 5) == [None, None]


def test_klines_cvd_is_buy_minus_sell():
    assert klines_cvd([c(0, 1, 1, 1, 1, qv=100, tbq=70)]) == {0: 40.0}


def test_bull_fvg_found_and_open():
    # gap between candle0.high (101) and candle2.low (104)
    cs = [
        c(0, 100, 101, 99, 100),
        c(1, 101, 106, 100, 105),
        c(2, 105, 108, 104, 107),
        c(3, 107, 110, 106, 109),
    ]
    zones = find_fvgs(cs, "4H", TF, now_ms=10 * TF)
    assert [(z.kind, z.lo, z.hi, z.t) for z in zones] == [("bull", 101, 104, TF)]


def test_bull_fvg_trimmed_then_filled_by_wick():
    base = [c(0, 100, 101, 99, 100), c(1, 101, 106, 100, 105), c(2, 105, 108, 104, 107)]
    trimmed = find_fvgs(base + [c(3, 107, 108, 102.5, 107)], "4H", TF, 10 * TF)
    assert (trimmed[0].lo, trimmed[0].hi) == (101, 102.5)
    assert find_fvgs(base + [c(3, 107, 108, 100.5, 107)], "4H", TF, 10 * TF) == []


def test_bear_fvg():
    cs = [c(0, 110, 111, 109, 110), c(1, 109, 110, 104, 105), c(2, 105, 107, 103, 104)]
    z = find_fvgs(cs, "4H", TF, 10 * TF)
    assert [(x.kind, x.lo, x.hi) for x in z] == [("bear", 107, 109)]


def test_forming_candle_cannot_create_a_gap():
    cs = [c(0, 100, 101, 99, 100), c(1, 101, 106, 100, 105), c(2, 105, 108, 104, 107)]
    assert find_fvgs(cs, "4H", TF, now_ms=2 * TF + 1) == []  # candle 2 still open


def test_tiny_gap_filtered():
    cs = [c(0, 100, 101, 99, 100), c(1, 101, 106, 100, 105), c(2, 105, 108, 101.01, 107)]
    assert find_fvgs(cs, "4H", TF, 10 * TF) == []


def test_nearest_picks_closest_and_sorts_by_time():
    zs = find_fvgs(
        [
            c(0, 100, 101, 99, 100),
            c(1, 101, 106, 100, 105),
            c(2, 105, 108, 104, 107),
            c(3, 120, 125, 119, 124),
            c(4, 126, 130, 125, 129),
        ],
        "4H",
        TF,
        20 * TF,
    )
    assert len(zs) == 3  # 101-104, 106-119 (via candle 3), 108-125
    assert nearest(zs, 103.0, 1)[0].lo == 101  # price inside the first gap
    assert [z.lo for z in nearest(zs, 103.0, 2)] == [101, 106]  # ordered by time, 2 closest


def test_cumulate_skips_leading_gap():
    assert cumulate({HOUR: 5.0, 2 * HOUR: -2.0}, [0, HOUR, 2 * HOUR, 3 * HOUR]) == [
        None,
        5.0,
        3.0,
        3.0,
    ]


def test_okx_rows_are_keyed_by_bucket_open_with_buy_minus_sell():
    # OKX stamps a bucket with its END time and lists [ts, sellVol, buyVol].
    res = {"code": "0", "data": [[str(5 * HOUR), "100", "130"], [str(4 * HOUR), "90", "60"]]}
    assert parse_okx_taker_volume(res) == {4 * HOUR: 30.0, 3 * HOUR: -30.0}


def test_okx_error_code_raises():
    with pytest.raises(SourceError):
        parse_okx_taker_volume({"code": "50011", "msg": "Too Many Requests", "data": []})


def test_coinbase_candle_close_is_column_four():
    # [time_s, low, high, open, close, volume] -- close is NOT the 4th element
    assert parse_coinbase_candles([[3600, 9.0, 12.0, 10.0, 11.0, 5.0]]) == {3600_000: 11.0}


def test_premium_pct_and_align_leave_gaps_as_none():
    hours = [0, HOUR, 2 * HOUR]
    assert premium_pct({0: 101.0, HOUR: 99.0}, {0: 100.0, HOUR: 100.0, 2 * HOUR: 100.0}, hours) == [
        pytest.approx(1.0), pytest.approx(-1.0), None]
    assert align({HOUR: 5.0}, hours) == [None, 5.0, None]


def test_publish_keeps_a_single_force_pushed_commit(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    page = tmp_path / "index.html"
    for body in ("v1", "v2", "v3"):
        page.write_text(body)
        publish(page, tmp_path / "pages", str(remote))
    log = subprocess.run(
        ["git", "--git-dir", str(remote), "log", "--format=%s", "gh-pages"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert len(log) > 0 and len(subprocess.run(
        ["git", "--git-dir", str(remote), "rev-list", "--count", "gh-pages"],
        capture_output=True, text=True, check=True).stdout.split()) == 1
    shown = subprocess.run(["git", "--git-dir", str(remote), "show", "gh-pages:index.html"],
                           capture_output=True, text=True, check=True).stdout
    assert shown == "v3"


# ---- the page re-implements the pure maths in JS; keep it identical to the Python versions ----
import json
import random
import shutil
from pathlib import Path

NODE = shutil.which("node")


def _run_js(snippet: str):
    html = (Path(__file__).parent.parent / "btc_chart" / "template.html").read_text(encoding="utf-8")
    pure = html.split("/*PURE*/")[1].split("/*END PURE*/")[0]
    # script goes via stdin: a 400-candle JSON blows the Windows command-line length limit
    res = subprocess.run(
        [NODE, "-"], input=pure + "\n" + snippet, capture_output=True, text=True, encoding="utf-8", timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _candles(n, tf_ms, seed=7):
    rnd, px, out = random.Random(seed), 80000.0, []
    for i in range(n):
        o = px
        c = o + rnd.uniform(-400, 400)
        hi, lo = max(o, c) + rnd.uniform(0, 250), min(o, c) - rnd.uniform(0, 250)
        out.append(Candle(i * tf_ms, o, hi, lo, c, 1000.0, 400.0))
        px = c
    return out


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_js_ema_matches_python():
    closes = [c.close for c in _candles(300, HOUR)]
    got = _run_js(f"console.log(JSON.stringify(ema({json.dumps(closes)}, 50)))")
    want = ema(closes, 50)
    assert got[:49] == [None] * 49
    assert all(g == pytest.approx(w) for g, w in zip(got[49:], want[49:]))


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_js_fvgs_match_python():
    cs = _candles(400, 4 * HOUR)
    now = cs[-1].t + 4 * HOUR - 1  # the last candle is still forming
    rows = [[c.t, c.open, c.high, c.low, c.close] for c in cs]
    got = _run_js(f"console.log(JSON.stringify(findFvgs({json.dumps(rows)}, '4H', {4 * HOUR}, {now})))")
    want = [z._asdict() for z in find_fvgs(cs, "4H", 4 * HOUR, now)]
    assert len(want) > 0
    assert [(g["kind"], g["t"]) for g in got] == [(w["kind"], w["t"]) for w in want]
    assert all(g["lo"] == pytest.approx(w["lo"]) and g["hi"] == pytest.approx(w["hi"]) for g, w in zip(got, want))
    near_js = _run_js(
        f"const z = findFvgs({json.dumps(rows)}, '4H', {4 * HOUR}, {now}); console.log(JSON.stringify(nearestZones(z, 80000, 6)))"
    )
    assert [z["t"] for z in near_js] == [z.t for z in nearest(find_fvgs(cs, "4H", 4 * HOUR, now), 80000, 6)]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_js_cumsum_rebase_and_aggregation():
    out = _run_js(
        "const cum = cumSum([null, 1, 2, null, -4]);"
        "const hrs = new Map([[0, 1], [3600000, 2], [7200000, 3], [10800000, 4], [14400000, 10]]);"  # hour 4 starts a new 4h bucket
        "console.log(JSON.stringify({cum, rb: rebase(cum, 3), agg: [...aggregateHourly(hrs, 4 * 3600000)],"
        " cl: [...aggregateCloses(new Map([[0, 5], [3600000, 6], [14400000, 7]]), 4 * 3600000)]}))"
    )
    assert out["cum"] == [None, 1, 3, 3, -1]
    assert out["rb"] == [None, -2, 0, 0, -4]  # re-based so index 3 - 1 == 0 (value at i0-1 = 3)
    assert out["agg"] == [[0, 10], [14400000, 10]]  # first bucket complete (4 hours); second is the forming one
    assert out["cl"] == [[0, 6], [14400000, 7]]
