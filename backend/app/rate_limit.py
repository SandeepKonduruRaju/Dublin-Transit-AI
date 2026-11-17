"""In-memory sliding-window rate limiter for /chat (Milestone 6.5).

Three limits: per IP per minute, per IP per day, and a GLOBAL daily cap that bounds LLM spend no matter
how many IPs show up. In-memory = one process only; several replicas would need Redis (Later).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

MINUTE = 60.0
DAY = 86_400.0


class RateLimiter:
    def __init__(self, per_ip_minute: int, per_ip_day: int, global_day: int, clock: Callable[[], float] = time.monotonic):
        self.per_ip_minute = per_ip_minute
        self.per_ip_day = per_ip_day
        self.global_day = global_day
        self.clock = clock
        self._ip: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._lock = threading.Lock()

    @staticmethod
    def _prune(q: deque[float], now: float, window: float) -> None:
        while q and q[0] <= now - window:
            q.popleft()

    def check(self, key: str) -> float | None:
        """Record a request. Returns None if allowed, else seconds to wait (for Retry-After)."""
        with self._lock:
            now = self.clock()
            self._prune(self._global, now, DAY)
            hits = self._ip.setdefault(key, deque())
            self._prune(hits, now, DAY)

            if len(self._global) >= self.global_day:
                return self._global[0] + DAY - now
            if len(hits) >= self.per_ip_day:
                return hits[0] + DAY - now
            recent = [t for t in hits if t > now - MINUTE]
            if len(recent) >= self.per_ip_minute:
                return recent[0] + MINUTE - now

            hits.append(now)
            self._global.append(now)
            if len(self._ip) > 10_000:  # bound memory: forget idle IPs
                for k in [k for k, q in self._ip.items() if not q]:
                    del self._ip[k]
            return None
