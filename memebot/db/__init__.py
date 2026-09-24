from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from memebot.config import ROOT, settings

MIGRATIONS = ROOT / "db" / "migrations"


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL not set (.env)")
    # Bounded waits: a blocked statement must fail fast and be retried by the caller,
    # never sit for Supabase's 2-minute statement_timeout inside a 10-minute job.
    with psycopg.connect(
        settings.database_url,
        row_factory=dict_row,
        options="-c lock_timeout=20000 -c statement_timeout=90000",
    ) as c:
        yield c


def migrate() -> list[str]:
    """Apply db/migrations/*.sql in order. Idempotent (all statements use IF NOT EXISTS)."""
    applied: list[str] = []
    with conn() as c:
        for path in sorted(Path(MIGRATIONS).glob("*.sql")):
            c.execute(path.read_text(encoding="utf-8"))
            applied.append(path.name)
        c.commit()
    return applied
