import asyncio
import time

import httpx
import pytest

from memebot.outcomes import evaluate as ev
from memebot.outcomes.rules import Candle, classify


def c(ts, o, h, lo, cl, v=1000.0):
    return Candle(ts, o, h, lo, cl, v)


def test_no_data():
    assert classify([], None, None).rug_reason == "no_data"


def test_healthy_runner_not_rugged():
    candles = [c(0, 1.0, 1.2, 0.9, 1.1), c(3600, 1.1, 12.0, 1.0, 9.0), c(7200, 9.0, 10.0, 7.0, 8.0)]  # hourly
    o = classify(candles, 50_000, 60_000)
    # base = first-candle body high (1.1), peak = body high (9.0), never the 12.0 wick
    assert o.peak_multiple == round(9.0 / 1.1, 4) and not o.rugged and o.rug_reason == "none"


def test_lp_pull():
    candles = [c(1, 1.0, 5.0, 0.9, 4.0), c(2, 4.0, 4.1, 0.1, 0.2)]
    o = classify(candles, liquidity_now_usd=500, liquidity_peak_usd=40_000)
    assert o.rugged and o.rug_reason == "lp_pull"


def test_price_collapse_without_liquidity_data():
    candles = [c(1, 1.0, 10.0, 0.9, 8.0), c(2, 8.0, 8.0, 0.3, 0.4)]
    o = classify(candles, None, None)
    assert o.rugged and o.rug_reason == "price_collapse"


def test_deep_drawdown_but_not_collapse_is_not_rug():
    candles = [c(1, 1.0, 1.0, 0.9, 1.0), c(2, 1.0, 10.0, 1.0, 10.0), c(3, 10.0, 10.0, 1.5, 2.0)]  # -80% from peak
    o = classify(candles, 20_000, 30_000)
    assert not o.rugged and o.drawdown_from_peak == 0.8


def test_wick_glitch_does_not_make_peak_or_collapse():
    # one candle with a 1e6x wick but a normal body: v1 called this a 1e6x "price_collapse"
    candles = [c(0, 1.0, 1.1, 0.9, 1.0), c(3600, 1.0, 1_000_000.0, 0.9, 1.2), c(7200, 1.2, 1.3, 1.0, 1.1)]
    o = classify(candles, 20_000, 30_000)
    assert o.peak_multiple == 1.2 and not o.rugged and o.rug_reason == "none"


def test_first_open_glitch_uses_close_as_base():
    candles = [c(1, 1e-12, 1.0, 1e-12, 1.0), c(2, 1.0, 3.0, 0.9, 2.5)]
    o = classify(candles, 20_000, 30_000)
    assert o.peak_multiple == 2.5


def test_zero_volume_candles_are_ignored():
    candles = [c(1, 1e-9, 1e-9, 1e-9, 1e-9, v=0.0), c(2, 1.0, 1.5, 0.9, 1.2), c(3, 1.2, 2.0, 1.0, 1.8)]
    assert classify(candles, None, None).peak_multiple == 1.5
    assert classify([c(1, 1.0, 1.0, 1.0, 1.0, v=0.0)], None, None).rug_reason == "no_data"


def test_absurd_multiple_is_bad_data():
    candles = [c(1, 1.0, 1.0, 1.0, 1.0), c(2, 1.0, 5000.0, 1.0, 5000.0)]
    o = classify(candles, None, None)
    assert o.rug_reason == "no_data" and o.peak_multiple is None


def test_drained_pool_zero_liquidity_is_lp_pull():
    candles = [c(1, 1.0, 5.0, 0.9, 4.0), c(2, 4.0, 4.1, 0.1, 0.2)]
    o = classify(candles, liquidity_now_usd=0.0, liquidity_peak_usd=40_000)
    assert o.rugged and o.rug_reason == "lp_pull"


def test_ohlcv_null_rows_are_skipped():
    from memebot.outcomes.evaluate import _ohlcv

    async def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": [[2, 1, 2, 0.5, 1.5, None], [1, 1, 1, 1, 1, 10]]}}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
            return await _ohlcv(c, "POOL", 5.0, time.monotonic() + 100)

    candles = asyncio.run(go())
    assert [c.ts for c in candles] == [1]


# --- evaluate.py: rate-limit handling and pools/multi parsing ---------------------


def test_parse_multi_maps_by_address_and_skips_junk():
    payload = {"data": [
        {"id": "solana_A", "attributes": {"address": "A", "reserve_in_usd": "10"}},
        {"attributes": {}},  # no address → ignored
        None,
    ]}
    assert ev.parse_multi(payload) == {"A": {"address": "A", "reserve_in_usd": "10"}}
    assert ev.parse_multi({}) == {}


