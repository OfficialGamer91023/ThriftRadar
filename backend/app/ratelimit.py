"""In-memory sliding-window rate limits (demo only). Spec: DESIGN.md §4.3 `SlidingWindowLimiter`.
A single process makes this exact; a restart resets the counters (the money cap is DB-backed)."""

import math
import threading
import time
from collections import OrderedDict, deque

from fastapi.responses import JSONResponse

from app.security import client_ip

# (limit, window seconds) per bucket, DESIGN §4.3
LIMITS = {
    "login": (10, 60),
    "search": (60, 60),
    "image_search": (20, 3600),
    "upload_ip": (5, 3600),
    "upload_session": (10, 86400),
    "upload_global": (300, 86400),
}


class SlidingWindowLimiter:
    def __init__(self, max_keys: int = 20_000, clock=time.monotonic):
        self._hits: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self._lock = threading.Lock()
        self.max_keys = max_keys
        self.clock = clock

    def _window(self, k: tuple[str, str], cutoff: float) -> deque[float]:
        q = self._hits.get(k)
        if q is None:
            q = self._hits[k] = deque()
            if len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)  # least recently used
        else:
            self._hits.move_to_end(k)
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def check(self, bucket: str, key: str, limit: int, window_s: float) -> tuple[bool, int]:
        """Would one more hit be allowed? -> (allowed, retry_after seconds). Records nothing."""
        now = self.clock()
        with self._lock:
            q = self._window((bucket, key), now - window_s)
            if len(q) < limit:
                return True, 0
            return False, max(1, math.ceil(q[0] + window_s - now))

    def hit(self, bucket: str, key: str, limit: int, window_s: float) -> tuple[bool, int]:
        """Record a hit if allowed -> (allowed, retry_after seconds)."""
        now = self.clock()
        with self._lock:
            q = self._window((bucket, key), now - window_s)
            if len(q) >= limit:
                return False, max(1, math.ceil(q[0] + window_s - now))
            q.append(now)
            return True, 0

    def seed(self, bucket: str, key: str, ages_s: list[float]) -> None:
        """Pre-load hits that happened `ages_s` seconds ago (the global upload count at boot)."""
        now = self.clock()
        with self._lock:
            q = self._hits.setdefault((bucket, key), deque())
            q.extend(sorted(now - a for a in ages_s))


def limit(limiter: SlidingWindowLimiter, bucket: str, key: str) -> tuple[bool, int]:
    n, window = LIMITS[bucket]
    return limiter.hit(bucket, key, n, window)


def guard(request, bucket: str, key: str):
    """Demo only: a 429 response with Retry-After if over the limit, else None (and the hit is recorded)."""
    if not request.app.state.settings.demo_mode:
        return None
    ok, retry = limit(request.app.state.limiter, bucket, key)
    return None if ok else JSONResponse({"detail": "rate_limited"}, 429, headers={"Retry-After": str(retry)})


def ip(request) -> str:
    return client_ip(request, request.app.state.settings.trust_proxy_hops)
