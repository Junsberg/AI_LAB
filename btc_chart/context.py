"""Market context for the AI commentary and the 'market indicators' card.

All free public endpoints (no keys): Binance futures positioning, CoinMetrics community on-chain data,
mempool.space network stats, DefiLlama stablecoin supply, alternative.me Fear & Greed, CoinGecko dominance.
Every group is fetched independently and may fail on its own; failures are listed in ``errors``.
Parsers are pure functions so they can be tested on saved responses.
"""

from __future__ import annotations

import sys
from typing import Any, Callable

from . import sources as src

FAPI = "https://fapi.binance.com"
COINMETRICS = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
MEMPOOL = "https://mempool.space/api/v1"
LLAMA_STABLES = "https://stablecoins.llama.fi/stablecoins"
FEAR_GREED = "https://api.alternative.me/fng/"
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
CM_METRICS = "CapMVRVCur,FlowInExUSD,FlowOutExUSD,SplyExNtv,AdrActCnt,TxCnt,HashRate"


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _pct(a: float | None, b: float | None) -> float | None:
    return None if a is None or not b else round((a / b - 1) * 100, 2)


def _r(v: float | None, n: int = 4) -> float | None:
    return None if v is None else round(v, n)


# ---------------------------------------------------------------- parsers


def parse_derivatives(prem: dict, funding: list, glob_ls: list, top_ls: list, taker: list) -> dict[str, Any]:
    mark, index = _f(prem.get("markPrice")), _f(prem.get("indexPrice"))
    fr = [_f(x.get("fundingRate")) for x in funding]
    fr = [x for x in fr if x is not None]
    g_now = glob_ls[-1] if glob_ls else {}
    g_old = glob_ls[0] if glob_ls else {}
    t_now = top_ls[-1] if top_ls else {}
    t_old = top_ls[0] if top_ls else {}
    tk = [_f(x.get("buySellRatio")) for x in taker]
    tk = [x for x in tk if x is not None]
    return {
        "funding_rate_8h_pct": _r(_f(prem.get("lastFundingRate")) * 100 if _f(prem.get("lastFundingRate")) is not None else None),
        "funding_avg_7d_pct": _r(sum(fr) / len(fr) * 100) if fr else None,
        "perp_basis_pct": _r((mark / index - 1) * 100, 3) if mark and index else None,
        "long_short_account_ratio": {"now": _r(_f(g_now.get("longShortRatio")), 3), "long_pct": _r(_f(g_now.get("longAccount")) * 100, 1) if _f(g_now.get("longAccount")) is not None else None,
                                     "24h_ago": _r(_f(g_old.get("longShortRatio")), 3)},
        "top_trader_long_short_position_ratio": {"now": _r(_f(t_now.get("longShortRatio")), 3), "24h_ago": _r(_f(t_old.get("longShortRatio")), 3)},
        "taker_buy_sell_ratio": {"now": _r(tk[-1], 3) if tk else None, "avg_24h": _r(sum(tk) / len(tk), 3) if tk else None},
    }


def parse_onchain(rows: list[dict]) -> dict[str, Any]:
    """CoinMetrics community rows, oldest first. The newest day is preliminary ('flash') -> flagged in the output."""
    rows = list(rows)
    while rows and rows[-1].get("CapMVRVCur") is None:   # the day after UTC rollover arrives half-empty: use the last complete day
        rows.pop()
    rows = rows[-8:]
    if not rows:
        return {}
    last, week_ago = rows[-1], rows[0]

    def col(k: str) -> list[float | None]:
        return [_f(r.get(k)) for r in rows]

    inflow, outflow = col("FlowInExUSD"), col("FlowOutExUSD")
    net = [(a - b) if a is not None and b is not None else None for a, b in zip(inflow, outflow)]
    adr = [x for x in col("AdrActCnt") if x is not None]
    hr = _f(last.get("HashRate"))
    bal, bal0 = _f(last.get("SplyExNtv")), _f(week_ago.get("SplyExNtv"))
    return {
        "as_of": str(last.get("time", ""))[:10],
        "preliminary": any("flash" in str(v) for k, v in last.items() if k.endswith("-status")),
        "mvrv": _r(_f(last.get("CapMVRVCur")), 3),
        "mvrv_7d_ago": _r(_f(week_ago.get("CapMVRVCur")), 3),
        "exchange_inflow_usd": _r(inflow[-1], 0),
        "exchange_outflow_usd": _r(outflow[-1], 0),
        "exchange_netflow_usd_latest_day": _r(net[-1], 0),   # + = more coins deposited to exchanges than withdrawn
        "exchange_netflow_usd_sum_7d": _r(sum(x for x in net if x is not None), 0) if any(x is not None for x in net) else None,
        "exchange_balance_btc": _r(bal, 0),
        "exchange_balance_change_7d_pct": _r(_pct(bal, bal0), 3),
        "active_addresses": _r(adr[-1], 0) if adr else None,
        "active_addresses_vs_7d_avg_pct": _r(_pct(adr[-1], sum(adr) / len(adr)), 1) if adr else None,
        "tx_count": _r(_f(last.get("TxCnt")), 0),
        "hashrate_eh_s": _r(hr / 1e6, 0) if hr is not None else None,   # CoinMetrics reports TH/s
    }


