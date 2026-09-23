from memebot.collectors.rugcheck import parse_report


def test_parse_report_aggregates():
    report = {
        "score_normalised": 42,
        "mintAuthority": None,
        "freezeAuthority": "SomeKey",
        "topHolders": [{"owner": "POOLVAULT", "pct": 80.0}, {"pct": 12.0, "insider": True}]
        + [{"pct": 3.0, "insider": False}] * 12,
        "markets": [{"pubkey": "POOLVAULT", "lp": {"lpLockedPct": 100.0}}],
        "risks": [{"name": "Freeze Authority still enabled"}],
    }
    s = parse_report(report)
    assert s.score == 42
    assert s.top10_pct == 12.0 + 3.0 * 9
    assert s.insiders_pct == 12.0
    assert s.lp_locked_pct == 100.0
    assert s.mint_authority is False and s.freeze_authority is True
    assert s.risks == ["Freeze Authority still enabled"]
    assert s.raw_top[0]["pct"] == 80.0  # unfiltered audit trail keeps the pool row
