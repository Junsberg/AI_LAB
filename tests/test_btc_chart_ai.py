import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace as NS

import pytest

from btc_chart import ai
from btc_chart.sources import Candle, HOUR

NOW = 1_800_000_000_000
ANSWER = {
    "headline": "단기 횡보, 수급은 현물 우위",
    "bias": "중립·혼조",
    "summary": ["a", "b", "c"],
    "timeframes": {"15m": "x", "1h": "y", "4h": "z", "1d": "w"},
    "flow": "현물 CVD 우위",
    "watch": [{"level": "100,000", "why": "EMA50"}],
    "caveats": ["CVD는 24시간 창"],
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k in ("BTC_AI_MODEL", "BTC_AI_EFFORT", "BTC_AI_MIN_MINUTES", "BTC_AI_MAX_CALLS_PER_DAY", "BTC_AI_BUDGET_USD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BTC_AI_WAIT_SEC", "0")


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
    return NS(content=[NS(type="thinking"), NS(type="text", text=text)], model=model, usage=NS(input_tokens=i, output_tokens=o))


class FakeClient:
    def __init__(self, ended=True, result_type="succeeded", text=None, fail_create=None):
        self.ended, self.result_type, self.fail_create = ended, result_type, fail_create
        self.text = json.dumps(ANSWER, ensure_ascii=False) if text is None else text
        self.created = []
        self.messages = NS(batches=NS(create=self.create, retrieve=self.retrieve, results=self.results, cancel=lambda i: None))

    def create(self, requests):
        if self.fail_create:
            raise self.fail_create
        self.created.append(requests)
        return NS(id="msgbatch_1")

    def retrieve(self, id):
        return NS(processing_status="ended" if self.ended else "in_progress")

    def results(self, id):
        r = NS(type=self.result_type, message=message(self.text), error=NS(type="invalid_request"))
        return [NS(custom_id=ai.CUSTOM_ID, result=r)]


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


def test_no_key_means_off_and_never_crashes(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ai, "KEY_FILE", tmp_path / "nokey.txt")
    info = ai.run(payload(), tmp_path, now_ms=NOW, fetch=tf_facts)
    assert info["analysis"] is None and info["status"].startswith("off")


def test_mode_off_skips_everything(tmp_path):
    c = FakeClient()
    assert ai.run(payload(), tmp_path, mode="off", client=c)["status"].startswith("off")
    assert not c.created


def test_submit_collect_and_budget_tracking(tmp_path):
    c = FakeClient()
    info = run(tmp_path, c)
    assert info["analysis"]["headline"] == ANSWER["headline"]
    req = c.created[0][0]
    assert req["custom_id"] == "analysis" and req["params"]["model"] == "claude-opus-5-5"
    assert req["params"]["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in req["params"] and "temperature" not in req["params"]
    st = ai.load_state(tmp_path / "ai.json")
    assert st["pending"] is None and st["spend"]["total_usd"] == pytest.approx(0.026, abs=1e-6)  # (3000*4+2000*20)/1e6/2


def test_not_due_again_within_min_interval(tmp_path):
    c = FakeClient()
    run(tmp_path, c)
    again = run(tmp_path, c, now_ms=NOW + 10 * 60_000)
    assert len(c.created) == 1 and again["analysis"] is not None
    run(tmp_path, c, now_ms=NOW + 56 * 60_000)
    assert len(c.created) == 2


def test_pending_batch_is_collected_by_next_build(tmp_path):
    c = FakeClient(ended=False)
    first = run(tmp_path, c)
    assert first["analysis"] is None and ai.load_state(tmp_path / "ai.json")["pending"]["id"] == "msgbatch_1"
    c.ended = True
    second = run(tmp_path, c, now_ms=NOW + 30 * 60_000)
    assert second["analysis"]["bias"] == "중립·혼조" and len(c.created) == 1


def test_daily_call_cap_and_total_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_AI_MAX_CALLS_PER_DAY", "1")
    c = FakeClient()
    run(tmp_path, c)
    info = run(tmp_path, c, now_ms=NOW + 2 * HOUR)
    assert len(c.created) == 1 and "daily call cap" in info["status"]
    monkeypatch.setenv("BTC_AI_MAX_CALLS_PER_DAY", "99")
    monkeypatch.setenv("BTC_AI_BUDGET_USD", "0.01")
    info = run(tmp_path, c, now_ms=NOW + 3 * HOUR)
    assert len(c.created) == 1 and "budget cap" in info["status"]


def test_failures_keep_old_text_and_pause_after_three(tmp_path):
    run(tmp_path, FakeClient())
    bad = FakeClient(fail_create=RuntimeError("credit balance too low"))
    for k in range(3):
        info = run(tmp_path, bad, now_ms=NOW + (k + 1) * 2 * HOUR)
    assert info["analysis"]["headline"] == ANSWER["headline"]       # last good text stays
    assert "pausing" in info["status"]
    ok = FakeClient()
    paused = run(tmp_path, ok, now_ms=NOW + 8 * HOUR)
    assert not ok.created and "paused" in paused["status"]
    run(tmp_path, ok, now_ms=NOW + 13 * HOUR)
    assert len(ok.created) == 1


def test_errored_or_garbled_answer_does_not_crash(tmp_path):
    assert run(tmp_path, FakeClient(result_type="errored"))["analysis"] is None
    assert run(tmp_path / "b", FakeClient(text="not json"))["analysis"] is None


# --- the real SDK against a local stub of the Batches API: checks the actual HTTP shape ---


def batch(status, port=0):
    return {"id": "msgbatch_1", "type": "message_batch", "processing_status": status,
            "request_counts": {"processing": 0, "succeeded": 1, "errored": 0, "canceled": 0, "expired": 0},
            "created_at": "2026-10-20T00:00:00Z", "expires_at": "2026-10-21T00:00:00Z", "ended_at": None,
            "cancel_initiated_at": None, "results_url": f"http://127.0.0.1:{port}/v1/messages/batches/msgbatch_1/results" if status == "ended" else None}


class Stub(BaseHTTPRequestHandler):
    log = []

    def _send(self, body, ctype="application/json"):
        b = body.encode()
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        Stub.log.append((self.path, json.loads(self.rfile.read(int(self.headers["content-length"])))))
        self._send(json.dumps(batch("in_progress")))

    def do_GET(self):
        Stub.log.append((self.path, None))
        if self.path.endswith("/results"):
            line = {"custom_id": "analysis", "result": {"type": "succeeded", "message": {
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "end_turn",
                "stop_sequence": None, "content": [{"type": "text", "text": json.dumps(ANSWER, ensure_ascii=False)}],
                "usage": {"input_tokens": 3000, "output_tokens": 2000}}}}
            self._send(json.dumps(line) + "\n", "application/x-jsonl")
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
    assert info["analysis"]["headline"] == ANSWER["headline"], info["status"]
    path, body = Stub.log[0]
    assert path == "/v1/messages/batches"
    params = body["requests"][0]["params"]
    assert params["output_config"]["effort"] == "high" and params["max_tokens"] == 16000
    assert "Fact sheet" in params["messages"][0]["content"]
