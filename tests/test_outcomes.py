from memebot.outcomes.rules import Candle, classify


def c(ts, o, h, lo, cl, v=1000.0):
    return Candle(ts, o, h, lo, cl, v)


def test_no_data():
    assert classify([], None, None).rug_reason == "no_data"


def test_healthy_runner_not_rugged():
    candles = [c(1, 1.0, 1.2, 0.9, 1.1), c(2, 1.1, 12.0, 1.0, 9.0), c(3, 9.0, 10.0, 7.0, 8.0)]
    o = classify(candles, 50_000, 60_000)
    assert o.peak_multiple == 12.0 and not o.rugged and o.rug_reason == "none"


def test_lp_pull():
    candles = [c(1, 1.0, 5.0, 0.9, 4.0), c(2, 4.0, 4.1, 0.1, 0.2)]
    o = classify(candles, liquidity_now_usd=500, liquidity_peak_usd=40_000)
    assert o.rugged and o.rug_reason == "lp_pull"


def test_price_collapse_without_liquidity_data():
    candles = [c(1, 1.0, 10.0, 0.9, 8.0), c(2, 8.0, 8.0, 0.3, 0.4)]
    o = classify(candles, None, None)
    assert o.rugged and o.rug_reason == "price_collapse"


def test_deep_drawdown_but_not_collapse_is_not_rug():
    candles = [c(1, 1.0, 10.0, 0.9, 8.0), c(2, 8.0, 8.0, 1.5, 2.0)]  # -80% from peak
    o = classify(candles, 20_000, 30_000)
    assert not o.rugged and o.drawdown_from_peak == 0.8