def parse_network(fees: dict, diff: dict) -> dict[str, Any]:
    return {
        "fee_fastest_sat_vb": fees.get("fastestFee"),
        "fee_hour_sat_vb": fees.get("hourFee"),
        "difficulty_epoch_progress_pct": _r(_f(diff.get("progressPercent")), 1),
        "expected_difficulty_change_pct": _r(_f(diff.get("difficultyChange")), 2),
        "blocks_to_retarget": diff.get("remainingBlocks"),
    }


def parse_stablecoins(assets: list[dict]) -> dict[str, Any]:
    def total(key: str) -> float:
        return sum(((a.get(key) or {}).get("peggedUSD") or 0) for a in assets if a.get("pegType") == "peggedUSD")

    now = total("circulating")
    return {"supply_usd": _r(now, 0), "change_1d_pct": _pct(now, total("circulatingPrevDay")),
            "change_7d_pct": _pct(now, total("circulatingPrevWeek")), "change_30d_pct": _pct(now, total("circulatingPrevMonth"))}


def parse_fear_greed(res: dict) -> dict[str, Any]:
    d = res.get("data") or []
    val = lambda i: int(d[i]["value"]) if len(d) > i else None   # noqa: E731
    return {"now": val(0), "label": d[0].get("value_classification") if d else None, "yesterday": val(1), "7d_ago": val(7)}


def parse_dominance(res: dict) -> dict[str, Any]:
    d = res.get("data") or {}
    p = d.get("market_cap_percentage") or {}
    others = max(0.0, 100 - sum(p.values())) if p else None   # everything outside the top 10 coins
    return {"btc_pct": _r(p.get("btc"), 2), "usdt_pct": _r(p.get("usdt"), 2), "others_pct": _r(others, 2),
            "total_market_cap_usd": _r(_f((d.get("total_market_cap") or {}).get("usd")), 0),
            "total_market_cap_change_24h_pct": _r(_f(d.get("market_cap_change_percentage_24h_usd")), 2)}


# ---------------------------------------------------------------- fetching


def _derivatives() -> dict[str, Any]:
    sym = {"symbol": "BTCUSDT"}
    ls = {**sym, "period": "1h", "limit": 24}
    return parse_derivatives(
        src.http_json(f"{FAPI}/fapi/v1/premiumIndex", sym),
        src.http_json(f"{FAPI}/fapi/v1/fundingRate", {**sym, "limit": 21}),
        src.http_json(f"{FAPI}/futures/data/globalLongShortAccountRatio", ls),
        src.http_json(f"{FAPI}/futures/data/topLongShortPositionRatio", ls),
        src.http_json(f"{FAPI}/futures/data/takerlongshortRatio", ls),
    )


def _onchain() -> dict[str, Any]:
    res = src.http_json(COINMETRICS, {"assets": "btc", "metrics": CM_METRICS, "frequency": "1d", "limit_per_asset": 9, "paging_from": "end"})
    return parse_onchain(res.get("data") or [])


GROUPS: dict[str, Callable[[], dict[str, Any]]] = {
    "derivatives": _derivatives,
    "onchain": _onchain,
    "network": lambda: parse_network(src.http_json(f"{MEMPOOL}/fees/recommended"), src.http_json(f"{MEMPOOL}/difficulty-adjustment")),
    "stablecoins": lambda: parse_stablecoins(src.http_json(LLAMA_STABLES, {"includePrices": "false"}).get("peggedAssets") or []),
    "sentiment": lambda: parse_fear_greed(src.http_json(FEAR_GREED, {"limit": 8})),
    "dominance": lambda: parse_dominance(src.http_json(COINGECKO_GLOBAL)),
}


