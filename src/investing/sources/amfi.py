"""NAV history from AMFI's portal, one fund house at a time, pulled in date windows newest first.

A house's window starts at the oldest newest-row among its live codes. Codes far behind their house
(matured or merged) do not set the window. A code with no history is probed backwards until
EMPTY_WINDOWS windows come back blank.
"""
import http.client
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import date, datetime, timedelta

from ..core.clock import today_ist
from ..core.enums import Plan
from ..db import day, undate, writing
from . import UA

logger = logging.getLogger(__name__)

AMCS_URL = "https://www.amfiindia.com/api/populate-mf"
HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
HEADER = ("Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;"
          "ISIN Div Reinvestment;Net Asset Value;Date")
NO_DATA = "No data found"
OPEN_ENDED = 1
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

CONNECT_TIMEOUT = 10        # seconds; the portal drops some SYNs, and a new port connects at once
CONNECT_RETRIES = 6
OVERLAP_DAYS = 7            # re-request this far back: AMFI revises recent NAVs and publishes late
EMPTY_WINDOWS = 2           # blank windows a probe gets before its history is taken to have started
NEW_CODE_DAYS = 30          # newest window when a house has nothing cached at all
STALE_DAYS = 60             # this far behind its house, a code no longer sets the window


# Old fund house names to the name AMFI lists now, after acquisitions and renames.
ALIASES = {
    "idfc mutual fund": "Bandhan Mutual Fund",
    "iifl mutual fund": "360 ONE Mutual Fund",
    "360 one asset": "360 ONE Mutual Fund",
    "baroda mutual fund": "Baroda BNP Paribas Mutual Fund",
    "bnp paribas mutual fund": "Baroda BNP Paribas Mutual Fund",
    "dhfl pramerica mutual fund": "PGIM India Mutual Fund",
    "mahindra mutual fund": "Mahindra Manulife Mutual Fund",
    "l&t mutual fund": "HSBC Mutual Fund",
    "principal mutual fund": "Sundaram Mutual Fund",
    "indiabulls mutual fund": "Groww Mutual Fund",
    "essel mutual fund": "Navi Mutual Fund",
    "boi axa mutual fund": "Bank of India Mutual Fund",
}


def _key(name: str | None) -> str:
    """Lowercase alphanumerics, so punctuation and spacing differences still match."""
    return re.sub(r"[^a-z0-9&]", "", (name or "").lower())


def resolve(name: str | None, by_key: dict[str, dict]) -> dict | None:
    if not name:
        return None
    alias = ALIASES.get(name.strip().lower())
    return by_key.get(_key(alias or name))


def amfi_date(d: date) -> str:
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def parse_amfi_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%d-%b-%Y").date()


class _QuickConnect(http.client.HTTPSConnection):
    """Connect gives up after CONNECT_TIMEOUT while reads keep the long timeout."""

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT,
                                             source_address=self.source_address)
        self.sock.settimeout(self.timeout)
        if self._tunnel_host:
            self._tunnel()
        # Behind a proxy self.host is the proxy, so SNI uses the tunnelled host.
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._tunnel_host or self.host)


class _QuickHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_QuickConnect, req, context=self._context)


OPENER = urllib.request.build_opener(_QuickHandler())
STALLS = 0


def _open(request: urllib.request.Request, timeout: int):
    global STALLS
    for n in range(CONNECT_RETRIES):
        try:
            return OPENER.open(request, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            stalled = isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason)
            if not stalled or n == CONNECT_RETRIES - 1:
                raise
            STALLS += 1


def get(url: str, attempts: int = 4, timeout: int = 180, report: bool = False) -> bytes:
    """One request with exponential backoff.

    AMFI throttles with an HTTP 200 error page, so a report's body is validated inside the retry."""
    error = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with _open(request, timeout) as response:
                payload = response.read()
            if report:
                # The report is newline-terminated; the no-data HTML page is not.
                if payload.startswith(HEADER.encode()) and not payload.endswith(b"\n"):
                    raise ValueError("report ended mid-line")
                text = payload.decode("utf-8", "replace")
                if HEADER not in text and NO_DATA not in text:
                    raise ValueError("response is neither the NAV report nor the no-data page")
            return payload
        except Exception as exc:  # a failed attempt is retried
            error = exc
            if attempt < attempts - 1:
                time.sleep(5.0 * 2 ** attempt)
    raise RuntimeError(f"AMFI request failed after {attempts} attempts: {url}: {error!r}")


