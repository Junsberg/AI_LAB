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
    candles = [c(1, 1.0, 1.2, 0.9, 1.1), c(2, 1.1, 12.0, 1.0, 9.0), c(3, 9.0, 10.0, 7.0, 8.0)]
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
    candles = [c(1, 1.0, 1.1, 0.9, 1.0), c(2, 1.0, 1_000_000.0, 0.9, 1.2), c(3, 1.2, 1.3, 1.0, 1.1)]
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
