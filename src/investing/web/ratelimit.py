"""Per-client request limits held in process memory. Assumes a single worker."""
import math
import threading
import time
from collections.abc import Callable

from fastapi import Request

from .. import config
from . import errors


class RateLimiter:
    """Fixed-window request counter per key, with bounded memory."""

    def __init__(self, limit: tuple[int, int], *, max_keys: int = 10_000) -> None:
        self._count, self._window = limit
        self._max_keys = max_keys
        self._hits: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int | None:
        """Count one request for ``key``. Return the seconds to wait when the key is over the limit, else None."""
        if self._count <= 0:
            return None
        with self._lock:
            now = time.monotonic()
            entry = self._hits.get(key)
            if entry is not None and now < entry[0] + self._window:
                start, count = entry
                self._hits[key] = (start, count + 1)
                if count + 1 > self._count:
                    return max(1, math.ceil(start + self._window - now))
                return None
            # Re-inserting keeps dict order equal to window start order.
            self._hits.pop(key, None)
            if len(self._hits) >= self._max_keys:
                self._hits = {k: v for k, v in self._hits.items() if now < v[0] + self._window}
                if len(self._hits) >= self._max_keys:
                    del self._hits[next(iter(self._hits))]
            self._hits[key] = (now, 1)
            return None


NSE = RateLimiter(config.RATE_LIMIT_NSE)
ALLOTMENT = RateLimiter(config.RATE_LIMIT_ALLOTMENT)
ANALYZE = RateLimiter(config.RATE_LIMIT_ANALYZE)


def retry_after(limiter: RateLimiter, request: Request) -> int | None:
    """Count ``request`` against ``limiter`` by client address; return the seconds to wait when over the limit."""
    # Prefer the proxy's client IP header. Over a unix socket without it, all requests share one bucket.
    header = request.headers.get(config.CLIENT_IP_HEADER) if config.CLIENT_IP_HEADER else None
    return limiter.retry_after(header or (request.client.host if request.client is not None else "unknown"))


def limit(limiter: RateLimiter) -> Callable[[Request], None]:
    """A FastAPI dependency that answers 429 when the client is over ``limiter``."""
    def check(request: Request) -> None:
        seconds = retry_after(limiter, request)
        if seconds is not None:
            raise errors.rate_limited(seconds)
    return check