def houses() -> list[dict]:
    got = json.loads(get(AMCS_URL).decode("utf-8"))
    if not isinstance(got, list) or len(got) < 40:
        raise RuntimeError(f"AMFI returned {len(got) if isinstance(got, list) else '?'} fund houses")
    return got


# AMFI writes both "Equity Scheme - ..." and "Equity Schemes - ...", sometimes in one report.
def normalise_category(category: str | None) -> str | None:
    return None if not category else re.sub(r"\bSchemes\b", "Scheme", category.strip())


def parse(text: str) -> dict[str, dict]:
    """Schemes keyed by AMFI code from one report; {} for AMFI's no-data page."""
    if HEADER not in text:
        if NO_DATA in text:
            return {}
        raise ValueError("response is neither the NAV report nor the no-data page")
    schemes: dict[str, dict] = {}
    scheme_type = category = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = [f.strip() for f in line.split(";")]
        if len(fields) != 8:
            # "Open Ended Schemes ( Equity Scheme - Flexi Cap Fund )" precedes each block; the bare
            # fund-house name that follows it carries no parentheses.
            m = re.fullmatch(r"(.+?Schemes?)\s*\(\s*(.+?)\s*\)", line)
            if m:
                scheme_type, category = m.group(1), m.group(2)
            continue
        if fields[0] == "Scheme Code":
            continue
        code, name, _plan, _option, isin_growth, isin_reinvest, nav, when = fields
        scheme = schemes.setdefault(code, {
            "scheme_name": name, "scheme_type": scheme_type,
            "scheme_category": normalise_category(category),
            "isin_growth": isin_growth or None, "isin_div_reinvestment": isin_reinvest or None,
            "navs": {}, "skipped": 0})
        try:
            d = parse_amfi_date(when)
            value = float(nav)
        except ValueError:
            scheme["skipped"] += 1          # AMFI emits "#N/A" for a handful of very old rows
            continue
        scheme["navs"][d] = value
    return schemes


def fetch_house(house: dict, since: date, until: date, window_days: int, pause: float,
                must_reach: date | None = None,
                probe: frozenset[str] = frozenset()) -> tuple[dict[str, dict], int]:
    """Every open-ended scheme of one house, merged over date windows newest first."""
    schemes: dict[str, dict] = {}
    end, requests, blank = until, 0, 0
    while end >= since:
        start = max(since, end - timedelta(days=window_days - 1)) if window_days > 0 else since
        if requests == 0 and must_reach is not None:
            start = max(start, must_reach)   # the newest window is only as long as the codes need
        query = urllib.parse.urlencode({"mf": house["mfId"], "tp": OPEN_ENDED,
                                        "frmdt": amfi_date(start), "todt": amfi_date(end)})
        text = get(f"{HISTORY_URL}?{query}", report=True).decode("utf-8", "replace")
        requests += 1
        window = parse(text)
        for code, scheme in window.items():
            merged = schemes.setdefault(code, {**scheme, "navs": {}, "skipped": 0})
            for when, nav in scheme["navs"].items():
                merged["navs"].setdefault(when, nav)   # the newest window's value wins
            merged["skipped"] += scheme["skipped"]
        end = start - timedelta(days=1)
        if must_reach is not None and end < must_reach and not any(window.get(c, {}).get("navs") for c in probe):
            blank += 1
            if blank >= EMPTY_WINDOWS or not probe:
                break
        elif probe and any(window.get(c, {}).get("navs") for c in probe):
            blank = 0
        if end >= since:
            time.sleep(pause)
    return schemes, requests


def newest_rows(db, codes: list[str]) -> dict[str, date | None]:
    """The newest NAV day the database holds for each code."""
    out: dict[str, date | None] = {c: None for c in codes}
    for code, d in db.execute(
            f"SELECT scheme_code, MAX(d) FROM nav WHERE scheme_code IN ({','.join('?' * len(codes))}) "
            "GROUP BY scheme_code", [int(c) for c in codes]):
        out[str(code)] = undate(d)
    return out


def stale_codes(codes: list[str], newest: Mapping[str, date | None]) -> list[str]:
    """Codes more than STALE_DAYS behind the newest among them: matured, merged, no longer published."""
    dates = [d for d in (newest.get(c) for c in codes) if d]
    if not dates:
        return []
    cutoff = max(dates) - timedelta(days=STALE_DAYS)
    return [c for c in codes if (d := newest.get(c)) and d < cutoff]


