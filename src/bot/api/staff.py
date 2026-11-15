"""Staff conversations: replies, ending, timeouts and presence.

A handoff puts the thread in staff mode (`handoff_pending` in the graph state, `pending` in
`conversations`). These helpers change that state from outside the graph with `update_state`,
recorded as the `staff` node, and mirror each change into the messages table so the chat page
can poll for it.

Presence is kept in memory (one API process on the free host): each open staff page (its own
session id) refreshing the handoff list marks staff online, and tells the server when it is
locked or closed; the chat page polling for staff messages marks the patient online (or away,
when their tab is hidden). After a restart it rebuilds within seconds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from bot.api import turns
from bot.config import load_yaml
from bot.graph.state import Turn

# The pages poll every 5 s (the chat page every 15 s while hidden), but browsers throttle timers
# in background tabs to as little as once a minute, so hidden pages get a longer allowance.
STAFF_ONLINE_SECONDS = 90  # staff often keep the staff page in a background tab
PATIENT_LEFT_SECONDS = 20  # last poll came from a visible tab
PATIENT_LEFT_HIDDEN_SECONDS = 90  # last poll came from a hidden tab
STAFF_LEAVE_GRACE_SECONDS = 10  # after a staff page closes; a reload pings again in time


@dataclass
class Presence:
    staff: dict[str, float] = field(default_factory=dict)  # staff page session -> last seen
    # tid -> (last poll from a visible tab, last poll from a hidden tab). Kept separately so a
    # duplicated tab in the background doesn't make an active patient look away.
    patients: dict[str, tuple[float, float]] = field(default_factory=dict)

    def staff_ping(self, session: str = "default", now: float | None = None) -> None:
        self.staff[session] = now or time.time()

    def staff_leave(self, session: str = "default", now: float | None = None) -> None:
        """A staff page was locked or closed: offline after a short grace period."""
        if session in self.staff:
            cutoff = (now or time.time()) - STAFF_ONLINE_SECONDS + STAFF_LEAVE_GRACE_SECONDS
            self.staff[session] = min(self.staff[session], cutoff)

    def staff_online(self, now: float | None = None) -> bool:
        now = now or time.time()
        return any(now - seen < STAFF_ONLINE_SECONDS for seen in self.staff.values())

    def staff_gone(self, now: float | None = None) -> bool:
        """Staff were here and have all left. False before any staff page was seen (e.g.
        right after a restart), so open chats aren't closed before staff pages check in."""
        return bool(self.staff) and not self.staff_online(now)

    def patient_ping(self, thread_id: str, hidden: bool = False, now: float | None = None) -> None:
        now = now or time.time()
        visible, hidden_seen = self.patients.get(thread_id, (0.0, 0.0))
        self.patients[thread_id] = (visible, now) if hidden else (now, hidden_seen)

    def patient_seen(self, thread_id: str, now: float | None = None) -> float:
        """Last poll from any of this patient's tabs. Unknown (e.g. after a restart) counts as
        now, so a chat is only closed after a full absence period we actually observed."""
        now = now or time.time()
        return max(self.patients.setdefault(thread_id, (now, 0.0)))

    def patient_status(self, thread_id: str, now: float | None = None) -> tuple[str, int]:
        """('online' | 'away' | 'left', seconds since the last poll): as present as the most
        active tab."""
        now = now or time.time()
        seen = self.patient_seen(thread_id, now)
        visible, hidden = self.patients[thread_id]
        if now - visible < PATIENT_LEFT_SECONDS:
            return "online", int(now - visible)
        if now - hidden < PATIENT_LEFT_HIDDEN_SECONDS:
            return "away", int(now - hidden)
        return "left", int(now - seen)


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def in_staff_mode(graph, thread_id: str) -> bool:
    return bool(graph.get_state(_cfg(thread_id)).values.get("handoff_pending"))


def reply(graph, thread_id: str, text: str, *, persist: bool = True) -> None:
    """Add a staff message; the patient is no longer waiting."""
    graph.update_state(
        _cfg(thread_id),
        {"messages": [Turn(role="human_agent", content=text)], "staff_waiting_since": None},
        as_node="staff",
    )
    if persist:
        turns.log_human_reply(thread_id, text)


def end(
    graph,
    thread_id: str,
    reason: str = "ended",
    *,
    persist: bool = True,
    presence: Presence | None = None,
) -> str:
    """Leave staff mode and tell the patient the assistant is back.
    reason: ended (by staff) | timeout (no staff reply) | left (patient gone) |
    closed (all staff went offline)."""
    cfg = load_yaml("generation")
    text = cfg["handoff"][f"{reason}_text"].strip().format(**cfg["clinic"])
    if in_staff_mode(graph, thread_id):
        graph.update_state(
            _cfg(thread_id),
            {
                "messages": [Turn(role="assistant", content=text)],
                "handoff_pending": False,
                "staff_waiting_since": None,
            },
            as_node="staff",
        )
    # Always take it off the staff list, even if the conversation state is missing.
    if persist:
        turns.log_staff_end(thread_id, text, reason)
    if presence:
        presence.patients.pop(thread_id, None)
    return text


def expire(
    graph,
    thread_id: str,
    now: float | None = None,
    *,
    persist: bool = True,
    presence: Presence | None = None,
) -> bool:
    """End staff mode if the patient waited too long for staff, has been gone too long, or
    all staff went offline.
    Checked whenever the patient writes or polls and whenever staff refresh their list, so no
    background job is needed."""
    values = graph.get_state(_cfg(thread_id)).values
    if not values.get("handoff_pending"):
        return False
    now = now or time.time()
    limits = load_yaml("generation")["handoff"]
    since = values.get("staff_waiting_since")
    if since and now - since >= limits["staff_timeout_seconds"]:
        end(graph, thread_id, "timeout", persist=persist, presence=presence)
        return True
    if presence and now - presence.patient_seen(thread_id, now) >= limits["patient_gone_seconds"]:
        end(graph, thread_id, "left", persist=persist, presence=presence)
        return True
    if presence and presence.staff_gone(now):
        end(graph, thread_id, "closed", persist=persist, presence=presence)
        return True
    return False
