"""Small fixed-window limiter used by the local gateway pilot."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable


class FixedWindowRateLimiter:
    """Limit each key to ``limit`` requests in a monotonic time window.

    Memory is bounded: when more than ``max_keys`` windows are tracked, expired
    windows are dropped first and, if that is not enough, the oldest windows
    are evicted. Evicting an active window only resets that key's count, so
    the bound can never block a request that would otherwise be allowed.
    """

    def __init__(
        self,
        limit: int,
        *,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ):
        if limit <= 0 or window_seconds <= 0 or max_keys <= 0:
            raise ValueError("limit, window_seconds and max_keys must be greater than zero")
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self.max_keys = max_keys
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._windows)

    def _prune(self, now: float) -> None:
        """Drop expired windows, then the oldest ones, down to ``max_keys``."""

        self._windows = {
            key: value for key, value in self._windows.items() if now - value[0] < self.window_seconds
        }
        overflow = len(self._windows) - self.max_keys
        if overflow > 0:
            oldest = sorted(self._windows, key=lambda key: self._windows[key][0])[:overflow]
            for key in oldest:
                del self._windows[key]

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
            if len(self._windows) > self.max_keys:
                self._prune(now)
            return True, 0
