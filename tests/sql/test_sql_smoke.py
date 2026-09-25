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
        c.execute("delete from wallet_edges; delete from cluster_scores; delete from wallets; delete from tokens;")
        c.commit()


def test_outcomes_selection_and_upsert_roundtrip():
    from memebot.db import conn
    from memebot.outcomes import evaluate as ev
    from memebot.outcomes.rules import Outcome

    with conn() as c:
        c.execute("insert into tokens(mint, deployer, launch_platform, created_at, pool_address) "
                  "values ('MINT_OUT', 'DEP_OUT', 'pumpfun', now() - interval '30 hours', 'POOL_OUT') "
                  "on conflict (mint) do nothing")
        c.commit()
    assert any(r["mint"] == "MINT_OUT" for r in ev.pending_rows(50))
    ev._upsert("MINT_OUT", 1234.5, Outcome(3.0, 1_700_000_000, True, "price_collapse", 0.96))
    ev._upsert("MINT_OUT", 1234.5, Outcome(3.0, 1_700_000_000, False, "none", 0.1))  # recovered → rug_at cleared
    with conn() as c:
        row = c.execute("select rugged, rug_at, rug_reason from token_outcomes where mint='MINT_OUT'").fetchone()
    assert row["rugged"] is False and row["rug_at"] is None and row["rug_reason"] == "none"
    assert not any(r["mint"] == "MINT_OUT" for r in ev.pending_rows(50))  # fresh evaluation is not re-selected
