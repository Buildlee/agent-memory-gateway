"""认证入口的进程内滑动窗口限流。"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class SlidingWindowRateLimiter:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 4096,
    ) -> None:
        if max_keys < 1:
            raise ValueError("max_keys 必须大于 0")
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[str, deque[float]] = {}
        self._expires_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        if not key or limit < 1 or window_seconds < 1:
            raise ValueError("限流参数无效")
        now = self._clock()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events.get(key)
            if events is None:
                if len(self._events) >= self._max_keys:
                    self._discard_expired(now)
                if len(self._events) >= self._max_keys:
                    return False
                events = deque()
                self._events[key] = events
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            self._expires_at[key] = max(self._expires_at.get(key, now), now + window_seconds)
            return True

    def _discard_expired(self, now: float) -> None:
        for key in tuple(self._events):
            if self._expires_at.get(key, now) <= now:
                self._events.pop(key, None)
                self._expires_at.pop(key, None)
