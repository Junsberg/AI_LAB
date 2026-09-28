"""Run every SQL path against a real (empty) Postgres. Skipped unless TEST_DATABASE_URL is
set — CI provides a postgres service; local runs without one skip cleanly."""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")


@pytest.fixture(scope="module", autouse=True)
def db(monkeypatch_module):
    from memebot import config
    from memebot import db as dbmod

    monkeypatch_module.setattr(config.settings, "database_url", os.environ["TEST_DATABASE_URL"])
    dbmod.migrate()
    yield


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


def test_migrations_idempotent():
    from memebot.db import migrate

    assert migrate()  # second application must not fail


def test_structural_owners_query():
    from memebot.collectors.rugcheck import structural_owners

    assert structural_owners() == set()


def test_lineage_job_queries():
    from memebot.lineage.job import (
        prune_orphans,
        rebuild_clusters,
        refresh_cluster_scores,
        repair_invalid_traces,
    )

    assert repair_invalid_traces() == {"retraced": 0, "structural_deployers_marked": 0}
    assert prune_orphans() == {"untagged": 0, "edges_pruned": 0}
    assert rebuild_clusters() == 0
    assert refresh_cluster_scores() == 0


def test_health_and_stats_queries(tmp_path, monkeypatch):
    from memebot.review import health, stats

    monkeypatch.setattr(health, "OUT", tmp_path / "health.json")
    monkeypatch.setattr(stats, "OUT", tmp_path / "stats")
    h = health.run()
    assert h["status"] in ("ok", "warn", "critical")
    assert stats.run().exists()


def test_rugcheck_selection_query():
    from memebot.db import conn

    with conn() as c:
        c.execute(
            """select mint from tokens
               where meta->'risk' is null
                  or (((meta->'risk'->>'unavailable')::bool or meta->'risk'->>'top10_pct' is null)
                      and coalesce((meta->'risk'->>'attempts')::int, 0) < 4
                      and created_at < now() - interval '20 minutes')
                  or ((meta->'risk'->>'top10_pct')::numeric > 90
                      and coalesce((meta->'risk'->>'recheck')::int, 0) < 2)
               order by (meta->'risk' is null) desc, created_at desc limit 1"""
        ).fetchall()


def test_insert_paths_roundtrip():
    """Exercise the insert/upsert statements poll and job use, with realistic values."""
    import json
    from datetime import datetime, timezone

    from memebot.db import conn

    now = datetime.now(timezone.utc)
    with conn() as c:
        meta = {"deployer_source": "rugcheck", "dex": "pumpswap", "stage": "graduated", "deployer_kind": "wallet",
                "risk": {"top10_pct": 12.5, "raw_top": [{"owner": "A" * 44, "pct": 12.5}], "attempts": 1, "recheck": 0}}
        c.execute(
            """insert into tokens(mint, symbol, deployer, launch_platform, created_at, migrated_at, pool_address, first_seen_slot, meta)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) on conflict (mint) do nothing""",
            ("M" * 44, "TST", "D" * 44, "pumpfun", now, now, "P" * 44, 1, json.dumps(meta)),
        )
        c.execute("""insert into wallets(address, tags) values (%s, '{deployer}')
                     on conflict (address) do update set tags = (select array(select distinct unnest(wallets.tags || '{deployer}')))""", ("D" * 44,))
        c.execute(
            """insert into wallets(address, funded_by, funded_at, funding_source_type) values (%s,%s,%s,%s)
               on conflict (address) do update set
                 funded_by = coalesce(wallets.funded_by, excluded.funded_by),
                 funded_at = coalesce(wallets.funded_at, excluded.funded_at),
                 funding_source_type = case when wallets.funded_by is null then excluded.funding_source_type else wallets.funding_source_type end
               where coalesce((wallets.meta->>'invalid_trace')::int, 0) = 0""",
            ("D" * 44, "F" * 44, now, "wallet"),
        )
        c.execute("insert into wallet_edges(src, dst, kind, amount_sol) values (%s,%s,%s,%s) on conflict do nothing", ("F" * 44, "D" * 44, "funded", 1.5))
        c.commit()
    from memebot.collectors.rugcheck import structural_owners
    from memebot.lineage.job import rebuild_clusters, refresh_cluster_scores, repair_invalid_traces

    assert structural_owners() == set()
    repair_invalid_traces()
    assert rebuild_clusters() >= 1
    assert refresh_cluster_scores() >= 1
    with conn() as c:
        c.execute("delete from wallet_edges; delete from cluster_scores; delete from wallets; "
                  "delete from token_outcomes; delete from tokens;")
        c.commit()


