"""Spread a fetcher's requests across this machine's public addresses, paced per address.

Kuvera, Yahoo and NSE rate limit per public IP. An exit is one local address bound on the socket;
INVESTING_ADDRESSES lists them. With none set this is a single paced fetcher.

    from investing.core.egress import rotation

    with rotation() as pool:                       # one exit per configured address
        payload = pool.get_json(url)               # next exit in turn, paced per exit, 429 moves on
"""
import contextlib
import http.client
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .. import config

logger = logging.getLogger(__name__)


class NotFound(urllib.error.HTTPError, RuntimeError):
    """A 404 or 410, raised without retrying.

    Also a RuntimeError, since callers wrap whole fetches in `except RuntimeError`.
    """


CHECK_URL = "https://api.ipify.org"  # returns the caller's public IP as plain text
DEFAULT_UA = config.USER_AGENT


def local_exit() -> Exit:
    """This machine as a single unbound exit."""
    exit_ = Exit("local")
    exit_.build()
    return exit_


def bound_opener(address: str) -> urllib.request.OpenerDirector:
    """An opener whose sockets bind to one local address.

    urllib only takes a source_address through the connection classes, hence the subclasses.
    """
    class _HTTPConnection(http.client.HTTPConnection):
        def __init__(self, *a, **kw):
            kw["source_address"] = (address, 0)
            super().__init__(*a, **kw)

    class _HTTPSConnection(http.client.HTTPSConnection):
        def __init__(self, *a, **kw):
            kw["source_address"] = (address, 0)
            super().__init__(*a, **kw)

    class _HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(_HTTPConnection, req)

    class _HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_HTTPSConnection, req)

    # ProxyHandler({}) ignores HTTP_PROXY/HTTPS_PROXY: a proxy would put every exit behind one address.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler, _HTTPSHandler)


@dataclass
class Exit:
    """One way out: a local address bound on the socket, or the kernel's choice when address is None."""
    name: str
    address: str | None = None
    opener: urllib.request.OpenerDirector = field(default_factory=urllib.request.build_opener)
    last: float = 0.0
    cooldown: float = 0.0  # monotonic time this exit may be used again, set by a 429
    refusals: int = 0  # consecutive 429s, across calls
    lock: threading.Lock = field(default_factory=threading.Lock)
    failures: int = 0

    def build(self) -> None:
        self.opener = bound_opener(self.address) if self.address else urllib.request.build_opener()


