"""Deterministic replay: re-run `decide()` over stored `signals.snapshot` rows with a
different params file and report what would have been entered and the realized pnl of
those that were (paper or live) — the cheap first gate for every hypothesis.

Positions we never opened have no pnl; replay therefore measures *filtering* changes
exactly and *sizing/exit* changes approximately. The paper window covers the rest.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import typer

from memebot.config import Params, load_params
from memebot.db import conn
from memebot.strategy.scorer import Candidate, decide

app = typer.Typer()


def _candidate(row: dict) -> Candidate:
    s = row["snapshot"] if isinstance(row["snapshot"], dict) else json.loads(row["snapshot"])
    return Candidate(
        mint=row["mint"],
        lineage=row["lineage_score"],
        kol=row["kol_score"] or 0.0,
        survivor=row["survivor_score"] or 0.0,
        narrative=row["narrative_score"] or 0.0,
        liquidity_sol=s.get("liquidity_sol", 0.0),
        top10_pct=s.get("top10_pct", 100.0),
        bundle_pct=s.get("bundle_pct", 100.0),
        snapshot=s,
    )


def run(params: Params, since: datetime) -> dict:
    with conn() as c:
        rows = c.execute(
            """select s.*, p.pnl_sol from signals s
               left join positions p on p.signal_id = s.id and p.closed_at is not null
               where s.ts >= %s order by s.ts""",
            (since,),
        ).fetchall()
    entered = 0
    pnl_known = 0.0
    n_known = 0
    for r in rows:
        d = decide(_candidate(r), params, open_positions=0, daily_pnl_sol=0.0)
        if d.decision != "enter":
            continue
        entered += 1
        if r["pnl_sol"] is not None:
            pnl_known += float(r["pnl_sol"])
            n_known += 1
    return {
        "signals": len(rows),
        "would_enter": entered,
        "known_outcomes": n_known,
        "pnl_sol": round(pnl_known, 4),
        "expectancy_sol": round(pnl_known / n_known, 4) if n_known else None,
    }


@app.command()
def main(params: Path = typer.Option(..., exists=True), since: datetime = typer.Option(...)) -> None:
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    typer.echo(json.dumps(run(load_params(params), since), indent=2))


if __name__ == "__main__":
    app()
