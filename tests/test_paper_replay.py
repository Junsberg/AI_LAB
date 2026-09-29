from datetime import datetime, timedelta, timezone

from memebot.review import paper_replay as pr

T0 = datetime(2026, 9, 25, tzinfo=timezone.utc)


def row(mint, cid, h, evaluated=True, rugged=False, peak=1.0):
    return {"mint": mint, "cluster_id": cid, "created_at": T0 + timedelta(hours=h),
            "evaluated": evaluated, "rugged": rugged, "peak_multiple": peak}


def test_point_in_time_uses_only_tokens_knowable_24h_earlier():
    rows = [row("a", "C", 0, peak=12), row("b", "C", 10, rugged=True), row("c", "C", 30),
            row("d", "C", 29, evaluated=False), row("x", "OTHER", 0)]
    pit = pr.point_in_time_stats(rows)
    assert pit["c"] == (1, 0, 1)  # only "a" (30h earlier); "b" is 20h earlier; "d" unevaluated
    assert pit["a"] == (0, 0, 0)  # never scores itself
    assert pit["x"] == (0, 0, 0)


def test_summarize():
    s = pr.summarize([{"ret": 1.0, "exit_reason": "trailing"}, {"ret": -0.5, "exit_reason": "hard_stop"},
                      {"ret": None, "exit_reason": "no_entry"}])
    assert s["n"] == 2 and s["mean_ret"] == 0.25 and s["win_rate"] == 0.5
    assert s["exit_reasons"] == {"trailing": 1, "hard_stop": 1, "no_entry": 1}
    assert pr.summarize([]) == {"n": 0}


def test_prior_launches_counts_only_earlier_cluster_tokens():
    rows = [row("a", "C", 0), row("b", "C", 5), row("c", "C", 9), row("x", "OTHER", 1)]
    assert pr.prior_launches(rows) == {"a": 0, "b": 1, "c": 2, "x": 0}
