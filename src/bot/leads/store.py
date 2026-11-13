"""Lead persistence. Personal data is saved ONLY with recorded consent; one lead per thread.

The leads table (migration 001) has `thread_id UNIQUE`, so saving is idempotent: a graph node
that re-runs (e.g. after a resume) updates the same row instead of creating duplicates.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg

from bot.config import get_settings


class ConsentRequired(ValueError):
    """Raised if anything tries to save a lead without explicit consent."""


def save_lead(
    thread_id: str,
    *,
    name: str,
    contact: str,
    consent_text: str,
    need: str | None = None,
    budget: str | None = None,
    timeline: str | None = None,
    consented: bool,
    database_url: str | None = None,
) -> str:
    """Insert or update the thread's lead; returns its id. Refuses without consent."""
    if not consented:
        raise ConsentRequired("lead not saved: patient has not consented")
    if not name.strip() or not contact.strip():
        raise ValueError("name and contact are required")
    with psycopg.connect(database_url or get_settings().database_url) as conn:
        row = conn.execute(
            """INSERT INTO leads (thread_id, need, budget, timeline, name, contact,
                                  consent_at, consent_text)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (thread_id) DO UPDATE SET
                   need = EXCLUDED.need, budget = EXCLUDED.budget, timeline = EXCLUDED.timeline,
                   name = EXCLUDED.name, contact = EXCLUDED.contact,
                   consent_at = EXCLUDED.consent_at, consent_text = EXCLUDED.consent_text
               RETURNING id""",
            (
                thread_id,
                need,
                budget,
                timeline,
                name.strip(),
                contact.strip(),
                datetime.now(UTC),
                consent_text,
            ),
        ).fetchone()
    return str(row[0])


def delete_lead(lead_id: str, database_url: str | None = None) -> bool:
    """Hard delete (privacy request). Returns True if a row was removed."""
    with psycopg.connect(database_url or get_settings().database_url) as conn:
        cur = conn.execute("DELETE FROM leads WHERE id = %s", (lead_id,))
        return cur.rowcount > 0