def test_outcomes_selection_and_upsert_roundtrip(monkeypatch):
    from memebot.db import conn
    from memebot.outcomes import evaluate as ev
    from memebot.outcomes.rules import Outcome

    with conn() as c:
        c.execute("delete from token_outcomes where mint='MINT_OUT'")  # rerunnable on a persistent DB
        c.execute("insert into tokens(mint, deployer, launch_platform, created_at, pool_address) "
                  "values ('MINT_OUT', 'DEP_OUT', 'pumpfun', now() - interval '30 hours', 'POOL_OUT') "
                  "on conflict (mint) do nothing")
        c.commit()
    with conn() as c:
        c.execute("""update tokens set meta = coalesce(meta, '{}'::jsonb) || '{"reserve_usd_at_seen": 12000.0}'::jsonb
                     where mint='MINT_OUT'""")
        c.commit()
    row = next(r for r in ev.pending_rows(50) if r["mint"] == "MINT_OUT")
    assert ev.liquidity_peak(row["liq_peak"], row["liq_seen"], 0.0) == 12000.0
    ev._upsert("MINT_OUT", 1234.5, Outcome(3.0, 1_700_000_000, True, "price_collapse", 0.96))
    ev._upsert("MINT_OUT", 1234.5, Outcome(3.0, 1_700_000_000, False, "none", 0.1))  # recovered → rug_at cleared
    with conn() as c:
        row = c.execute("select rugged, rug_at, rug_reason from token_outcomes where mint='MINT_OUT'").fetchone()
    assert row["rugged"] is False and row["rug_at"] is None and row["rug_reason"] == "none"
    assert not any(r["mint"] == "MINT_OUT" for r in ev.pending_rows(50))  # fresh evaluation is not re-selected


def test_paper_replay_selects_point_in_time_cluster(monkeypatch):
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone

    from memebot.config import load_params
    from memebot.db import conn
    from memebot.review import paper_replay as pr

    t0 = datetime.now(timezone.utc) - timedelta(days=3)
    risk = json.dumps({"risk": {"top10_pct": 10.0}})
    with conn() as c:
        c.execute("delete from token_outcomes where mint like 'RP%%'")
        c.execute("delete from tokens where mint like 'RP%%'")
        c.execute("insert into wallets(address, cluster_id, tags) values ('RPDEP', 'RPCLUSTER', '{deployer}') "
                  "on conflict (address) do update set cluster_id = 'RPCLUSTER'")
        for i in range(12):  # 12 clean history tokens two days before the candidate
            c.execute("insert into tokens(mint, deployer, launch_platform, created_at, pool_address, meta) "
                      "values (%s, 'RPDEP', 'pumpfun', %s, %s, %s::jsonb)",
                      (f"RPH{i}", t0 - timedelta(days=2), f"RPPOOLH{i}", risk))
            c.execute("insert into token_outcomes(mint, peak_multiple, rugged, rug_reason, evaluated_at) "
                      "values (%s, 2.0, false, 'none', now())", (f"RPH{i}",))
        c.execute("insert into tokens(mint, deployer, launch_platform, created_at, pool_address, meta) "
                  "values ('RPCAND', 'RPDEP', 'pumpfun', %s, 'RPPOOLC', %s::jsonb)", (t0, risk))
        c.commit()

    async def no_fetch(*a, **k):
        raise pr.BudgetExhausted  # stop before any network call

    monkeypatch.setattr(pr, "_get", no_fetch)
    rep = asyncio.run(pr.run(load_params(), budget_s=1))
    assert rep["entries_selected"] >= 1  # RPCAND: 12 evaluated, 0 rugged → score 13.5/15 = 0.9
    with conn() as c:
        c.execute("delete from token_outcomes where mint like 'RP%%'")
        c.execute("delete from tokens where mint like 'RP%%'")
        c.execute("delete from wallets where address = 'RPDEP'")
        c.commit()