def collect(skip: set[str] | None = None, groups: dict[str, Callable[[], dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Fetch every group independently; a failing source only blanks its own group."""
    out: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, fn in (groups or GROUPS).items():
        if skip and name in skip:
            continue
        try:
            data = fn()
            if data and any(v is not None for v in data.values()):
                out[name] = data
            else:
                errors[name] = "no data"
        except Exception as e:   # one flaky free API must never break the build
            errors[name] = f"{type(e).__name__}: {str(e)[:120]}"
            print(f"[context:{name}] {errors[name]}", file=sys.stderr)
    out["errors"] = errors
    return out


# ---------------------------------------------------------------- page tiles


def _sgn(v: float | None, fmt: str) -> str:
    return "–" if v is None else f"{v:+{fmt}}"


def _big(v: float | None) -> str:
    if v is None:
        return "–"
    a = abs(v)
    s = f"{a / 1e9:.2f}B" if a >= 1e9 else f"{a / 1e6:.1f}M" if a >= 1e6 else f"{a / 1e3:.0f}K"
    return ("-" if v < 0 else "") + s


def _usd(v: float | None) -> str:
    return "–" if v is None else ("-$" if v < 0 else "$") + _big(abs(v))


def display_rows(m: dict[str, Any]) -> list[dict[str, str]]:
    """[{group, k, v, d}] for the 'market indicators' card; formatting lives here so the page stays dumb."""
    rows: list[dict[str, str]] = []

    def add(group: str, k: str, v: str, d: str = "") -> None:
        rows.append({"g": group, "k": k, "v": v, "d": d})

    de, oc, nw, st, se = (m.get(x) or {} for x in ("derivatives", "onchain", "network", "stablecoins", "sentiment"))
    if de:
        add("파생", "펀딩비 (8h)", _sgn(de.get("funding_rate_8h_pct"), ".4f") + "%", f"7일 평균 {_sgn(de.get('funding_avg_7d_pct'), '.4f')}%")
        g = de.get("long_short_account_ratio") or {}
        add("파생", "롱/숏 계정 비율", f"{g.get('now', '–')}", f"롱 {g.get('long_pct', '–')}% · 24h 전 {g.get('24h_ago', '–')}")
        t = de.get("top_trader_long_short_position_ratio") or {}
        add("파생", "탑트레이더 롱/숏", f"{t.get('now', '–')}", f"24h 전 {t.get('24h_ago', '–')}")
        add("파생", "선물 베이시스", _sgn(de.get("perp_basis_pct"), ".3f") + "%", "마크 − 인덱스")
        k = de.get("taker_buy_sell_ratio") or {}
        add("파생", "테이커 매수/매도", f"{k.get('avg_24h', '–')}", f"24h 평균 · 현재 {k.get('now', '–')}")
    if oc:
        add("온체인", "MVRV", f"{oc.get('mvrv', '–')}", f"7일 전 {oc.get('mvrv_7d_ago', '–')}")
        add("온체인", "거래소 순유입", _usd(oc.get("exchange_netflow_usd_latest_day")), f"7일 합 {_usd(oc.get('exchange_netflow_usd_sum_7d'))} · + = 입금 우세")
        add("온체인", "거래소 보유 BTC", _big(oc.get("exchange_balance_btc")), f"7일 {_sgn(oc.get('exchange_balance_change_7d_pct'), '.2f')}%")
        add("온체인", "활성 주소", _big(oc.get("active_addresses")), f"7일 평균 대비 {_sgn(oc.get('active_addresses_vs_7d_avg_pct'), '.1f')}%")
        add("온체인", "해시레이트", f"{oc['hashrate_eh_s']:,.0f} EH/s" if oc.get("hashrate_eh_s") else "–", f"기준일 {oc.get('as_of', '')}")
    if nw:
        add("네트워크", "난이도 조정 예상", _sgn(nw.get("expected_difficulty_change_pct"), ".2f") + "%", f"진행 {nw.get('difficulty_epoch_progress_pct', '–')}% · {nw.get('blocks_to_retarget', '–')}블록 남음")
        add("네트워크", "수수료 (sat/vB)", f"{nw.get('fee_fastest_sat_vb', '–')}", f"1시간 내 {nw.get('fee_hour_sat_vb', '–')}")
    if st:
        add("자금", "스테이블 공급", _usd(st.get("supply_usd")), f"7일 {_sgn(st.get('change_7d_pct'), '.2f')}% · 30일 {_sgn(st.get('change_30d_pct'), '.2f')}%")
    if se:
        add("심리", "공포·탐욕", f"{se.get('now', '–')} {se.get('label') or ''}".strip(), f"어제 {se.get('yesterday', '–')} · 7일 전 {se.get('7d_ago', '–')}")
    return rows
