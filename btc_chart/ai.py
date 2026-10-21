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
TFS = ("4h", "1d")   # the only chart timeframes the commentary covers

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

SYSTEM = """You write a short Korean market briefing for a personal BTC dashboard. The reader wants ONE integrated view, not a list of separate analyses.
You receive a JSON fact sheet (numbers already computed). Rules:
- Use ONLY numbers present in the fact sheet. Never invent prices, levels or statistics.
- Synthesize: weigh price/EMA structure, spot and futures flows, positioning (funding, long/short ratios, basis, OI), the Coinbase premium and the on-chain / stablecoin / sentiment backdrop together, and say what they add up to. Mention an indicator only when it changes the conclusion; skip the rest. Never walk through the data source by source.
- Do not discuss dominance. Do not predict prices, give targets, or tell the reader to buy or sell.
- Plain Korean, short sentences, no jargon pile-up, no hype, no emoji. Few numbers per sentence (the ones that matter).
- headline: one line with the overall call. bias: the overall lean.
- summary: exactly 3 bullets: (1) what is happening now, (2) why - the two or three signals that matter most and how they fit or conflict, (3) what would change the picture.
- timeframes: one short sentence each for 4h and 1d (trend vs EMAs, and anything notable).
- flow: at most 3 sentences tying flows, positioning and the backdrop into one reading. On-chain is daily and its newest day is preliminary; say so only if you lean on it.
- watch: 2-3 price zones from the fact sheet (FVG, EMAs, range edges), each with a short reason.
- caveats: at most 2, only limits that affect how to read this briefing."""


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
        "market_context": {k: v for k, v in (payload.get("market") or {}).items() if k not in ("errors", "dominance")},
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


# ---------------------------------------------------------------- config & state

DAY = 24 * HOUR
JOB_ORDER = ("deep", "hourly")   # the rarer, pricier job gets the budget first

DEEP_ADDENDUM = """
This is the deeper briefing, written a few times a day. Same rules and format, but explain the reasoning behind the call a little more and say how confident the read is. Stay readable: each summary bullet at most 3 sentences and 4 numbers, no long chains of figures.
- If the fact sheet has previous_report, add one short clause on what changed since it (levels reached, bias flips).
- Separate what the data shows from what is only inferred (an OI drop is not proof of liquidations).
- Never mention JSON field names; if there is no earlier report just do not compare."""


def _cfg() -> dict[str, Any]:
    e = os.environ.get
    return {
        "model": e("BTC_AI_MODEL", "claude-opus-5-5"),   # also what `--ai compare` uses
        "effort": e("BTC_AI_EFFORT", "xhigh"),
        "budget": float(e("BTC_AI_BUDGET_USD", "90")),
        "expires": e("BTC_AI_EXPIRES", "2026-11-18"),
        "max_calls_day": int(e("BTC_AI_MAX_CALLS_PER_DAY", "30")),
        "wait_sec": float(e("BTC_AI_WAIT_SEC", "120")),
        "jobs": {
            "hourly": {"model": e("BTC_AI_MODEL", "claude-opus-5-5"), "effort": e("BTC_AI_EFFORT", "xhigh"),
                       "min_minutes": float(e("BTC_AI_MIN_MINUTES", "55")), "max_tokens": 24000, "est": 0.06,
                       "system": SYSTEM},
            "deep": {"model": e("BTC_AI_DEEP_MODEL", "claude-fable-5-1"), "effort": e("BTC_AI_DEEP_EFFORT", "xhigh"),
                     "min_minutes": float(e("BTC_AI_DEEP_MIN_MINUTES", "660")), "max_tokens": 32000, "est": 0.15,
                     "system": SYSTEM + DEEP_ADDENDUM},
        },
    }


def _empty_job() -> dict[str, Any]:
    return {"analysis": None, "generated_ms": None, "model": None, "pending": None, "last_submit_ms": 0,
            "last_cost_usd": None, "failures": 0, "pause_until_ms": 0, "last_error": None}


def _empty_state() -> dict[str, Any]:
    return {"jobs": {n: _empty_job() for n in JOB_ORDER}, "spend": {"total_usd": 0.0, "by_day": {}, "by_job": {}}}


