from memebot.config import ExitRules
from memebot.execution.sim import simulate
from memebot.outcomes.rules import Candle

R = ExitRules(take_initial_at_x=2.0, initial_recover_fraction=0.5, trailing_stop_pct=35,
              volume_dead_minutes=45)
H = 24 * 3600


def c(i, o, cl, v=100.0, lo=None, hi=None):
    return Candle(i * 300, o, hi if hi is not None else max(o, cl), lo if lo is not None else min(o, cl), cl, v)


def run(cs, **kw):
    kw.setdefault("data_until_ts", cs[-1].ts)  # series fetched right after its last candle
    return simulate(cs, 0, R, -50, kw.pop("cost", 0.0), H, **kw)


def test_hard_stop_at_level_and_gap_fills_at_open():
    t = run([c(0, 1.0, 1.0), c(1, 1.0, 0.6), c(2, 0.6, 0.4)])
    assert t.exit_reason == "hard_stop" and t.fills[-1][3] == 0.5 and t.ret == -0.5
    gap = run([c(0, 1.0, 1.0), c(1, 0.3, 0.2)])  # opens through the stop
    assert gap.fills[-1][3] == 0.3 and gap.ret == -0.7


def test_wick_glitch_does_not_stop_out():
    t = run([c(0, 1.0, 1.0), c(1, 1.0, 1.0, lo=1e-9), c(2, 1.0, 1.0)])
    assert t.exit_reason == "horizon" and t.ret == 0.0


def test_take_initial_then_trailing_uses_previous_peak():
    cs = [c(0, 1.0, 1.0), c(1, 1.0, 2.5), c(2, 2.5, 4.0), c(3, 4.0, 4.0), c(4, 4.0, 2.5)]
    t = run(cs)
    # half sold at exactly 2.0 once 2.5 is confirmed; peak 4.0 confirmed by candle 3 →
    # trail at 2.6; candle 4 body low 2.5 → out at 2.6
    assert [f[1] for f in t.fills] == ["take_initial", "trailing"]
    assert t.fills[1][3] == 4.0 * 0.65 and t.ret == round(0.5 * 2.0 + 0.5 * 2.6 - 1, 4)


def test_stop_checked_before_target_in_same_candle():
    t = run([c(0, 1.0, 1.0), c(1, 0.45, 2.5)])  # opens below stop, closes above target
    assert t.exit_reason == "hard_stop"


def test_dead_volume_gap_and_early_data_end():
    t = run([c(0, 1.0, 1.0), c(1, 1.0, 1.2), c(20, 1.2, 1.3)])  # 95-minute gap
    assert t.exit_reason == "dead_volume" and t.fills[-1][3] == 1.1  # min(1.2, median(1.0, 1.2))
    t2 = run([c(0, 1.0, 1.0), c(1, 1.0, 1.1)], data_until_ts=H)
    assert t2.exit_reason == "dead_volume"


def test_costs_both_ways_and_no_entry():
    t = run([c(0, 1.0, 1.0), c(1, 1.0, 1.0)], cost=3.0)
    assert t.ret == round(0.97 / 1.03 - 1, 4)
    assert simulate([c(0, 1.0, 1.0)], 10_000, R, -50, 0, H).exit_reason == "no_entry"


def test_entry_is_first_candle_at_or_after_entry_time_body_high():
    t = simulate([c(0, 1.0, 1.0), c(1, 1.0, 3.0), c(2, 3.0, 3.0)], 300, R, -50, 0, H)
    assert t.entry_px == 3.0


def test_lone_spike_before_death_is_not_an_exit_price():
    # 09-28 control trades: last 5m candle ~70x, then no trades → old sim sold everything there
    t = run([c(0, 1.0, 1.0), c(1, 1.0, 1.05), c(2, 1.05, 70.0)], data_until_ts=H)
    assert t.exit_reason == "dead_volume" and t.ret < 0.1
    assert "take_initial" not in [f[1] for f in t.fills]  # the lone print never confirmed 2x


def test_confirmed_spike_still_takes_profit():
    t = run([c(0, 1.0, 1.0), c(1, 1.0, 3.0), c(2, 3.0, 3.0), c(3, 3.0, 3.0)])
    assert t.fills[0][1] == "take_initial" and t.fills[0][3] == 2.0
