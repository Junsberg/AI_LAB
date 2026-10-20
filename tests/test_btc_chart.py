from btc_chart.indicators import cumulate, ema, find_fvgs, nearest
from btc_chart.sources import HOUR, Candle, klines_cvd

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
