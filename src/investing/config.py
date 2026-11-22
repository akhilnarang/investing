"""Settings read from the environment.

`.env` (or INVESTING_ENV_FILE) is loaded on import without overriding the real environment.
`.env.example` lists every key.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = Path(os.environ.get("INVESTING_ENV_FILE") or ROOT / ".env")
load_dotenv(ENV_FILE, override=False)

# A browser agent by default: NSE's WAF stalls other agents and KFintech's CDN refuses them.
USER_AGENT = os.environ.get("INVESTING_USER_AGENT") or (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/154.0.0.0 Safari/537.36")


def _path(key: str, default: Path) -> Path:
    """A path setting; ~ is expanded and a relative one is taken from the repo root."""
    value = os.environ.get(key)
    return ROOT / Path(value).expanduser() if value else default


DB_PATH = _path("INVESTING_DB", ROOT / "data" / "investing.sqlite3")
CACHE_DIR = _path("INVESTING_CACHE", Path.home() / ".cache" / "investing")


def _rate(key: str, default: str) -> tuple[int, int]:
    """A rate limit setting as (requests, seconds), written "30/60"."""
    try:
        count, seconds = (int(part) for part in (os.environ.get(key) or default).split("/"))
    except ValueError as exc:
        raise ValueError(f"{key} must look like 30/60 (requests/seconds)") from exc
    if seconds < 1:
        raise ValueError(f"{key} must have a window of at least 1 second")
    return count, seconds


# Per client address: live NSE routes, the allotment PAN lookup, and prospectus analysis. A count of
# 0 turns a limit off.
RATE_LIMIT_NSE = _rate("INVESTING_RATE_LIMIT_NSE", "30/60")
RATE_LIMIT_ALLOTMENT = _rate("INVESTING_RATE_LIMIT_ALLOTMENT", "60/60")
RATE_LIMIT_ANALYZE = _rate("INVESTING_RATE_LIMIT_ANALYZE", "10/600")

# Header carrying the client address, set by a proxy that overwrites it on every request. When unset,
# uvicorn's address is used, and with --forwarded-allow-ips='*' the client can choose it.
CLIENT_IP_HEADER = os.environ.get("INVESTING_CLIENT_IP_HEADER", "").strip()

# Zerodha Console session for the coin-tradebook tool. The tool prompts when unset.
CONSOLE_COOKIE = os.environ.get("CONSOLE_COOKIE", "")
CONSOLE_CSRF = os.environ.get("CONSOLE_CSRF", "")


def addresses() -> list[str]:
    """Local IPs to bind outgoing sockets to (INVESTING_ADDRESSES, comma separated); see core/egress.py."""
    return [a.strip() for a in os.environ.get("INVESTING_ADDRESSES", "").split(",") if a.strip()]
