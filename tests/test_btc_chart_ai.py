import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace as NS

import pytest

from btc_chart import ai
from btc_chart.sources import Candle, HOUR

NOW = 1792584000000
ANSWER = {
    "headline": "단기 횡보, 수급은 현물 우위",
    "bias": "중립·혼조",
    "summary": ["a", "b", "c"],
    "timeframes": {"15m": "x", "1h": "y", "4h": "z", "1d": "w"},
    "flow": "현물 CVD 우위",
    "context": "도미넌스 59.6%, MVRV 1.52",
    "watch": [{"level": "100,000", "why": "EMA50"}],
    "caveats": ["CVD는 24시간 창"],
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k in ("BTC_AI_MODEL", "BTC_AI_EFFORT", "BTC_AI_MIN_MINUTES", "BTC_AI_MAX_CALLS_PER_DAY", "BTC_AI_BUDGET_USD", "BTC_AI_EXPIRES", "BTC_AI_DEEP_MODEL", "BTC_AI_DEEP_EFFORT", "BTC_AI_DEEP_MIN_MINUTES", "BTC_AI_DEEP"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BTC_AI_WAIT_SEC", "0")
    monkeypatch.setenv("BTC_AI_MODEL", "claude-opus-5-5")   # most tests exercise the two-job (Opus hourly + Fable deep) setup
    monkeypatch.setenv("BTC_AI_DEEP", "on")


def payload():
    n = 30
    return {
        "generated": "2026. 10. 20. 12:00:00", "tz": "UTC+9",
        "candles": [[NOW - (n - i) * HOUR, 1, 2, 0, 100 + i] for i in range(n)],
        "cvd": [{"key": "binance_spot", "values": [float(i * 10) for i in range(n)]}, {"key": "okx", "values": None}],
        "extra": [{"key": "binance_oi", "values": [1e10 + i * 1e7 for i in range(n)]},
                  {"key": "cb_premium", "values": [0.01 * i for i in range(n)]}],
        "fvg": [{"tf": "4H", "kind": "bull", "lo": 90.0, "hi": 95.0}],
    }


def tf_facts():
    cs = [Candle(i * HOUR, 100, 101, 99, 100 + i, 1000.0, 600.0) for i in range(300)]
    return {tf: ai.summarize_candles(tf, cs) for tf in ai.TFS}


def message(text, model="claude-opus-5-5", i=3000, o=2000):
    return NS(content=[NS(type="thinking"), NS(type="text", text=text)], model=model, usage=NS(input_tokens=i, output_tokens=o),
              stop_reason="end_turn")


class FakeClient:
    """Minimal stand-in for anthropic.Anthropic().messages.batches; remembers what each batch contained."""

    def __init__(self, ended=True, result_type="succeeded", text=None, fail_create=None):
        self.ended, self.result_type, self.fail_create = ended, result_type, fail_create
        self.text = json.dumps(ANSWER, ensure_ascii=False) if text is None else text
        self.created = []            # one list of requests per batch
        self.messages = NS(batches=NS(create=self.create, retrieve=self.retrieve, results=self.results, cancel=lambda i: None))

    def create(self, requests):
        if self.fail_create:
            raise self.fail_create
        self.created.append(requests)
        return NS(id=f"msgbatch_{len(self.created)}")

    def retrieve(self, id):
        return NS(processing_status="ended" if self.ended else "in_progress")

    def results(self, id):
        reqs = self.created[int(id.split("_")[1]) - 1]
        return [NS(custom_id=r["custom_id"], result=NS(type=self.result_type, message=message(self.text, model=r["params"]["model"],
                o=1000 if r["params"]["model"].startswith("claude-fable") else 2000), error=NS(type="invalid_request")))
                for r in reqs]

    def jobs(self):
        return [r["custom_id"] for reqs in self.created for r in reqs]


def run(tmp_path, client, **kw):
    return ai.run(payload(), tmp_path, now_ms=kw.pop("now_ms", NOW), client=client, fetch=tf_facts, sleep=lambda s: None, **kw)


def test_market_facts_only_contains_computed_numbers():
    f = ai.market_facts(payload(), tf_facts())
    assert f["price"] == 129
    assert f["net_taker_buy_usd_last_24h"] == {"binance_spot": 240, "okx": None}
    assert f["binance_futures_open_interest_usd"]["change_24h_pct"] > 0
    assert f["unfilled_fvg_zones"][0]["distance_pct"] < 0
    assert set(f["timeframes"]) == set(ai.TFS)
    json.dumps(f)


def test_parse_analysis_trims_and_requires_headline():
    long = dict(ANSWER, headline="x" * 500, summary=["s"] * 20)
    out = ai.parse_analysis(json.dumps(long))
    assert len(out["headline"]) == 200 and len(out["summary"]) == 6
    with pytest.raises(KeyError):
        ai.parse_analysis("{}")


def test_batch_cost_is_half_price():
    assert ai.batch_cost("claude-opus-5-5", 1_000_000, 0) == pytest.approx(2.0)
    assert ai.batch_cost("claude-opus-5-5", 0, 1_000_000) == pytest.approx(10.0)
    assert ai.batch_cost("claude-fable-5-1", 1_000_000, 1_000_000) == pytest.approx(30.0)


def test_no_key_means_off_and_never_crashes(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ai, "KEY_FILE", tmp_path / "nokey.txt")
    info = ai.run(payload(), tmp_path, now_ms=NOW, fetch=tf_facts)
    assert info["hourly"]["analysis"] is None and info["status"]["all"].startswith("off")


def test_mode_off_skips_everything(tmp_path):
    c = FakeClient()
    assert ai.run(payload(), tmp_path, mode="off", client=c)["status"]["all"].startswith("off")
    assert not c.created


def test_first_build_submits_both_jobs_with_their_own_model_and_collects(tmp_path):
    c = FakeClient()
    info = run(tmp_path, c)
    assert c.jobs() == ["deep", "hourly"]
    by = {r["custom_id"]: r["params"] for reqs in c.created for r in reqs}
    assert by["hourly"]["model"] == "claude-opus-5-5" and by["deep"]["model"] == "claude-fable-5-1"
    assert by["hourly"]["output_config"]["effort"] == "xhigh" and by["hourly"]["max_tokens"] == 24000
    assert by["deep"]["max_tokens"] == 32000 and "deeper briefing" in by["deep"]["system"]
    assert "deeper briefing" not in by["hourly"]["system"]
    assert by["hourly"]["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in by["hourly"] and "temperature" not in by["hourly"]
    assert info["hourly"]["analysis"]["headline"] == ANSWER["headline"] and info["deep"]["model"] == "claude-fable-5-1"
    st = ai.load_state(tmp_path / "ai.json")
    assert st["jobs"]["hourly"]["pending"] is None
    assert st["spend"]["by_job"]["hourly"] == pytest.approx(0.026, abs=1e-6)      # (3000*4+2000*20)/1e6/2
    assert st["spend"]["by_job"]["deep"] == pytest.approx(0.04, abs=1e-6)         # (3000*10+1000*50)/1e6/2
    assert st["spend"]["total_usd"] == pytest.approx(0.066, abs=1e-6)


def test_default_is_one_fable_job_and_the_deep_job_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv("BTC_AI_MODEL")
    monkeypatch.delenv("BTC_AI_DEEP")
    c = FakeClient()
    info = run(tmp_path, c)
    assert c.jobs() == ["hourly"]
    assert c.created[0][0]["params"]["model"] == "claude-fable-5-1"
    assert info["hourly"]["analysis"] and info["deep"]["analysis"] is None
    assert ai.plan(ai._empty_state(), ai._cfg(), NOW)["deep"] == float("inf")


def test_jobs_run_on_their_own_schedules(tmp_path):
    c = FakeClient()
    run(tmp_path, c)
    run(tmp_path, c, now_ms=NOW + 10 * 60_000)
    assert len(c.jobs()) == 2                                  # nothing due yet
    run(tmp_path, c, now_ms=NOW + 60 * 60_000)
    assert c.jobs() == ["deep", "hourly", "hourly"]            # only the hourly note
    run(tmp_path, c, now_ms=NOW + 12 * HOUR)
    assert c.jobs()[-2:] == ["deep", "hourly"]


def test_deep_request_carries_previous_report(tmp_path):
    c = FakeClient()
    run(tmp_path, c)
    run(tmp_path, c, now_ms=NOW + 12 * HOUR)
    deep = [r for reqs in c.created for r in reqs if r["custom_id"] == "deep"]
    assert "previous_report" in deep[1]["params"]["messages"][0]["content"]
    assert ANSWER["headline"] in deep[1]["params"]["messages"][0]["content"]
    assert "\"previous_report\": null" in deep[0]["params"]["messages"][0]["content"]


def test_pending_batch_is_collected_by_next_build(tmp_path):
    c = FakeClient(ended=False)
    first = run(tmp_path, c)
    assert first["hourly"]["analysis"] is None
    assert ai.load_state(tmp_path / "ai.json")["jobs"]["hourly"]["pending"]["id"] == "msgbatch_2"
    c.ended = True
    second = run(tmp_path, c, now_ms=NOW + 30 * 60_000)
    assert second["hourly"]["analysis"]["bias"] == "중립·혼조" and second["deep"]["analysis"] and len(c.jobs()) == 2


def test_plan_slows_down_when_calls_get_more_expensive_and_stops_when_broke():
    cfg, now = ai._cfg(), NOW
    st = ai._empty_state()
    st["jobs"]["hourly"]["last_cost_usd"], st["jobs"]["deep"]["last_cost_usd"] = 0.06, 0.15   # Opus hourly + Fable deep
    base = ai.plan(st, cfg, now)
    assert base["hourly"] == 55 and base["deep"] == 660
    st["jobs"]["hourly"]["last_cost_usd"] = 0.6                 # e.g. many more facts / tokens
    slower = ai.plan(st, cfg, now)
    assert slower["hourly"] > 55 and slower["deep"] == 660      # the deep report keeps its slot
    st["spend"]["total_usd"] = 89.9
    assert ai.plan(st, cfg, now)["hourly"] == float("inf")


def test_daily_call_cap_and_total_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_AI_MAX_CALLS_PER_DAY", "2")
    c = FakeClient()
    run(tmp_path, c)
    info = run(tmp_path, c, now_ms=NOW + 2 * HOUR)
    assert len(c.jobs()) == 2 and "daily call cap" in info["status"]["hourly"]
    monkeypatch.setenv("BTC_AI_MAX_CALLS_PER_DAY", "99")
    monkeypatch.setenv("BTC_AI_BUDGET_USD", "0.07")
    info = run(tmp_path, c, now_ms=NOW + 3 * HOUR)
    assert len(c.jobs()) == 2 and ("budget" in info["status"]["hourly"] or "pace" in info["status"]["hourly"])


def test_nothing_is_submitted_after_the_credit_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_AI_EXPIRES", "2026-10-01")
    c = FakeClient()
    info = run(tmp_path, c)                      # NOW is after the expiry date
    assert not c.created and "credit window over" in info["status"]["hourly"]


def test_failures_keep_old_text_and_pause_after_three(tmp_path):
    run(tmp_path, FakeClient())
    bad = FakeClient(fail_create=RuntimeError("credit balance too low"))
    for k in range(3):
        info = run(tmp_path, bad, now_ms=NOW + (k + 1) * 2 * HOUR)
    assert info["hourly"]["analysis"]["headline"] == ANSWER["headline"]       # last good text stays
    assert "pausing" in info["status"]["hourly"]
    ok = FakeClient()
    paused = run(tmp_path, ok, now_ms=NOW + 8 * HOUR)
    assert "paused" in paused["status"]["hourly"]
    run(tmp_path, ok, now_ms=NOW + 13 * HOUR)
    assert "hourly" in ok.jobs()


def test_old_single_job_state_file_is_migrated(tmp_path):
    (tmp_path / "ai.json").write_text(json.dumps({"analysis": ANSWER, "generated_ms": 5, "model": "claude-opus-5-5", "pending": None,
                                                  "last_submit_ms": 0, "spend": {"total_usd": 0.7, "by_day": {}}}), encoding="utf-8")
    st = ai.load_state(tmp_path / "ai.json")
    assert st["jobs"]["hourly"]["analysis"] == ANSWER and st["spend"]["total_usd"] == 0.7 and st["jobs"]["deep"]["analysis"] is None


def test_errored_or_garbled_answer_does_not_crash(tmp_path):
    assert run(tmp_path, FakeClient(result_type="errored"))["hourly"]["analysis"] is None
    assert run(tmp_path / "b", FakeClient(text="not json"))["hourly"]["analysis"] is None


# --- the real SDK against a local stub of the Batches API: checks the actual HTTP shape ---


def batch(status, port=0):
    return {"id": "msgbatch_1", "type": "message_batch", "processing_status": status,
            "request_counts": {"processing": 0, "succeeded": 1, "errored": 0, "canceled": 0, "expired": 0},
            "created_at": "2026-10-20T00:00:00Z", "expires_at": "2026-10-21T00:00:00Z", "ended_at": None,
            "cancel_initiated_at": None, "results_url": f"http://127.0.0.1:{port}/v1/messages/batches/msgbatch_1/results" if status == "ended" else None}


class Stub(BaseHTTPRequestHandler):
    log = []
    ids = []

    def _send(self, body, ctype="application/json"):
        b = body.encode()
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        Stub.log.append((self.path, body))
        Stub.ids += [(r["custom_id"], r["params"]["model"]) for r in body["requests"]]
        self._send(json.dumps(batch("in_progress")))

    def do_GET(self):
        Stub.log.append((self.path, None))
        if self.path.endswith("/results"):
            lines = [{"custom_id": cid, "result": {"type": "succeeded", "message": {
                "id": "msg_1", "type": "message", "role": "assistant", "model": model, "stop_reason": "end_turn",
                "stop_sequence": None, "content": [{"type": "text", "text": json.dumps(ANSWER, ensure_ascii=False)}],
                "usage": {"input_tokens": 3000, "output_tokens": 2000}}}} for cid, model in Stub.ids]
            nl = chr(10)
            self._send(nl.join(json.dumps(x) for x in lines) + nl, "application/x-jsonl")
        else:
            self._send(json.dumps(batch("ended", self.server.server_port)))

    def log_message(self, *a):
        pass


def test_real_sdk_round_trip_against_stub_server(tmp_path, monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = anthropic.Anthropic(api_key="test-key", base_url=f"http://127.0.0.1:{srv.server_port}", max_retries=0)
        monkeypatch.setenv("BTC_AI_WAIT_SEC", "30")
        info = ai.run(payload(), tmp_path, now_ms=NOW, client=client, fetch=tf_facts, sleep=lambda s: None)
    finally:
        srv.shutdown()
    assert info["hourly"]["analysis"]["headline"] == ANSWER["headline"], info["status"]
    assert info["deep"]["analysis"]["headline"] == ANSWER["headline"], info["status"]
    posts = [(p, b) for p, b in Stub.log if b]
    assert all(p == "/v1/messages/batches" for p, _ in posts) and len(posts) == 2
    by_job = {b["requests"][0]["custom_id"]: b["requests"][0]["params"] for _, b in posts}
    assert by_job["deep"]["model"] == "claude-fable-5-1"
    params = by_job["hourly"]
    assert params["output_config"]["effort"] == "xhigh" and params["max_tokens"] == 24000
    assert "Fact sheet" in params["messages"][0]["content"]


def test_compare_sends_one_request_per_effort_and_writes_report(tmp_path):
    class Cmp(FakeClient):
        def results(self, id):
            out = []
            for r in self.created[0]:
                e = r["custom_id"]
                tokens = {"effort-high": 5000, "effort-xhigh": 9000, "effort-max": 14000}[e]
                out.append(NS(custom_id=e, result=NS(type="succeeded", message=message(self.text, o=tokens), error=None)))
            return out

    c = Cmp()
    rows = ai.compare(payload(), tmp_path, client=c, fetch=tf_facts, sleep=lambda s: None)
    reqs = c.created[0]
    assert [r["params"]["output_config"]["effort"] for r in reqs] == ["high", "xhigh", "max"]
    assert all(r["params"]["max_tokens"] == 32000 for r in reqs)
    assert rows["max"]["cost_usd"] > rows["high"]["cost_usd"] > 0
    md = (tmp_path / "ai_compare_claude-opus-5-5.md").read_text(encoding="utf-8")
    assert "## xhigh" in md and ANSWER["headline"] in md
    assert ai.load_state(tmp_path / "ai.json")["spend"]["total_usd"] == pytest.approx(sum(r["cost_usd"] for r in rows.values()))
