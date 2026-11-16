"""In-memory rate limits for chat messages, checked before any LLM call.

Every chat turn costs several thousand LLM tokens; without a cap, one visitor (or a script) can
use up the free daily quota and every patient then gets the error message. Limits are per chat
(per minute and per day) and per visitor IP (per day, so opening new chats doesn't get around
them). Counters live in this process: enough for one server instance; a restart resets them.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

MINUTE, DAY = 60.0, 86_400.0


class RateLimiter:
    def __init__(self, per_chat_per_minute: int, per_chat_per_day: int, per_ip_per_day: int):
        self.rules = {
            "chat_minute": (per_chat_per_minute, MINUTE),
            "chat_day": (per_chat_per_day, DAY),
            "ip_day": (per_ip_per_day, DAY),
        }
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, thread_id: str, ip: str, now: float | None = None) -> str | None:
        """Record one message; return the name of the limit it exceeds (not recorded), or None."""
        now = time.time() if now is None else now
        keys = {
            "chat_minute": ("chat", thread_id),
            "chat_day": ("chat", thread_id),
            "ip_day": ("ip", ip),
        }
        with self._lock:
            for rule, (limit, window) in self.rules.items():
                hits = self._hits[keys[rule]]
                while hits and hits[0] <= now - DAY:  # keep a day of history, drop the rest
                    hits.popleft()
                if sum(1 for t in hits if t > now - window) >= limit:
                    return rule
            self._hits[("chat", thread_id)].append(now)
            self._hits[("ip", ip)].append(now)
            if len(self._hits) > 10_000:  # forget chats and visitors idle for a day
                for k in [k for k, h in self._hits.items() if not h or h[-1] <= now - DAY]:
                    del self._hits[k]
            return None


def client_ip(headers, fallback: str | None) -> str:
    """The visitor's IP: the first X-Forwarded-For entry (set by the hosting proxy), else the
    socket address. A client can fake this header, so this stops heavy accidental use and simple
    scripts, not a determined attacker."""
    forwarded = headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or fallback or "unknown"
