"""NSE's own site APIs, fetched on request with a short cache.

They answer a browser user agent with no cookie; a bare client gets its connection held open until it
times out.
"""
import gzip
import json
import threading
import time
import urllib.request

from ..config import USER_AGENT

BASE = "https://www.nseindia.com/api/"
CACHE_SECONDS = 30
# Keys include caller-supplied symbols, so the cache is bounded.
MAX_ENTRIES = 256
# path -> (stored at, ttl, data), in insertion order.
_cache: dict[str, tuple[float, float, dict]] = {}
_lock = threading.Lock()


def get(path: str, ttl: float = CACHE_SECONDS) -> dict:
    """Fetch one NSE API path as parsed JSON, cached for `ttl` seconds."""
    with _lock:
        hit = _cache.get(path)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[2]
    req = urllib.request.Request(BASE + path, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                       "Accept-Encoding": "gzip"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=15) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    data = json.loads(body)
    now = time.monotonic()
    with _lock:
        for key in [k for k, (at, life, _) in _cache.items() if now - at >= life]:
            del _cache[key]
        if path not in _cache and len(_cache) >= MAX_ENTRIES:
            del _cache[next(iter(_cache))]
        _cache[path] = (now, ttl, data)
    return data


def num(v) -> float | None:
    """NSE writes a missing value as 0 or "-"; neither is a price."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f or None
