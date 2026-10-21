import json

import pytest

from btc_chart import ai, context


def test_parse_derivatives():
    d = context.parse_derivatives(
        {"markPrice": "81499", "indexPrice": "81530.8", "lastFundingRate": "0.00009208"},
        [{"fundingRate": "0.0001"}, {"fundingRate": "0.0003"}],
        [{"longShortRatio": "1.4", "longAccount": "0.58"}, {"longShortRatio": "1.79", "longAccount": "0.641"}],
        [{"longShortRatio": "1.7"}, {"longShortRatio": "1.78"}],
        [{"buySellRatio": "0.8"}, {"buySellRatio": "1.2"}],
    )
    assert d["funding_rate_8h_pct"] == pytest.approx(0.0092)
    assert d["funding_avg_7d_pct"] == pytest.approx(0.02)
    assert d["perp_basis_pct"] == pytest.approx(-0.039, abs=1e-3)
    assert d["long_short_account_ratio"] == {"now": 1.79, "long_pct": 64.1, "24h_ago": 1.4}
    assert d["top_trader_long_short_position_ratio"] == {"now": 1.78, "24h_ago": 1.7}
    assert d["taker_buy_sell_ratio"] == {"now": 1.2, "avg_24h": 1.0}


def test_parse_derivatives_tolerates_empty_lists():
    d = context.parse_derivatives({}, [], [], [], [])
    assert d["funding_rate_8h_pct"] is None and d["long_short_account_ratio"]["now"] is None


def rows():
    out = []
    for i in range(8):
        out.append({"time": f"2026-10-{12 + i}T00:00:00.000000000Z", "CapMVRVCur": str(1.5 + i / 100), "FlowInExUSD": str(1000 + i),
                    "FlowOutExUSD": str(900), "SplyExNtv": str(2_700_000 - i * 1000), "AdrActCnt": str(600_000 + i * 1000),
                    "TxCnt": "650000", "HashRate": "1000000000", "FlowInExUSD-status": "flash" if i == 7 else "reviewed"})
    return out


def test_parse_onchain_netflow_balance_and_flags():
    o = context.parse_onchain(rows())
    assert o["as_of"] == "2026-10-19" and o["preliminary"] is True
    assert o["mvrv"] == 1.57 and o["mvrv_7d_ago"] == 1.5
    assert o["exchange_netflow_usd_latest_day"] == 107           # 1007 in - 900 out
    assert o["exchange_netflow_usd_sum_7d"] == sum(1000 + i - 900 for i in range(8))
    assert o["exchange_balance_change_7d_pct"] == pytest.approx((2_693_000 / 2_700_000 - 1) * 100, abs=1e-3)
    assert o["hashrate_eh_s"] == 1000                           # TH/s -> EH/s
    assert o["active_addresses"] == 607000 and o["active_addresses_vs_7d_avg_pct"] > 0
    assert context.parse_onchain([]) == {}


def test_parse_onchain_skips_a_half_empty_newest_day():
    partial = {"time": "2026-10-20T00:00:00.000000000Z", "CapMVRVCur": None, "FlowInExUSD": None, "AdrActCnt": "648048", "HashRate": "1153000000"}
    o = context.parse_onchain(rows() + [partial])
    assert o["as_of"] == "2026-10-19" and o["mvrv"] == 1.57 and o["exchange_netflow_usd_latest_day"] == 107


def test_parse_other_groups():
    assert context.parse_network({"fastestFee": 5, "hourFee": 1}, {"progressPercent": 29.31, "difficultyChange": 5.38, "remainingBlocks": 1425}) == {
        "fee_fastest_sat_vb": 5, "fee_hour_sat_vb": 1, "difficulty_epoch_progress_pct": 29.3, "expected_difficulty_change_pct": 5.38, "blocks_to_retarget": 1425}
    st = context.parse_stablecoins([
        {"pegType": "peggedUSD", "circulating": {"peggedUSD": 110}, "circulatingPrevDay": {"peggedUSD": 100}, "circulatingPrevWeek": {"peggedUSD": 100},
         "circulatingPrevMonth": {"peggedUSD": 100}},
        {"pegType": "peggedEUR", "circulating": {"peggedUSD": 999}},
    ])
    assert st["supply_usd"] == 110 and st["change_1d_pct"] == 10.0
    fg = context.parse_fear_greed({"data": [{"value": str(v), "value_classification": "Greed"} for v in (55, 59, 53, 55, 63, 53, 62, 57)]})
    assert fg == {"now": 55, "label": "Greed", "yesterday": 59, "7d_ago": 57}
    dom = context.parse_dominance({"data": {"market_cap_percentage": {"btc": 60.0, "eth": 10.0, "usdt": 7.0}, "total_market_cap": {"usd": 3e12},
                                            "market_cap_change_percentage_24h_usd": -3.19}})
    assert dom["btc_pct"] == 60.0 and dom["usdt_pct"] == 7.0 and dom["others_pct"] == 23.0 and dom["total_market_cap_change_24h_pct"] == -3.19


def test_collect_isolates_failing_groups_and_honours_skip():
    boom = lambda: (_ for _ in ()).throw(RuntimeError("HTTP 429 api.coingecko.com"))   # noqa: E731
    out = context.collect({"network"}, groups={"derivatives": lambda: {"funding_rate_8h_pct": 0.01}, "onchain": boom,
                                               "network": lambda: {"a": 1}, "sentiment": lambda: {}})
    assert out["derivatives"] == {"funding_rate_8h_pct": 0.01}
    assert "network" not in out and "network" not in out["errors"]
    assert "429" in out["errors"]["onchain"] and out["errors"]["sentiment"] == "no data"


def test_display_rows_cover_each_group_and_survive_missing_data():
    m = {"derivatives": context.parse_derivatives({"markPrice": "1", "indexPrice": "1", "lastFundingRate": "0.0001"}, [], [], [], []),
         "onchain": context.parse_onchain(rows()), "stablecoins": {"supply_usd": 3.04e11, "change_7d_pct": -0.69, "change_30d_pct": 0.15},
         "sentiment": {"now": 55, "label": "Greed", "yesterday": 59, "7d_ago": 57}}
    r = context.display_rows(m)
    assert {x["g"] for x in r} == {"파생", "온체인", "자금", "심리"}
    assert any(x["k"] == "스테이블 공급" and x["v"] == "$304.00B" for x in r)
    assert any(x["k"] == "거래소 순유입" and x["v"].startswith("$") for x in r)
    assert context.display_rows({}) == []
    json.dumps(r, ensure_ascii=False)


def test_ai_fact_sheet_carries_market_context_without_errors():
    payload = {"generated": "g", "tz": "UTC+9", "candles": [[0, 1, 2, 0, 100]] * 30, "cvd": [], "extra": [], "fvg": [],
               "market": {"dominance": {"btc_pct": 59.6}, "sentiment": {"now": 55}, "errors": {"onchain": "boom"}}}
    tf = {"15m": {}}
    facts = ai.market_facts(payload, tf)
    assert facts["market_context"] == {"sentiment": {"now": 55}}   # dominance is not analysed, errors are not sent
    assert "market_context" not in ai.SYSTEM and set(ai.SCHEMA["properties"]["timeframes"]["required"]) == {"4h", "1d"}