def window_start(codes: list[str], newest: Mapping[str, date | None], since: date) -> date:
    """OVERLAP_DAYS before the oldest newest-row among the live codes, never before `since`."""
    dates = [newest.get(c) for c in codes]
    if not dates or any(d is None for d in dates):
        return since
    stale = set(stale_codes(codes, newest))
    live = [d for c, d in zip(codes, dates, strict=True) if c not in stale and d is not None]
    return max(since, min(live) - timedelta(days=OVERLAP_DAYS)) if live else since


def store(db, code: str, scheme: dict, house: dict) -> int:
    """One scheme's rows, plus the fund row AMFI's report describes."""
    # Readers expect scheme_code in the meta blob as well as the column.
    meta = {"scheme_code": int(code),
            **{k: scheme.get(k) for k in ("scheme_name", "scheme_type", "scheme_category",
                                          "isin_growth", "isin_div_reinvestment")},
            "fund_house": house.get("mfName"), "source": "amfi",
            "source_url": HISTORY_URL}
    name = scheme.get("scheme_name") or code
    db.execute(
        "INSERT INTO fund (scheme_code, name, isin_growth, fund_house, category, plan, meta) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(scheme_code) DO UPDATE SET "
        "name=excluded.name, isin_growth=excluded.isin_growth, fund_house=excluded.fund_house, "
        # Merge: universe.py, ter.py and sif.py write into the same blob. AMFI's keys win.
        "category=excluded.category, meta=json_patch(fund.meta, excluded.meta)",
        (int(code), name, scheme.get("isin_growth"), house.get("mfName"),
         normalise_category(scheme.get("scheme_category")),
         Plan.DIRECT if "direct" in name.lower() else Plan.REGULAR,
         json.dumps(meta, ensure_ascii=False)))
    rows = [(int(code), day(d), v) for d, v in scheme["navs"].items()]
    db.executemany("INSERT OR REPLACE INTO nav (scheme_code, d, nav) VALUES (?,?,?)", rows)
    return len(rows)


def fetch(db, since: date | None = None, until: date | None = None, window_days: int = 366,
          pause: float = 0.5, only: list[str] | None = None) -> str:
    """Every house that has schemes in the database, brought up to date."""
    since = since or date(2006, 4, 1)
    until = until or today_ist()
    all_houses = houses()
    by_key = {_key(h.get("mfName")): h for h in all_houses}

    wanted: dict[str, list[str]] = {}
    unmatched: set[str] = set()
    for code, fund_house in db.execute(
            "SELECT scheme_code, fund_house FROM fund WHERE fund_house IS NOT NULL"):
        if only is not None and str(code) not in only:
            continue
        h = resolve(fund_house, by_key)
        if h is None:
            # SIF providers are not on AMFI's list; sources/sif.py fetches them.
            if "sif" not in (fund_house or "").strip().lower():
                unmatched.add(fund_house)
            continue
        wanted.setdefault(h["mfName"], []).append(str(code))
    if not wanted:
        return "amfi: no houses matched the funds in the database; nothing fetched"

    houses_done = codes_done = rows = requests = 0
    for name, codes in sorted(wanted.items()):
        house = by_key[_key(name)]
        newest = newest_rows(db, codes)
        probe = frozenset(c for c in codes if newest.get(c) is None)
        start = window_start(codes, newest, since)
        # With nothing cached, start with a short recent window.
        must_reach = start if any(newest.values()) else until - timedelta(days=NEW_CODE_DAYS)
        try:
            schemes, n = fetch_house(house, since if probe else start, until, window_days, pause,
                                     must_reach=must_reach, probe=probe)
        except Exception as exc:  # one house failing does not stop the others
            logger.info("amfi %s: %s", name, exc)
            continue
        requests += n
        with writing(db):
            for code, scheme in schemes.items():
                if scheme["navs"]:
                    rows += store(db, code, scheme, house)
                    codes_done += 1
        houses_done += 1
    return (f"amfi: {houses_done} houses, {codes_done:,} schemes, {rows:,} NAV rows, "
            f"{requests} requests"
            + (f", {STALLS} stalled connects retried" if STALLS else "")
            + (f", {len(unmatched)} house(s) AMFI does not list: {', '.join(sorted(unmatched))}"
               if unmatched else ""))
