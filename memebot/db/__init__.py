from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from memebot.config import ROOT, settings

MIGRATIONS = ROOT / "db" / "migrations"


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL not set (.env)")
    with psycopg.connect(settings.database_url, row_factory=dict_row) as c:
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