class Pool:
    """Round-robin over the exits, with the minimum gap enforced per exit."""

    def __init__(self, exits: list[Exit], min_gap: float, user_agent: str) -> None:
        self.exits, self.min_gap, self.user_agent = exits, min_gap, user_agent
        self._next, self._turn = 0, threading.Lock()
        self.requests, self.rate_limited = 0, 0

    @property
    def names(self) -> list[str]:
        return [e.name for e in self.exits]

    def _take(self) -> Exit:
        """The next usable exit after the last one taken.

        The cursor indexes all exits, not the usable ones, so exits entering and leaving cooldown do
        not shift it and starve a healthy exit.
        """
        with self._turn:
            now = time.monotonic()

            def usable(e: Exit) -> bool:
                return e.failures < 3 and e.cooldown <= now

            n = len(self.exits)
            for step in range(1, n + 1):
                candidate = self.exits[(self._next + step) % n]
                if usable(candidate):
                    self._next = (self._next + step) % n
                    return candidate
            # everything is cooling or retired: take the one that frees up soonest, and wait for it
            self._next = min(range(n), key=lambda i: (self.exits[i].failures >= 3, self.exits[i].cooldown))
            return self.exits[self._next]

    def open(self, url: str, timeout: int = 60, tries: int = 5, accept: str = "*/*",
             headers: dict[str, str] | None = None) -> bytes:
        """One request, retried across exits. A 429 cools that exit down; a 404 or 410 raises
        NotFound; anything else is retried, since a 403 can differ per exit."""
        last: BaseException | None = None
        for attempt in range(tries):
            exit_ = self._take()
            backoff = 0.0  # slept outside the lock
            with exit_.lock:
                # Inside the lock: another thread may have set a cooldown while this one waited.
                wait = max(exit_.cooldown - time.monotonic(), self.min_gap - (time.monotonic() - exit_.last))
                if wait > 0:
                    time.sleep(wait)
                try:
                    request = urllib.request.Request(
                        url, headers={"User-Agent": self.user_agent, "Accept": accept, **(headers or {})})
                    with exit_.opener.open(request, timeout=timeout) as response:
                        body = response.read()
                    exit_.failures, exit_.refusals, self.requests = 0, 0, self.requests + 1
                    return body
                except urllib.error.HTTPError as exc:
                    last = exc
                    if exc.code == 429:
                        self.rate_limited += 1
                        exit_.refusals += 1
                        after = str((exc.headers or {}).get("Retry-After") or "")
                        # Grows with the exit's refusals, not `attempt`: get_json calls with tries=1.
                        penalty = (min(int(after), 120) if after.isdigit()
                                   else min(120, 5 * 2 ** min(exit_.refusals - 1, 5)))
                        exit_.cooldown = max(exit_.cooldown, time.monotonic() + penalty)
                        continue
                    if exc.code in (404, 410):
                        raise NotFound(exc.url, exc.code, exc.reason, exc.headers, exc.fp) from exc
                    backoff = 1.0 * (attempt + 1)
                except Exception as exc:
                    last, exit_.failures = exc, exit_.failures + 1
                    backoff = 1.0 * (attempt + 1)
                finally:
                    # Every attempt counts against the gap, successes included.
                    exit_.last = time.monotonic()
            if backoff:
                time.sleep(backoff)
        raise RuntimeError(f"{url}: {last!r}")

    def get_json(self, url: str, tries: int = 5, **kw):
        """As open(), but a body that does not decode (a throttle page, a truncated body) is retried."""
        accept = kw.pop("accept", "application/json")
        last: Exception | None = None
        for attempt in range(tries):
            try:
                return json.loads(self.open(url, tries=1, accept=accept, **kw).decode("utf-8"))
            except NotFound:
                raise
            except (ValueError, UnicodeDecodeError) as exc:
                last = exc
            except RuntimeError as exc:
                last = exc
            time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"{url}: no usable JSON after {tries} attempts: {last!r}")


def _bindable(address: str) -> bool:
    """Whether a socket can bind this address."""
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.bind((address, 0))
                return True
        except OSError:
            continue
    return False


@contextlib.contextmanager
def rotation(min_gap: float = 0.35, user_agent: str = DEFAULT_UA, quiet: bool = False):
    """One exit per address in INVESTING_ADDRESSES, or a single unbound exit when none are set."""
    exits: list[Exit] = []
    for address in config.addresses():
        if not _bindable(address):
            if not quiet:
                logger.warning("egress: cannot bind %s, continuing without it", address)
            continue
        exits.append(Exit(f"local:{address}", address=address))
    # No bindable address: fall back to one unbound exit.
    if not exits:
        exits = [Exit("local")]
    for exit_ in exits:
        exit_.build()
    pool = Pool(exits, min_gap, user_agent)
    if not quiet:
        logger.info("egress: %d exit(s): %s", len(exits), ", ".join(e.name for e in exits))
    try:
        yield pool
    finally:
        if not quiet and pool.requests:
            logger.info("egress: %d requests over %d exit(s), %d rate limited",
                        pool.requests, len(exits), pool.rate_limited)


if __name__ == "__main__":  # print the public address each exit presents
    with rotation() as pool:
        for exit_ in pool.exits:
            request = urllib.request.Request(CHECK_URL, headers={"User-Agent": DEFAULT_UA})
            with exit_.opener.open(request, timeout=20) as response:
                address = response.read().decode().strip()
            print(f"  {exit_.name:22} {address}")
