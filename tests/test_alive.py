from memebot.config import load_params
from memebot.outcomes.rules import Candle
from memebot.signals.alive import alive

TH = load_params().thresholds
D = 1800 * 10  # decision time, slot-aligned


def c(slot, o, cl, v=1500.0):
    return Candle(D - slot * 300, o, max(o, cl), min(o, cl), cl, v)


def test_alive_token_enters():
    cs = [c(6, 1.0, 1.2), c(5, 1.2, 1.3), c(4, 1.3, 1.2), c(3, 1.2, 1.1), c(2, 1.1, 1.2), c(1, 1.2, 1.2)]
    ok, why, m = alive(cs, D, TH)
    assert ok and why is None and m["recent_slots_traded"] == 3


def test_gap_in_recent_slots_is_not_trading():
    cs = [c(6, 1.0, 1.2), c(5, 1.2, 1.3), c(4, 1.3, 1.2), c(3, 1.2, 1.1), c(1, 1.2, 1.2)]  # slot 2 empty
    assert alive(cs, D, TH)[1] == "not_trading"


def test_low_volume_and_collapse():
    thin = [c(k, 1.0, 1.0, v=100.0) for k in range(6, 0, -1)]
    assert alive(thin, D, TH)[1] == "low_volume"
    crash = [c(6, 1.0, 4.0), c(5, 4.0, 4.0), c(4, 4.0, 3.0), c(3, 3.0, 1.0), c(2, 1.0, 1.1), c(1, 1.1, 1.0)]
    assert alive(crash, D, TH)[1] == "collapsed"  # 1.0 < 0.5 x confirmed peak 4.0


def test_no_lookahead_and_lone_spike_is_not_the_peak():
    # the candle starting at the decision time is not closed yet and must be ignored
    cs = [c(3, 1.0, 1.0), c(2, 1.0, 1.0), c(1, 1.0, 1.0), Candle(D, 1.0, 50.0, 0.1, 0.1, 99999.0)]
    ok, _, m = alive(cs, D, TH)
    assert ok and m["candles"] == 3
    spike = [c(4, 1.0, 1.0), c(3, 1.0, 70.0), c(2, 1.0, 1.0), c(1, 1.0, 1.0)]  # unconfirmed 70x print
    assert alive(spike, D, TH)[0]


def test_nothing_traded():
    assert alive([], D, TH) == (False, "not_trading", {"candles": 0})