def load_state(path: Path) -> dict[str, Any]:
    st = _empty_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return st
    if "jobs" not in raw:   # first version kept a single flat job
        flat = {k: raw[k] for k in _empty_job() if k in raw}
        raw = {"jobs": {"hourly": flat}, "spend": raw.get("spend", {})}
    for n in JOB_ORDER:
        st["jobs"][n].update(raw.get("jobs", {}).get(n, {}))
    st["spend"].update(raw.get("spend", {}))
    return st


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def _expiry_ms(cfg: dict[str, Any]) -> int:
    d = datetime.strptime(cfg["expires"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000) + DAY   # usable through the end of that day


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


def plan(state: dict[str, Any], cfg: dict[str, Any], now_ms: int) -> dict[str, float]:
    """Minutes between calls per job so the remaining budget lasts until the credit expires.

    Costs are the measured last cost of each job, so adding facts (more tokens) automatically slows the
    hourly job instead of overspending. The deep job is funded first, the hourly job gets what is left.
    """
    days_left = max(1.0, (_expiry_ms(cfg) - now_ms) / DAY)
    per_day = max(0.0, cfg["budget"] - state["spend"]["total_usd"]) / days_left
    jobs = cfg["jobs"]
    est = {n: state["jobs"][n]["last_cost_usd"] or jobs[n]["est"] for n in jobs}
    out: dict[str, float] = {}
    left = per_day
    for n in JOB_ORDER:
        want = max(jobs[n]["min_minutes"], 1440 * est[n] / left) if left > 0 else float("inf")
        out[n] = want
        if want != float("inf"):
            left -= 1440 / want * est[n]
    return out


# ---------------------------------------------------------------- the batch round trip


def _request(facts: dict[str, Any], jcfg: dict[str, Any], effort: str | None = None, custom_id: str = CUSTOM_ID,
             max_tokens: int = 24000, system: str = SYSTEM) -> dict[str, Any]:
    return {
        "custom_id": custom_id,
        "params": {
            "model": jcfg["model"],
            "max_tokens": max_tokens,   # thinking counts toward this
            "system": system,
            "output_config": {"effort": effort or jcfg["effort"], "format": {"type": "json_schema", "schema": SCHEMA}},
            "messages": [{"role": "user", "content": "Fact sheet:\n" + json.dumps(facts, ensure_ascii=False, indent=1)}],
        },
    }


def _fail(job: dict[str, Any], why: str) -> str:
    job["failures"] += 1
    job["last_error"] = why
    return why


def _collect(client: Any, name: str, state: dict[str, Any], batch_id: str, now_ms: int) -> str:
    job = state["jobs"][name]
    for res in client.messages.batches.results(batch_id):
        if res.custom_id not in (name, CUSTOM_ID):
            continue
        r = res.result
        if r.type != "succeeded":
            return _fail(job, f"batch result {r.type}: {getattr(getattr(r, 'error', None), 'type', '') or r.type}")
        msg = r.message
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            analysis = parse_analysis(text)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            return _fail(job, f"unparseable answer ({getattr(msg, 'stop_reason', '?')}): {e}")
        model = msg.model or "?"
        cost = batch_cost(model, msg.usage.input_tokens, msg.usage.output_tokens)
        sp = state["spend"]
        day = sp["by_day"].setdefault(_day(now_ms), {"calls": 0, "usd": 0.0})
        day["calls"] += 1
        day["usd"] = round(day["usd"] + cost, 4)
        sp["total_usd"] = round(sp["total_usd"] + cost, 4)
        sp["by_job"][name] = round(sp["by_job"].get(name, 0.0) + cost, 4)
        job.update(analysis=analysis, generated_ms=now_ms, model=model, last_cost_usd=round(cost, 4), failures=0, last_error=None)
        return f"ok (${cost:.3f}, total ${sp['total_usd']:.2f})"
    return _fail(job, "batch returned no result")


def _poll(client: Any, name: str, state: dict[str, Any], now_ms: int) -> str | None:
    """Check a job's pending batch once. Returns a status message when it finished, else None."""
    job = state["jobs"][name]
    pend = job["pending"]
    batch = client.messages.batches.retrieve(pend["id"])
    if batch.processing_status != "ended":
        if now_ms - pend["submitted_ms"] > 6 * HOUR:   # stuck: give up, start fresh next time
            try:
                client.messages.batches.cancel(pend["id"])
            except Exception:
                pass
            job["pending"] = None
            return _fail(job, "batch took more than 6 h")
        return None
    job["pending"] = None
    return _collect(client, name, state, pend["id"], now_ms)


def _previous_report(job: dict[str, Any]) -> dict[str, Any] | None:
    a = job["analysis"]
    if not a or not job["generated_ms"]:
        return None
    return {"generated": datetime.fromtimestamp(job["generated_ms"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "bias": a["bias"], "headline": a["headline"], "watch_levels": [w["level"] for w in a["watch"]]}


def page_info(state: dict[str, Any], status: dict[str, str]) -> dict[str, Any] | None:
    jobs = {}
    for n in JOB_ORDER:
        j = state["jobs"][n]
        jobs[n] = {"analysis": j["analysis"], "generated_ms": j["generated_ms"], "model": j["model"],
                   "last_cost_usd": j["last_cost_usd"]}
    return {"status": status, "spend_total_usd": state["spend"]["total_usd"], **jobs}


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
    """Advance the AI state machine (hourly note + deep report) by one build; returns what the page should show."""
    now_ms = now_ms or int(time.time() * 1000)
    cfg = _cfg()
    path = out_dir / STATE_NAME
    state = load_state(path)
    if mode == "off":
        return page_info(state, {"all": "off: disabled (--ai off)"})
    client = client or make_client()
    if client is None:
        return page_info(state, {"all": f"off: no API key (set ANTHROPIC_API_KEY or put it in {KEY_FILE})"})

    status: dict[str, str] = {}
    pace = plan(state, cfg, now_ms)
    facts: dict[str, Any] | None = None
    for name in JOB_ORDER:
        job, jc = state["jobs"][name], cfg["jobs"][name]
        try:
            if job["pending"]:
                status[name] = _poll(client, name, state, now_ms) or "batch still running"
            if job["pending"]:
                continue
            today = state["spend"]["by_day"].get(_day(now_ms), {"calls": 0, "usd": 0.0})
            est = job["last_cost_usd"] or jc["est"]
            due = mode == "force" or now_ms - job["last_submit_ms"] >= pace[name] * 60_000
            if now_ms >= _expiry_ms(cfg):
                status[name] = f"credit window over ({cfg['expires']})"
            elif now_ms < job["pause_until_ms"]:
                status[name] = status.get(name) or "paused after repeated failures"
            elif not due:
                status[name] = status.get(name) or ("not due yet" if pace[name] != float("inf") else "over budget pace")
            elif today["calls"] >= cfg["max_calls_day"]:
                status[name] = f"daily call cap reached ({cfg['max_calls_day']})"
            elif state["spend"]["total_usd"] + est > cfg["budget"]:
                status[name] = f"budget cap reached (${state['spend']['total_usd']:.2f} of ${cfg['budget']:.0f})"
            else:
                facts = facts or market_facts(payload, fetch())
                sheet = {**facts, "previous_report": _previous_report(job)} if name == "deep" else facts
                req = _request(sheet, jc, None, name, jc["max_tokens"], jc["system"])
                job["pending"] = {"id": client.messages.batches.create(requests=[req]).id, "submitted_ms": now_ms}
                job["last_submit_ms"] = now_ms
                status[name] = "submitted"
        except Exception as e:   # never let AI break the chart build
            status[name] = _fail(job, f"{type(e).__name__}: {str(e)[:200]}")

    t0 = time.time()   # most batches finish within minutes: wait a little for all of them together
    while any(state["jobs"][n]["pending"] and status.get(n) == "submitted" for n in JOB_ORDER):
        sleep(5)
        for name in JOB_ORDER:
            if state["jobs"][name]["pending"] and status.get(name) == "submitted":
                try:
                    done = _poll(client, name, state, now_ms + int((time.time() - t0) * 1000))
                except Exception as e:
                    done = _fail(state["jobs"][name], f"{type(e).__name__}: {str(e)[:200]}")
                if done is not None:
                    status[name] = done
        if time.time() >= t0 + cfg["wait_sec"]:
            break
    for name in JOB_ORDER:
        job = state["jobs"][name]
        if status.get(name) == "submitted" and job["pending"]:
            status[name] = "submitted (result will be collected by the next build)"
        if job["failures"] >= 3 and now_ms >= job["pause_until_ms"]:
            job["pause_until_ms"] = now_ms + 6 * HOUR   # e.g. credit used up / expired: stop hammering the API
            job["failures"] = 0
            status[name] = status.get(name, "") + " | 3 failures in a row: pausing 6 h"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        print(f"[ai] could not save state: {e}", file=sys.stderr)
    for n in JOB_ORDER:
        print(f"[ai] {n}: {status.get(n, '-')}", file=sys.stderr)
    return page_info(state, status)


def compare(
    payload: dict[str, Any],
    out_dir: Path,
    efforts: tuple[str, ...] | None = None,
    *,
    client: Any | None = None,
    fetch: Callable[[], dict[str, Any]] = fetch_timeframes,
    sleep: Callable[[float], None] = time.sleep,
    wait_sec: float = 1800,
) -> dict[str, Any]:
    """One batch, the same fact sheet at several effort levels; writes out/ai_compare.json and .md."""
    cfg = _cfg()
    efforts = efforts or tuple(os.environ.get("BTC_AI_COMPARE_EFFORTS", "high,xhigh,max").split(","))
    client = client or make_client()
    if client is None:
        raise RuntimeError(f"no API key (set ANTHROPIC_API_KEY or put it in {KEY_FILE})")
    facts = market_facts(payload, fetch())
    reqs = [_request(facts, cfg, e, f"effort-{e}", 32000) for e in efforts]
    batch = client.messages.batches.create(requests=reqs)
    print(f"[ai] compare batch {batch.id} submitted ({', '.join(efforts)})", file=sys.stderr)
    t0 = time.time()
    while client.messages.batches.retrieve(batch.id).processing_status != "ended":
        if time.time() - t0 > wait_sec:
            raise TimeoutError(f"batch {batch.id} still running; results stay available for 29 days")
        sleep(15)
    rows: dict[str, Any] = {}
    for res in client.messages.batches.results(batch.id):
        e = res.custom_id.removeprefix("effort-")
        r = res.result
        if r.type != "succeeded":
            rows[e] = {"error": f"{r.type}: {getattr(getattr(r, 'error', None), 'type', '')}"}
            continue
        m = r.message
        text = next((b.text for b in m.content if b.type == "text"), "")
        row: dict[str, Any] = {"model": m.model, "input_tokens": m.usage.input_tokens, "output_tokens": m.usage.output_tokens,
                               "stop_reason": getattr(m, "stop_reason", None),
                               "cost_usd": round(batch_cost(m.model or cfg["model"], m.usage.input_tokens, m.usage.output_tokens), 4)}
        try:
            row["analysis"] = parse_analysis(text)
        except (ValueError, KeyError, TypeError, AttributeError) as err:
            row["error"] = f"unparseable answer: {err}"
        rows[e] = row
    state = load_state(out_dir / STATE_NAME)   # keep the budget tracker honest
    spent = sum(r.get("cost_usd", 0) for r in rows.values())
    state["spend"]["total_usd"] = round(state["spend"]["total_usd"] + spent, 4)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / STATE_NAME).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / f"ai_compare_{cfg['model']}.json").write_text(json.dumps({"facts": facts, "results": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / f"ai_compare_{cfg['model']}.md").write_text(_compare_md(rows, efforts), encoding="utf-8")
    return rows


def _compare_md(rows: dict[str, Any], efforts: tuple[str, ...]) -> str:
    out = [f"# AI effort comparison", "", "| effort | output tokens | cost (batch) | stop |", "|---|---|---|---|"]
    for e in efforts:
        r = rows.get(e, {})
        out.append(f"| {e} | {r.get('output_tokens', '-')} | ${r.get('cost_usd', 0):.3f} | {r.get('stop_reason') or r.get('error', '-')} |")
    for e in efforts:
        a = rows.get(e, {}).get("analysis")
        out += ["", f"## {e}", ""]
        if not a:
            out.append(f"(no result: {rows.get(e, {}).get('error')})")
            continue
        out += [f"**{a['bias']}** - {a['headline']}", ""] + [f"- {x}" for x in a["summary"]]
        out += ["", "### Timeframes"] + [f"- {k}: {v}" for k, v in a["timeframes"].items()]
        out += ["", "### Flow", a["flow"], "", "### Watch"] + [f"- {w['level']}: {w['why']}" for w in a["watch"]]
        out += ["", "### Caveats"] + [f"- {x}" for x in a["caveats"]]
    return "\n".join(out) + "\n"


def status(out_dir: Path) -> str:
    st = load_state(out_dir / STATE_NAME)
    cfg, now = _cfg(), int(time.time() * 1000)
    sp = st["spend"]
    today = sp["by_day"].get(_day(now), {"calls": 0, "usd": 0.0})
    pace = plan(st, cfg, now)
    lines = [f"spent ${sp['total_usd']:.2f} of ${cfg['budget']:.0f} (by job: {sp['by_job']}); today {today['calls']} calls ${today['usd']:.2f}"]
    for n in JOB_ORDER:
        j = st["jobs"][n]
        every = "never" if pace[n] == float("inf") else f"every {pace[n] / 60:.1f} h"
        lines.append(f"  {n}: {cfg['jobs'][n]['model']} {cfg['jobs'][n]['effort']}, {every}, last cost {j['last_cost_usd']}, "
                     f"pending={bool(j['pending'])}, last error: {j['last_error']}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(status(Path(__file__).parent / "out"))
