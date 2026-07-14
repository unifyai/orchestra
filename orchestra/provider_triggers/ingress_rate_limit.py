"""In-process rate limiting for public provider-trigger ingress."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class IngressRateLimiter:
    """Sliding-window limiter keyed by backend and ingress key."""

    def __init__(self, *, limit_per_minute: int) -> None:
        self._limit = max(1, int(limit_per_minute))
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, *, backend_id: str, ingress_key: str) -> bool:
        """Return True when the request is within the configured window."""

        key = f"{backend_id}:{ingress_key}"
        now = time.monotonic()
        window_start = now - 60.0
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if len(bucket) >= self._limit:
                return False
            bucket.append(now)
            if len(self._events) > 10_000:
                stale_keys = [
                    seen_key
                    for seen_key, seen_bucket in list(self._events.items())
                    if not seen_bucket
                ]
                for seen_key in stale_keys:
                    self._events.pop(seen_key, None)
            return True


_DEFAULT_LIMITER: IngressRateLimiter | None = None
_DEFAULT_LIMITER_LIMIT: int | None = None
_LIMITER_LOCK = threading.Lock()


def get_ingress_rate_limiter(*, limit_per_minute: int) -> IngressRateLimiter:
    """Return a process-wide limiter for the configured rate."""

    global _DEFAULT_LIMITER, _DEFAULT_LIMITER_LIMIT
    with _LIMITER_LOCK:
        if _DEFAULT_LIMITER is None or _DEFAULT_LIMITER_LIMIT != int(limit_per_minute):
            _DEFAULT_LIMITER = IngressRateLimiter(limit_per_minute=limit_per_minute)
            _DEFAULT_LIMITER_LIMIT = int(limit_per_minute)
        return _DEFAULT_LIMITER


def reset_ingress_rate_limiter_for_tests() -> None:
    """Clear the process-wide limiter between tests."""

    global _DEFAULT_LIMITER, _DEFAULT_LIMITER_LIMIT
    with _LIMITER_LOCK:
        _DEFAULT_LIMITER = None
        _DEFAULT_LIMITER_LIMIT = None
