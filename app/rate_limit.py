from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status


class SlidingWindowLimiter:
    """Small single-process guard for local/single-worker deployments.

    Production deployments should replace this with a shared Redis-backed
    limiter at the ingress/API gateway.
    """

    def __init__(self, *, requests: int, window_seconds: int) -> None:
        self.requests = requests
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.requests:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "code": "rate_limit_exceeded",
                        "message": "Too many requests. Please try again shortly.",
                    },
                    headers={"Retry-After": str(self.window_seconds)},
                )
            bucket.append(now)


qr_limiter = SlidingWindowLimiter(requests=30, window_seconds=60)
google_auth_limiter = SlidingWindowLimiter(requests=20, window_seconds=60)
push_registration_limiter = SlidingWindowLimiter(requests=30, window_seconds=60)


def enforce_qr_rate_limit(request: Request) -> None:
    # Uvicorn may normalize request.client from proxy headers, but only when the
    # connection comes from a configured trusted proxy. Reading X-Forwarded-For
    # here directly would let an internet client choose a fresh limiter key.
    client = request.client.host if request.client else "unknown"
    qr_limiter.check(client)


def enforce_google_auth_rate_limit(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    google_auth_limiter.check(client)
