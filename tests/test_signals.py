from datetime import datetime, timedelta, timezone

from memebot.signals.calls import extract_mints
from memebot.signals.kol_reverse import Call, Trade, kol_score, precursor_wallets
from memebot.signals.survivor import SurvivorSnapshot, survivor_score

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_extract_mints_from_links_and_text():
    txt = (
        "🚀 new gem https://dexscreener.com/solana/7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr "
        "CA: 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtpump"
    )
    mints = extract_mints(txt)
    assert "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr" in mints
    assert "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtpump" in mints
    assert extract_mints("gm frens no CA here") == []


def test_precursor_wallet_detected_and_sprayer_rejected():
    calls = [Call(f"m{i}", T0 + timedelta(hours=i)) for i in range(5)]
    trades = []
    # insider buys 5 min before each of 4 calls
    for i in range(4):
        trades.append(Trade("insider", f"m{i}", "buy", T0 + timedelta(hours=i, minutes=-5), 1))
    # sprayer buys 20 mints, only 3 happen to precede calls
    for i in range(20):
        mint = f"m{i}" if i < 3 else f"junk{i}"
        trades.append(Trade("sprayer", mint, "buy", T0 + timedelta(hours=i, minutes=-5), 1))
    # late buyer after calls
    trades.append(Trade("late", "m0", "buy", T0 + timedelta(minutes=10), 1))

    pw = precursor_wallets(trades, calls)
    assert "insider" in pw and pw["insider"].hits == 4
    assert "sprayer" not in pw  # precision 3/20 < 0.3
    assert "late" not in pw

    assert kol_score({"insider"}, pw) > 0.5
    assert kol_score({"nobody"}, pw) == 0.0


def test_survivor_rejects_and_scores():
    base = dict(peak_mcap_usd=1_000_000, holders_peak=1000, top10_pct=20, hours_since_peak=24,
                buys_last_hour=15, sells_last_hour=5)
    s, snap = survivor_score(SurvivorSnapshot(mcap_usd=500_000, holders_now=800, deployer_pct_now=0, **base))
    assert s == 0.0 and snap["reject"] == "not_deep_enough"
    s, snap = survivor_score(SurvivorSnapshot(mcap_usd=150_000, holders_now=800, deployer_pct_now=8, **base))
    assert snap["reject"] == "deployer_still_holds"
    s, snap = survivor_score(SurvivorSnapshot(mcap_usd=150_000, holders_now=800, deployer_pct_now=0, **base))
    assert 0.7 < s <= 1.0
