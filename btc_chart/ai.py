"""AI market commentary through the Claude Message Batches API (build time only).

The API key stays on this PC: the hourly build calls Claude, and only the resulting text is embedded in
the page. Batches cost half of the normal price and are what the Console promo credit can be spent on
(Agent SDK / Batch API / Playground / Managed Agents -- not plain real-time Messages calls).

One batch is in flight at a time. A build submits one, waits a short while for it, and otherwise the next
build collects it. Spend is tracked in ``out/ai.json`` and capped per day and in total.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import sources as src
from .indicators import ema
from .sources import HOUR

KEY_FILE = Path.home() / ".btc_chart" / "anthropic_key.txt"
STATE_NAME = "ai.json"
CUSTOM_ID = "analysis"
BATCH_DISCOUNT = 0.5
# USD per million tokens (input, output) -- cached 2026-10-06 from the Claude API docs.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}
TFS = ("15m", "1h", "4h", "1d")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "bias": {"type": "string", "enum": ["상승 우위", "하락 우위", "중립·혼조"]},
        "summary": {"type": "array", "items": {"type": "string"}},
        "timeframes": {
            "type": "object",
            "properties": {tf: {"type": "string"} for tf in TFS},
            "required": list(TFS),
            "additionalProperties": False,
        },
        "flow": {"type": "string"},
        "watch": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"level": {"type": "string"}, "why": {"type": "string"}},
                "required": ["level", "why"],
                "additionalProperties": False,
            },
        },
        "caveats": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "bias", "summary", "timeframes", "flow", "watch", "caveats"],
    "additionalProperties": False,
}

SYSTEM = """You write a short Korean market commentary for a personal BTC dashboard.
You receive a JSON fact sheet (numbers already computed). Rules:
- Use ONLY numbers present in the fact sheet. Never invent prices, levels or statistics.
- Describe what the data shows now and what would change the picture. Do not predict prices, give targets, or tell the reader to buy or sell.
- Be concrete and brief: every sentence should carry a number or a condition. Plain Korean, no hype, no emoji.
- timeframes: one or two sentences per chart timeframe (15m, 1h, 4h, 1d) on trend vs EMAs and spot CVD.
- flow: how spot/futures CVD, open interest and the Coinbase premium fit together (or conflict).
- watch: 2-4 price zones taken from the fact sheet (FVG zones, EMAs, range edges) and why each matters.
- caveats: data limits worth knowing (e.g. CVD windows, USD/USDT basis in the premium). Keep it to what is true.
- headline: one line. summary: 3-5 bullets."""


# ---------------------------------------------------------------- facts


def summarize_candles(interval: str, candles: list[src.Candle]) -> dict[str, Any]:
    closes = [c.close for c in candles]
    last = closes[-1]

    def chg(n: int) -> float | None:
        return round((last / closes[-1 - n] - 1) * 100, 2) if len(closes) > n else None

    emas: dict[str, Any] = {}
    for n in (20, 50, 100, 200):
        v = ema(closes, n)[-1]
        emas[str(n)] = None if v is None else {"value": round(v, 1), "price_vs_pct": round((last / v - 1) * 100, 2)}
    e20 = ema(closes, 20)
    slope = round((e20[-1] / e20[-6] - 1) * 100, 3) if len(e20) > 6 and e20[-1] and e20[-6] else None
    recent = candles[-100:]
    return {
        "interval": interval,
        "last_close": round(last, 1),
        "change_pct_over_candles": {"6": chg(6), "24": chg(24), "96": chg(96)},
        "ema": emas,
        "ema20_slope_pct_over_5_candles": slope,
        "range_last_100_candles": {"high": round(max(c.high for c in recent), 1), "low": round(min(c.low for c in recent), 1)},
        "spot_cvd_net_usd_last_24_candles": round(sum(2 * c.tbq - c.qv for c in candles[-24:])),
    }


def fetch_timeframes(limit: int = 500) -> dict[str, Any]:
    return {
        tf: summarize_candles(tf, src.binance_klines(src.BINANCE_SPOT_KLINES, "BTCUSDT", tf, limit)) for tf in TFS
    }


def _net24(values: list[float | None]) -> float | None:
    if len(values) < 25 or values[-1] is None or values[-25] is None:
        return None
    return round(values[-1] - values[-25])


def market_facts(payload: dict[str, Any], timeframes: dict[str, Any]) -> dict[str, Any]:
    """Compact fact sheet from the build payload (1H based) plus per-timeframe candle stats."""
    last = payload["candles"][-1][4]
    cvd = {p["key"]: _net24(p["values"]) if p["values"] else None for p in payload["cvd"]}
    extra = {p["key"]: p["values"] for p in payload.get("extra", [])}
    oi = extra.get("binance_oi") or []
    prem = [v for v in (extra.get("cb_premium") or [])[-24:] if v is not None]
    oi_now = next((v for v in reversed(oi) if v is not None), None)
    oi_24 = oi[-25] if len(oi) >= 25 else None
    return {
        "as_of": payload["generated"] + " " + payload["tz"],
        "price": round(last, 1),
        "timeframes": timeframes,
        "net_taker_buy_usd_last_24h": cvd,
        "binance_futures_open_interest_usd": {
            "now": round(oi_now) if oi_now else None,
            "change_24h_pct": round((oi_now / oi_24 - 1) * 100, 2) if oi_now and oi_24 else None,
        },
        "coinbase_premium_pct": {
            "now": round(prem[-1], 3) if prem else None,
            "avg_24h": round(sum(prem) / len(prem), 3) if prem else None,
        },
        "unfilled_fvg_zones": [
            {
                "tf": z["tf"],
                "kind": z["kind"],
                "low": round(z["lo"], 1),
                "high": round(z["hi"], 1),
                "distance_pct": round(((z["lo"] + z["hi"]) / 2 / last - 1) * 100, 2),
            }
            for z in payload["fvg"]
        ],
    }


# ---------------------------------------------------------------- parsing & cost


def parse_analysis(text: str) -> dict[str, Any]:
    d = json.loads(text)

    def s(v: Any, n: int = 600) -> str:
        return str(v).strip()[:n]

    tfs = d.get("timeframes") or {}
    return {
        "headline": s(d["headline"], 200),
        "bias": s(d.get("bias", "중립·혼조"), 20),
        "summary": [s(x) for x in d.get("summary", [])][:6],
        "timeframes": {tf: s(tfs.get(tf, "")) for tf in TFS},
        "flow": s(d.get("flow", ""), 900),
        "watch": [{"level": s(w.get("level", ""), 80), "why": s(w.get("why", ""), 300)} for w in d.get("watch", [])][:5],
        "caveats": [s(x, 300) for x in d.get("caveats", [])][:5],
    }


def batch_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (input_tokens * pin + output_tokens * pout) / 1e6 * BATCH_DISCOUNT


# ---------------------------------------------------------------- state


def _cfg() -> dict[str, Any]:
    e = os.environ.get
    return {
        "model": e("BTC_AI_MODEL", "claude-opus-5-5"),
        "effort": e("BTC_AI_EFFORT", "medium"),
        "min_minutes": float(e("BTC_AI_MIN_MINUTES", "55")),
        "max_calls_day": int(e("BTC_AI_MAX_CALLS_PER_DAY", "24")),
        "budget": float(e("BTC_AI_BUDGET_USD", "90")),
        "wait_sec": float(e("BTC_AI_WAIT_SEC", "120")),
    }


def _empty_state() -> dict[str, Any]:
    return {"analysis": None, "generated_ms": None, "model": None, "pending": None, "last_submit_ms": 0,
            "spend": {"total_usd": 0.0, "by_day": {}}, "last_cost_usd": None, "failures": 0,
            "pause_until_ms": 0, "last_error": None}


def load_state(path: Path) -> dict[str, Any]:
    try:
        return {**_empty_state(), **json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return _empty_state()


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def make_client() -> Any | None:
    """Official SDK client from the environment or the key file; None when there is no key or SDK."""
    try:
        import anthropic
    except ImportError:
        return None
    if os.environ.get("ANTHROPIC_API_KEY"):
        return anthropic.Anthropic()
    try:
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return anthropic.Anthropic(api_key=key) if key else None


# ---------------------------------------------------------------- the batch round trip


def _request(facts: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "custom_id": CUSTOM_ID,
        "params": {
            "model": cfg["model"],
            "max_tokens": 16000,   # thinking counts toward this
            "system": SYSTEM,
            "output_config": {"effort": cfg["effort"], "format": {"type": "json_schema", "schema": SCHEMA}},
            "messages": [{"role": "user", "content": "Fact sheet:\n" + json.dumps(facts, ensure_ascii=False, indent=1)}],
        },
    }


def _collect(client: Any, batch_id: str, state: dict[str, Any], now_ms: int, cfg: dict[str, Any]) -> str:
    for res in client.messages.batches.results(batch_id):
        if res.custom_id != CUSTOM_ID:
            continue
        r = res.result
        if r.type != "succeeded":
            detail = getattr(getattr(r, "error", None), "type", "") or r.type
            state["failures"] += 1
            state["last_error"] = f"batch result {r.type}: {detail}"
            return state["last_error"]
        msg = r.message
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            analysis = parse_analysis(text)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            state["failures"] += 1
            state["last_error"] = f"unparseable answer: {e}"
            return state["last_error"]
        cost = batch_cost(msg.model or cfg["model"], msg.usage.input_tokens, msg.usage.output_tokens)
        day = state["spend"]["by_day"].setdefault(_day(now_ms), {"calls": 0, "usd": 0.0})
        day["calls"] += 1
        day["usd"] = round(day["usd"] + cost, 4)
        state["spend"]["total_usd"] = round(state["spend"]["total_usd"] + cost, 4)
        state.update(analysis=analysis, generated_ms=now_ms, model=msg.model or cfg["model"], last_cost_usd=round(cost, 4),
                     failures=0, last_error=None)
        return f"ok (${cost:.3f}, total ${state['spend']['total_usd']:.2f})"
    state["failures"] += 1
    state["last_error"] = "batch returned no result"
    return state["last_error"]


def _poll(client: Any, state: dict[str, Any], now_ms: int, cfg: dict[str, Any]) -> str | None:
    """Check the pending batch once. Returns a status message when it finished, else None."""
    pend = state["pending"]
    batch = client.messages.batches.retrieve(pend["id"])
    if batch.processing_status != "ended":
        if now_ms - pend["submitted_ms"] > 6 * HOUR:   # something is stuck: give up and start fresh next time
            try:
                client.messages.batches.cancel(pend["id"])
            except Exception:
                pass
            state["pending"] = None
            state["failures"] += 1
            state["last_error"] = "batch took more than 6 h"
            return state["last_error"]
        return None
    state["pending"] = None
    return _collect(client, pend["id"], state, now_ms, cfg)


def page_info(state: dict[str, Any], status: str) -> dict[str, Any] | None:
    if not state["analysis"] and status.startswith("off"):
        return {"status": status, "analysis": None}
    return {
        "status": status,
        "analysis": state["analysis"],
        "generated_ms": state["generated_ms"],
        "model": state["model"],
        "last_cost_usd": state["last_cost_usd"],
        "spend_total_usd": state["spend"]["total_usd"],
    }


def run(
    payload: dict[str, Any],
    out_dir: Path,
    *,
    mode: str = "auto",
    now_ms: int | None = None,
    client: Any | None = None,
    fetch: Callable[[], dict[str, Any]] = fetch_timeframes,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any] | None:
    """Advance the AI-commentary state machine by one build; returns what the page should show."""
    now_ms = now_ms or int(time.time() * 1000)
    cfg = _cfg()
    path = out_dir / STATE_NAME
    state = load_state(path)
    if mode == "off":
        return page_info(state, "off: disabled (--ai off)")
    client = client or make_client()
    if client is None:
        return page_info(state, f"off: no API key (set ANTHROPIC_API_KEY or put it in {KEY_FILE})")

    msg = ""
    try:
        if state["pending"]:
            msg = _poll(client, state, now_ms, cfg) or "batch still running"
        if not state["pending"]:
            today = state["spend"]["by_day"].get(_day(now_ms), {"calls": 0, "usd": 0.0})
            est = state["last_cost_usd"] or 0.15
            due = mode == "force" or now_ms - state["last_submit_ms"] >= cfg["min_minutes"] * 60_000
            if now_ms < state["pause_until_ms"]:
                msg = msg or "paused after repeated failures"
            elif not due:
                msg = msg or "not due yet"
            elif today["calls"] >= cfg["max_calls_day"]:
                msg = msg or f"daily call cap reached ({cfg['max_calls_day']})"
            elif state["spend"]["total_usd"] + est > cfg["budget"]:
                msg = msg or f"budget cap reached (${state['spend']['total_usd']:.2f} of ${cfg['budget']:.0f})"
            else:
                facts = market_facts(payload, fetch())
                batch = client.messages.batches.create(requests=[_request(facts, cfg)])
                state["pending"] = {"id": batch.id, "submitted_ms": now_ms}
                state["last_submit_ms"] = now_ms
                msg = "submitted"
                t0 = time.time()
                deadline = t0 + cfg["wait_sec"]
                msg = "submitted (result will be collected by the next build)"
                while True:   # most batches finish within minutes
                    sleep(5)
                    done = _poll(client, state, now_ms + int((time.time() - t0) * 1000), cfg)
                    if done is not None:
                        msg = done
                        break
                    if time.time() >= deadline:
                        break
    except Exception as e:   # never let AI break the chart build
        state["failures"] += 1
        state["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        msg = state["last_error"]
    if state["failures"] >= 3 and now_ms >= state["pause_until_ms"]:
        state["pause_until_ms"] = now_ms + 6 * HOUR   # e.g. credit used up / expired: stop hammering the API
        state["failures"] = 0
        msg += " | 3 failures in a row: pausing 6 h"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        print(f"[ai] could not save state: {e}", file=sys.stderr)
    print(f"[ai] {msg}", file=sys.stderr)
    return page_info(state, msg)


def status(out_dir: Path) -> str:
    st = load_state(out_dir / STATE_NAME)
    sp = st["spend"]
    today = sp["by_day"].get(_day(int(time.time() * 1000)), {"calls": 0, "usd": 0.0})
    return (f"spent ${sp['total_usd']:.2f} total (budget ${_cfg()['budget']:.0f}); today {today['calls']} calls ${today['usd']:.2f}; "
            f"last cost {st['last_cost_usd']}; pending={bool(st['pending'])}; last error: {st['last_error']}")


if __name__ == "__main__":
    print(status(Path(__file__).parent / "out"))
