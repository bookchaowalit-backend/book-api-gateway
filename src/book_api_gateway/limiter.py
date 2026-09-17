"""Small fixed-window limiter used by the local gateway pilot."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable


class FixedWindowRateLimiter:
    """Limit each key to ``limit`` requests in a monotonic time window."""

    def __init__(self, limit: int, *, window_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic):
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("limit and window_seconds must be greater than zero")
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)`` for one request."""

        now = self.clock()
        with self._lock:
            window_start, count = self._windows.get(key, (now, 0))
            if now - window_start >= self.window_seconds:
                window_start, count = now, 0
            if count >= self.limit:
                retry_after = max(1, math.ceil(self.window_seconds - (now - window_start)))
                self._windows[key] = (window_start, count)
                return False, retry_after
            self._windows[key] = (window_start, count + 1)
            return True, 0
