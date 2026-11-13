"""Minimal forward-only SQL migration runner.

Applies src/bot/db/migrations/NNN_*.sql in order, each in its own transaction,
and records applied files in schema_migrations.

    python -m bot.db.migrate
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from bot.config import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def migrate(database_url: str | None = None) -> list[str]:
    url = database_url or get_settings().database_url
    applied_now: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   filename   TEXT PRIMARY KEY,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
        )
        done = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            applied_now.append(path.name)
    return applied_now


if __name__ == "__main__":
    applied = migrate()
    print(f"Applied {len(applied)} migration(s): {applied}" if applied else "Schema up to date.")
