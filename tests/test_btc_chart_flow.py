import io
import json
import zipfile
from datetime import datetime, timezone

import pytest

from btc_chart import flow

H = flow.HOUR


def test_size_buckets_edges():
    assert flow.size_bucket(99.99) == -1
    assert [flow.size_bucket(x) for x in (100, 999.99, 1000, 9999, 10_000, 99_999, 100_000, 999_999, 1_000_000, 50_000_000)] == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]


def test_add_trade_splits_taker_side_size_and_price_bin():
    hours = {}
    flow.add_trade(hours, 5, 80_100.0, 0.5, False)      # 40 050 USD, taker BUY (buyer is not the maker)
    flow.add_trade(hours, 6, 80_100.0, 0.001, True)     # 80 USD, taker SELL, tiny: only in "all"
    flow.add_trade(hours, H + 1, 79_999.0, 20.0, True)  # 1.6M USD, taker sell, next hour
    h0, h1 = hours[0], hours[H]
    assert h0["all"] == pytest.approx([40_050.0, 80.1])
    assert h0["b"][2] == pytest.approx([40_050.0, 0.0]) and sum(sum(x) for x in h0["b"]) == pytest.approx(40_050.0)
    assert h0["p"] == {str(int(80_100 // flow.BIN)): pytest.approx(40_130.1)}
    assert h1["b"][4] == pytest.approx([0.0, 79_999.0 * 20]) and list(h1["p"]) == [str(int(79_999 // flow.BIN))]


def test_aggregate_csv_skips_header_and_returns_last_id():
    csv = ["agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker",
           "100,80000.0,1.0,1,1,1000,true", "101,80010.0,2.0,2,3,2000,false", ""]
    hours, last = flow.aggregate_csv(csv)
    assert last == 101 and hours[0]["all"] == pytest.approx([160_020.0, 80_000.0])


def _zip(day_ms: int, n: int = 3) -> bytes:
    lines = ["agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker"]
    for i in range(n):
        lines.append(f"{1000 + i},80000.0,0.1,1,1,{day_ms + i * 1000},{'true' if i % 2 else 'false'}")
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("x.csv", "\n".join(lines))
    return b.getvalue()


def test_load_day_caches_the_aggregate_not_the_raw_file(tmp_path):
    calls = []

    def fetch(day):
        calls.append(day)
        return _zip(int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000))

    d = flow.load_day("2026-11-08", tmp_path, fetch)
    assert d["last_id"] == 1002 and len(d["hours"]) == 1
    assert flow.load_day("2026-11-08", tmp_path, fetch)["last_id"] == 1002 and calls == ["2026-11-08"]   # second call hits the cache
    assert flow.load_day("2026-11-09", tmp_path, lambda day: None) is None                                # not published yet


def test_catch_up_pages_by_id_stops_at_now_and_resumes_from_the_cursor():
    now = 10_000_000
    data = [{"a": i, "p": "80000.0", "q": "0.01", "T": 1_000_000 + i * 1000, "m": bool(i % 2)} for i in range(2500)]
    seen = []

    def rest(from_id):
        seen.append(from_id)
        return data[from_id:from_id + 1000]

    live = {"hours": {}, "cursor": 0}
    t = [0.0]
    flow.catch_up(live, 0, now, 30.0, rest, sleep=lambda s: None, clock=lambda: t[0])
    assert seen == [0, 1000, 2000] and live["cursor"] == 2500        # the short last page ends the loop
    total = sum(h["all"][0] + h["all"][1] for h in live["hours"].values())
    assert total == pytest.approx(2500 * 800.0)

    seen.clear()
    ticks = iter([0.0, 0.0, 99.0, 99.0])                              # budget exhausted after the first call
    live2 = {"hours": {}, "cursor": 0}
    flow.catch_up(live2, 0, now, 5.0, rest, sleep=lambda s: None, clock=lambda: next(ticks))
    assert seen == [0] and live2["cursor"] == 1000                    # a later build continues from here
    flow.catch_up(live2, 0, now, 30.0, rest, sleep=lambda s: None, clock=lambda: t[0])
    assert live2["cursor"] == 2500


def _hours(n=48, start=0):
    out = {}
    for i in range(n):
        h = flow._hour()
        h["all"] = [100.0 + i, 90.0]
        for b in range(5):
            h["b"][b] = [10.0 * (b + 1) * (i % 3 + 1), 5.0 * (b + 1)]
        h["p"] = {str(320 + (i % 4)): 2_000_000.0}
        out[start + i * H] = h
    return out


def test_build_payload_normalises_cvd_and_lays_out_the_heatmap():
    now = 47 * H + 1000
    p = flow.build_payload(_hours(), {i * H: 80_000.0 + i for i in range(48)}, now, days=2)
    assert len(p["t"]) == 48 and p["labels"][0] == "all" and len(p["series"]) == 6
    for v in p["series"].values():
        assert min(v) == 0.0 and max(v) == 1.0 or set(v) == {0.5}
    heat = p["heat"]
    assert heat["rows"] == 4 and heat["lo"] == 320 * flow.BIN and all(0 <= c[1] < 4 for c in heat["cells"]) and len(heat["cells"]) == 48
    assert heat["cells"][0][2] == 2.0 and heat["close"][3] == 80_003.0
    assert flow.build_payload({}, {}, now) == {}


def test_collect_merges_archive_and_live_and_archive_wins(tmp_path):
    now = int(datetime(2026, 11, 10, 6, tzinfo=timezone.utc).timestamp() * 1000)

    def fetch(day):
        ms = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        lines = ["agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker"]
        for h in range(24):
            lines.append(f"{5000 + h},80000.0,1.0,1,1,{ms + h * H + 5},false")
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("x.csv", "\n".join(lines))
        return b.getvalue()

    day_start = int(datetime(2026, 11, 9, tzinfo=timezone.utc).timestamp() * 1000)
    live_rows = [{"a": 6000 + i, "p": "80000.0", "q": "1.0", "T": day_start + H + i * 600_000, "m": False} for i in range(6)]   # today 00:00-ish
    p = flow.collect(tmp_path, now, {}, 10.0, days=2, fetch=fetch, rest=lambda fid: live_rows if fid == 5000 + 23 + 1 else [], sleep=lambda s: None)
    assert p and p["t"][-1] <= now
    assert json.loads((tmp_path / "flow" / "live.json").read_text())["cursor"] == 6006
