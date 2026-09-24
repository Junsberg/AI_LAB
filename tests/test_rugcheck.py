import pytest

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
    assert s.raw_top[0]["owner"] == "POOLVAULT"  # full address, not truncated


def test_structural_owner_excluded():
    report = {
        "topHolders": [{"owner": "LEARNEDVAULT", "pct": 90.0}, {"owner": "human", "pct": 5.0}],
        "markets": [],
    }
    assert parse_report(report).top10_pct == 95.0
    assert parse_report(report, structural={"LEARNEDVAULT"}).top10_pct == 5.0


def test_off_curve_detection():
    from memebot.collectors.rugcheck import _is_off_curve

    # Raydium AMM authority is a PDA (off-curve); the system program id is on-curve-like edge case
    assert _is_off_curve("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1") is True
    # pump.fun fee account is also a PDA
    assert _is_off_curve("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM") is True
    # a person's wallet (launch-farm funder observed in our data) is on-curve
    assert _is_off_curve("5F1seMKUqSNhv45f6FhB2cFmgJbk8U1avJw7M6TexUq1") is False


@pytest.mark.asyncio
async def test_fetch_report_distinguishes_transient_from_missing():
    import httpx

    from memebot.collectors.rugcheck import fetch_report

    async def h(req: httpx.Request) -> httpx.Response:
        m = req.url.path.split("/")[-2]
        return {"m404": httpx.Response(404), "m429": httpx.Response(429), "mhtml": httpx.Response(200, text="<html>")}.get(m, httpx.Response(200, json={"creator": "x" * 40}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        assert (await fetch_report(c, "m404"))[0] == "missing"
        assert (await fetch_report(c, "m429"))[0] == "error"
        assert (await fetch_report(c, "mhtml"))[0] == "error"
        assert (await fetch_report(c, "ok"))[0] == "ok"
