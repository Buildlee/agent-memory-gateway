"""认证入口的进程内滑动窗口限流。"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable


class SlidingWindowRateLimiter:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        if not key or limit < 1 or window_seconds < 1:
            raise ValueError("限流参数无效")
        now = self._clock()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True