def test_paper_runner_enters_manages_and_closes(monkeypatch):
    import asyncio
    import json
    from datetime import datetime, timezone

    import httpx

    from memebot.config import load_params
    from memebot.db import conn, migrate
    from memebot.execution import paper_runner as pr

    migrate()
    now = datetime.now(timezone.utc)
    with conn() as c:
        for t in ("fills", "positions", "signals"):
            c.execute(f"delete from {t}")
        c.execute("delete from tokens where mint = 'PRMINT'")
        c.execute("insert into wallets(address, cluster_id, tags) values ('PRDEP', 'PRC', '{deployer}') "
                  "on conflict (address) do update set cluster_id = 'PRC'")
        c.execute("insert into cluster_scores(cluster_id, tokens_total, tokens_evaluated, tokens_rugged, tokens_10x, "
                  "score, updated_at) values ('PRC', 12, 12, 0, 0, 0.95, now()) on conflict (cluster_id) do update "
                  "set score = 0.95, tokens_evaluated = 12")
        c.execute("insert into tokens(mint, deployer, launch_platform, created_at, pool_address, meta) "
                  "values ('PRMINT', 'PRDEP', 'pumpfun', now(), 'PRPOOL', %s::jsonb)",
                  (json.dumps({"seen_at": now.isoformat(), "risk": {"top10_pct": 10.0}}),))
        c.commit()

    async def fake_pools(client, pools, deadline):
        return {p: {"reserve_in_usd": "20000", "quote_token_price_usd": "200", "base_token_price_usd": "1"}
                for p in pools}

    start = int(now.timestamp()) // 300 * 300
    candles = [[start, 1.0, 1.0, 1.0, 1.0, 10], [start + 300, 1.0, 1.0, 0.3, 0.3, 10]]  # hard stop

    async def fake_get(client, url, params, deadline):
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": candles}}},
                              request=httpx.Request("GET", url))

    async def no_sleep(s):
        pass

    monkeypatch.setattr(pr, "_pools", fake_pools)
    monkeypatch.setattr(pr, "_get", fake_get)
    monkeypatch.setattr(pr.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(pr, "RECHECK_AFTER", pr.timedelta(0))
    stats = asyncio.run(pr.run(load_params()))
    assert stats["decided"] == {"enter": 1} and stats["positions"] == {"closed": 1}
    with conn() as c:
        pos = c.execute("select * from positions where mint = 'PRMINT'").fetchone()
        fills = c.execute("select reason from fills where position_id = %s", (pos["id"],)).fetchall()
    assert pos["mode"] == "paper" and pos["exit_reason"] == "hard_stop" and float(pos["pnl_sol"]) < 0
    assert [f["reason"] for f in fills] == ["hard_stop"]
    assert asyncio.run(pr.run(load_params()))["decided"] == {}  # decided once per mint

    # open path: half sold at 2x, still running → one take_initial fill, no close, no mark fill
    candles[:] = [[start, 1.0, 1.0, 1.0, 1.0, 10], [start + 300, 1.0, 2.5, 1.0, 2.5, 10]]
    with conn() as c:
        c.execute("update positions set closed_at = null, exit_reason = null, pnl_sol = null where mint = 'PRMINT'")
        c.execute("delete from fills")
        c.commit()
    monkeypatch.setattr(pr, "_candidates", lambda p: [])
    assert asyncio.run(pr.run(load_params()))["positions"] == {"open": 1}
    with conn() as c:
        pos = c.execute("select * from positions where mint = 'PRMINT'").fetchone()
        fills = c.execute("select reason from fills where position_id = %s", (pos["id"],)).fetchall()
    assert pos["closed_at"] is None and float(pos["remaining_tokens"]) == 0.5
    assert [f["reason"] for f in fills] == ["take_initial"]
    with conn() as c:
        for t in ("fills", "positions", "signals"):
            c.execute(f"delete from {t}")
        c.execute("delete from tokens where mint = 'PRMINT'")
        c.execute("delete from cluster_scores where cluster_id = 'PRC'")
        c.execute("delete from wallets where address = 'PRDEP'")
        c.commit()