def test_retry_after_clamped_and_fallback():
    assert ev._retry_after(httpx.Headers({"Retry-After": "2"}), 60) == ev.RETRY_MIN_S
    assert ev._retry_after(httpx.Headers({"Retry-After": "999"}), 60) == ev.RETRY_MAX_S
    assert ev._retry_after(httpx.Headers({"Retry-After": "soon"}), 60) == 60
    assert ev._retry_after(httpx.Headers({}), 60) == 60


def _client(responses):
    calls = iter(responses)

    def handler(request):
        return next(calls)(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_get_retries_same_request_after_429(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(ev.asyncio, "sleep", fake_sleep)
    client = _client([
        lambda req: httpx.Response(429, headers={"Retry-After": "7"}),
        lambda req: httpx.Response(200, json={"ok": True}),
    ])
    r = asyncio.run(ev._get(client, "https://x/y", None, time.monotonic() + 1000))
    assert r.status_code == 200 and slept == [7.0]


def test_get_raises_when_pause_exceeds_budget():
    client = _client([lambda req: httpx.Response(429)])
    with pytest.raises(ev.BudgetExhausted):
        asyncio.run(ev._get(client, "https://x/y", None, time.monotonic() + 1))


def test_pools_falls_back_to_single_lookup_for_missing(monkeypatch):
    async def no_sleep(s):
        pass

    monkeypatch.setattr(ev.asyncio, "sleep", no_sleep)
    seen = []

    def multi(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={"data": [{"attributes": {"address": "A", "reserve_in_usd": "1"}}]})

    def single_b(req):
        seen.append(req.url.path)
        return httpx.Response(404)

    client = _client([multi, single_b])
    out = asyncio.run(ev._pools(client, ["A", "B"], time.monotonic() + 1000))
    assert out == {"A": {"address": "A", "reserve_in_usd": "1"}, "B": {}}
    assert seen == ["/api/v2/networks/solana/pools/multi/A,B", "/api/v2/networks/solana/pools/B"]


def test_pools_one_bad_pool_does_not_sink_the_batch(monkeypatch):
    async def no_sleep(s):
        pass

    monkeypatch.setattr(ev.asyncio, "sleep", no_sleep)
    client = _client([
        lambda req: httpx.Response(200, json={"data": [{"attributes": {"address": "A", "reserve_in_usd": "1"}}]}),
        lambda req: httpx.Response(500),  # single lookup for B fails
    ])
    out = asyncio.run(ev._pools(client, ["A", "B"], time.monotonic() + 1000))
    assert out == {"A": {"address": "A", "reserve_in_usd": "1"}}  # B absent → its token is skipped, A survives


def test_liquidity_peak_uses_collect_time_floor():
    # pool drained before its first evaluation: without the floor peak == now and lp_pull is impossible
    assert ev.liquidity_peak(None, "12000", 0.0) == 12000.0
    assert ev.liquidity_peak(30_000, "12000", 500) == 30_000.0
    assert ev.liquidity_peak(None, None, 800.0) == 800.0
    assert ev.liquidity_peak(None, "junk", None) is None
    o = classify([c(1, 1.0, 5.0, 0.9, 4.0), c(2, 4.0, 4.1, 0.1, 0.2)], 0.0, ev.liquidity_peak(None, "12000", 0.0))
    assert o.rug_reason == "lp_pull"


# --- rules v3: first 24h from 5-minute candles ---------------------------------------


def test_first_hour_pump_visible_with_5m_candles():
    # hourly: the whole 1→8→3 move sits in the first (base) candle → multiple ~1
    hourly = [c(3600, 1.0, 8.0, 1.0, 3.0), c(7200, 3.0, 3.2, 2.8, 3.0)]
    assert classify(hourly, None, None).peak_multiple == 1.0
    fine = [c(3600, 1.0, 1.2, 1.0, 1.1), c(3900, 1.1, 8.0, 1.1, 8.0), c(4200, 8.0, 8.0, 3.0, 3.0)]
    merged = ev.merge_candles(fine, hourly, cutoff_ts=3600 + ev.FIRST_DAY_S)
    assert classify(merged, None, None).peak_multiple == round(8.0 / 1.1, 4)


def test_merge_candles_splits_at_cutoff_and_falls_back():
    fine = [c(0, 1, 1, 1, 1), c(300, 1, 1, 1, 1), c(1000, 9, 9, 9, 9)]
    coarse = [c(0, 2, 2, 2, 2), c(900, 2, 2, 2, 2), c(3600, 3, 3, 3, 3)]
    assert [x.ts for x in ev.merge_candles(fine, coarse, 900)] == [0, 300, 900, 3600]
    assert ev.merge_candles([], coarse, 900) == coarse  # 5m missing → hourly as before


def _capture(payload_rows):
    seen = {}

    async def h(req: httpx.Request) -> httpx.Response:
        seen["path"], seen["params"] = req.url.path, dict(req.url.params)
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": payload_rows}}})

    return seen, h


def test_5m_young_token_is_one_call_covering_whole_life():
    from datetime import datetime, timezone

    seen, h = _capture([[5, 1, 1, 1, 1, 1], [2, 1, 1, 1, 1, 1]])

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as cl:
            return await ev._ohlcv_5m(cl, "POOL", datetime(2026, 9, 20, tzinfo=timezone.utc), 72.5, time.monotonic() + 100)

    out, whole = asyncio.run(go())
    assert whole and [x.ts for x in out] == [2, 5]
    assert seen["path"].endswith("/pools/POOL/ohlcv/minute")
    assert seen["params"] == {"aggregate": "5", "limit": str(int(72.5 * 12) + 12)}  # 882 ≤ 1000


def test_5m_old_token_is_anchored_to_launch_day():
    from datetime import datetime, timezone

    seen, h = _capture([])
    created = datetime(2026, 9, 20, tzinfo=timezone.utc)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as cl:
            return await ev._ohlcv_5m(cl, "POOL", created, 120.0, time.monotonic() + 100)

    out, whole = asyncio.run(go())
    assert not whole and out == []
    assert seen["params"] == {
        "aggregate": "5", "before_timestamp": str(int(created.timestamp()) + 86400), "limit": "288"}


def test_to_hourly_matches_hourly_candle_semantics():
    fine = [c(3600, 1.0, 1.5, 0.9, 1.2, 10), c(3900, 1.2, 3.0, 1.1, 2.0, 20), c(7200, 2.0, 2.1, 1.9, 2.05, 5)]
    assert ev.to_hourly(fine) == [c(3600, 1.0, 3.0, 0.9, 2.0, 30), c(7200, 2.0, 2.1, 1.9, 2.05, 5)]


def test_merge_without_coarse_builds_hourly_after_cutoff():
    cutoff = 86400
    fine = [c(0, 1, 1, 1, 1), c(300, 1, 1, 1, 2), c(cutoff, 5, 5, 5, 6), c(cutoff + 300, 6, 6, 6, 9)]
    merged = ev.merge_candles(fine, None, cutoff)
    assert [x.ts for x in merged] == [0, 300, cutoff]
    assert (merged[-1].open, merged[-1].close) == (5, 9)  # hourly body, not the 5m peak body


# --- rules v4: dead tokens and the post-entry multiple -----------------------------------


def test_dead_when_no_trade_after_first_hour():
    cs = [c(0, 1.0, 5.0, 1.0, 4.0), c(600, 4.0, 4.0, 3.0, 3.5), c(3000, 3.5, 3.5, 3.0, 3.2)]
    o = classify(cs, None, None)
    assert o.rug_reason == "dead" and not o.rugged


def test_rug_beats_dead_and_live_token_is_not_dead():
    collapse = [c(0, 1.0, 10.0, 1.0, 9.0), c(600, 9.0, 9.0, 0.1, 0.2)]
    assert classify(collapse, None, None).rug_reason == "price_collapse"
    live = [c(0, 1.0, 1.0, 1.0, 1.0), c(7200, 1.0, 1.2, 1.0, 1.1)]
    assert classify(live, None, None).rug_reason == "none"


def test_entry_multiple_excludes_the_launch_pump():
    # 1 → 10 in the first 5 minutes, then flat at 8-9: peak_multiple 10x, reachable ~1.1x
    cs = [c(0, 1.0, 1.0, 1.0, 1.0), c(300, 1.0, 10.0, 1.0, 10.0), c(900, 8.0, 8.0, 8.0, 8.0),
          c(4000, 8.0, 9.0, 8.0, 9.0)]
    o = classify(cs, None, None)
    assert o.peak_multiple == 10.0 and o.entry_multiple == round(9.0 / 8.0, 4)
    assert classify([c(0, 1.0, 1.0, 1.0, 1.0)], None, None).entry_multiple is None
