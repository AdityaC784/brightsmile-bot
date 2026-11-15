"""Per-turn persistence: conversations, messages (with metrics), review candidates.

Contact details are redacted from logged message text (plan §15: "don't log contact details").
Leads with consent are stored separately in `leads`.
"""

from __future__ import annotations

import re

import psycopg
from psycopg.types.json import Jsonb

from bot.config import get_settings, load_yaml

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Indian/intl phone numbers: 10+ digits, optional +country code, spaces or dashes inside.
_PHONE = re.compile(r"(?<![\w-])\+?\d[\d\s-]{8,}\d(?![\w-])")


def redact(text: str) -> str:
    """Mask emails and phone numbers. The clinic's own published numbers are kept."""
    keep = {_digits(v) for v in _clinic_numbers()}

    def phone(m: re.Match) -> str:
        return m.group(0) if _digits(m.group(0)) in keep else "[phone]"

    return _PHONE.sub(phone, _EMAIL.sub("[email]", text))


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _clinic_numbers() -> list[str]:
    """The clinic's published numbers (extracted from config text like
    '+91-20-5550-0140 (phone & WhatsApp, Mon-Sat 09:30 AM - ...)')."""
    c = load_yaml("generation")["clinic"]
    return [n for field in ("reception", "after_hours_triage") for n in _PHONE.findall(c[field])]


def _conn(database_url: str | None = None):
    return psycopg.connect(database_url or get_settings().database_url)


def log_turn(thread_id: str, user_text: str, state: dict, handoff_pending: bool) -> int:
    """Store the user message and the assistant reply; returns the assistant message id."""
    reply = state.get("reply") or {}
    behavior = reply.get("behavior", "")
    status = "pending" if handoff_pending else None
    with _conn() as conn:
        conn.execute(
            """INSERT INTO conversations (thread_id, tenant_id, handoff_status)
               VALUES (%s, %s, COALESCE(%s, 'none'))
               ON CONFLICT (thread_id) DO UPDATE SET updated_at = now(),
                   handoff_status = COALESCE(%s, conversations.handoff_status)""",
            (thread_id, get_settings().tenant_id, status, status),
        )
        conn.execute(
            "INSERT INTO messages (thread_id, role, content, intent) VALUES (%s, 'user', %s, %s)",
            (thread_id, redact(user_text), state.get("intent")),
        )
        row = conn.execute(
            """INSERT INTO messages (thread_id, role, content, intent, retrieved_ids, citations,
                   verifier, abstained, latency_ms, tokens_in, tokens_out, model)
               VALUES (%s, 'assistant', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                thread_id,
                redact(reply.get("answer", "")),
                state.get("intent"),
                Jsonb(reply.get("retrieved_ids", [])),
                Jsonb(reply.get("citations", [])),
                Jsonb(
                    {
                        **(reply.get("verifier") or {}),
                        "fallback_reason": reply.get("fallback_reason"),
                        "repaired": reply.get("repaired", False),
                    }
                ),
                behavior in ("abstain", "refuse"),
                reply.get("latency_ms"),
                reply.get("tokens_in"),
                reply.get("tokens_out"),
                load_yaml("models")["roles"]["generator"]["model"]
                if reply.get("retrieved_ids")
                else None,
            ),
        ).fetchone()
        message_id = row[0]
        # Automatic review candidates (plan §9): abstained, handoff, failed citation check.
        source = None
        if reply.get("fallback_reason"):
            source = "citation_failed"
        elif behavior == "abstain":
            source = "abstained"
        elif handoff_pending:
            source = "handoff"
        if source:
            add_candidate(
                conn,
                source,
                thread_id,
                message_id,
                user_text,
                reply.get("answer"),
                reply.get("retrieved_ids", []),
            )
    return message_id


def add_candidate(conn, source, thread_id, message_id, question, bot_answer, retrieved_ids):
    conn.execute(
        """INSERT INTO candidates (source, thread_id, message_id, question, bot_answer,
                                   retrieved_ids)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (
            source,
            thread_id,
            message_id,
            redact(question),
            redact(bot_answer or ""),
            Jsonb(retrieved_ids),
        ),
    )


def log_human_reply(thread_id: str, text: str) -> int:
    with _conn() as conn:
        row = conn.execute(
            "INSERT INTO messages (thread_id, role, content) VALUES (%s, 'human_agent', %s) "
            "RETURNING id",
            (thread_id, redact(text)),
        ).fetchone()
        conn.execute(
            "UPDATE conversations SET handoff_status = 'resolved', updated_at = now() "
            "WHERE thread_id = %s",
            (thread_id,),
        )
    return row[0]


def save_feedback(thread_id: str, message_id: int | None, rating: int, comment: str | None) -> int:
    with _conn() as conn:
        row = conn.execute(
            "INSERT INTO feedback (thread_id, message_id, rating, comment) VALUES (%s, %s, %s, %s) "
            "RETURNING id",
            (thread_id, message_id, rating, redact(comment) if comment else None),
        ).fetchone()
        if rating < 0 and message_id is not None:
            msg = conn.execute(
                """SELECT a.content, a.retrieved_ids,
                          (SELECT u.content FROM messages u WHERE u.thread_id = a.thread_id
                             AND u.role = 'user' AND u.id < a.id ORDER BY u.id DESC LIMIT 1)
                   FROM messages a WHERE a.id = %s AND a.thread_id = %s""",
                (message_id, thread_id),
            ).fetchone()
            if msg:
                add_candidate(
                    conn, "thumbs_down", thread_id, message_id, msg[2] or "", msg[0], msg[1]
                )
    return row[0]


def pending_handoffs(recent: int = 6) -> list[dict]:
    """Conversations waiting for staff, with the last few messages for context."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT c.thread_id, c.updated_at,
                      (SELECT content FROM messages m WHERE m.thread_id = c.thread_id
                         AND m.role = 'user' ORDER BY id DESC LIMIT 1),
                      (SELECT coalesce(json_agg(json_build_object('role', role, 'content', content)
                                                ORDER BY id), '[]'::json)
                         FROM (SELECT id, role, content FROM messages m
                                WHERE m.thread_id = c.thread_id ORDER BY id DESC LIMIT %s) t)
               FROM conversations c WHERE c.handoff_status = 'pending' ORDER BY c.updated_at""",
            (recent,),
        ).fetchall()
    return [
        {
            "thread_id": r[0],
            "updated_at": r[1].isoformat(),
            "last_user_message": r[2],
            "recent": r[3],
        }
        for r in rows
    ]


def staff_replies(thread_id: str, after: int = 0) -> dict:
    """Staff messages in one conversation newer than message id `after`, and whether it still
    waits for staff. Contacts in the text are redacted like every stored message."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, content, created_at FROM messages "
            "WHERE thread_id = %s AND role = 'human_agent' AND id > %s ORDER BY id",
            (thread_id, after),
        ).fetchall()
        status = conn.execute(
            "SELECT handoff_status FROM conversations WHERE thread_id = %s", (thread_id,)
        ).fetchone()
    return {
        "pending": bool(status and status[0] == "pending"),
        "replies": [{"id": r[0], "text": r[1], "created_at": r[2].isoformat()} for r in rows],
    }
