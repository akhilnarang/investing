"""Total-return index series from NSE's own historical endpoints.

Equity indices use the TRI endpoint (gross total return). Fixed-income indices (Nifty 1D Rate,
Nifty 10 yr Benchmark G-Sec) are already total-return and use the general historical endpoint,
which names its fields differently.

Fetched in one-year windows newest first, stopping after three consecutive empty windows.
"""
import json
import re
import time
import urllib.request
from datetime import date, datetime, timedelta

from ..core.clock import today_ist
from ..core.enums import SeriesKind
from . import UA, last_day, store_series

TRI_URL = "https://www.niftyindices.com/BackPage/getTotalReturnIndexString"
FI_URL = "https://www.niftyindices.com/BackPage/getHistoricaldatatabletoString"
PAGE = "https://www.niftyindices.com/reports/historical-data"
OVERLAP_DAYS = 7      # re-request this far back: NSE revises recent values, and the merge is by date
MIN_ROWS = 200        # a real index series; anything less is a truncated or refused response

# The indices the site is built on. Fixed-income ones are flagged: they use the other endpoint.
INDEXES = [
    ("NIFTY 50", False), ("NIFTY 100", False), ("NIFTY 500", False),
    ("NIFTY MIDCAP 150", False), ("NIFTY SMALLCAP 250", False), ("NIFTY LARGEMIDCAP 250", False),
    ("NIFTY FINANCIAL SERVICES", False), ("NIFTY HEALTHCARE", False),
    ("NIFTY INFRASTRUCTURE", False), ("NIFTY IT", False),
    ("NIFTY 1D RATE INDEX", True), ("NIFTY 10 YR BENCHMARK G-SEC", True),
    # Indices the index-fund page and the tracking document need. A series that is read must be
    # listed here, or it is not refreshed.
    ("NIFTY NEXT 50", False), ("NIFTY 200", False), ("NIFTY TOTAL MARKET", False),
    ("NIFTY MIDCAP 100", False), ("NIFTY SMALLCAP 100", False), ("NIFTY MICROCAP 250", False),
    ("NIFTY BANK", False), ("NIFTY PHARMA", False),
    ("NIFTY COMPOSITE G-SEC INDEX", True),
]


def slug_of(index: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", index.lower()).strip("-")


def fetch_window(index: str, start: date, end: date, attempts: int = 4, url: str = TRI_URL) -> list[dict]:
    body = json.dumps({"cinfo": f"{{'name':'{index}','startDate':'{start:%d-%b-%Y}',"
                                f"'endDate':'{end:%d-%b-%Y}','indexName':'{index}'}}"}).encode()
    request = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA, "Content-Type": "application/json; charset=utf-8",
        "Referer": PAGE, "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01"})
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and "d" in payload:
                payload = json.loads(payload["d"])
            if isinstance(payload, list):
                return payload
            raise ValueError(f"unexpected payload shape: {str(payload)[:120]}")
        except Exception as exc:  # transport and decode failures are retried alike
            error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{index} {start}..{end} failed after {attempts} attempts: {error!r}")


def rows_of(batch: list[dict]) -> dict[date, float]:
    """The two endpoints name their columns differently; both carry a date and a level."""
    out: dict[date, float] = {}
    for row in batch:
        try:
            d = datetime.strptime(row.get("Date") or row["HistoricalDate"], "%d %b %Y").date()
            v = float(row["TotalReturnsIndex"] if "TotalReturnsIndex" in row else row["CLOSE"])
        except (KeyError, ValueError, TypeError):
            continue
        out[d] = v
    return out


def fetch_one(db, index: str, fixed_income: bool = False, since: date | None = None,
              pause: float = 0.5) -> int:
    name = slug_of(index)
    since = since or date(1995, 1, 1)
    newest = last_day(db, name)
    if newest is not None:
        if newest >= today_ist():
            return 0
        since = max(since, newest - timedelta(days=OVERLAP_DAYS))

    series: dict[date, float] = {}
    end, empty = today_ist(), 0
    while end >= since and empty < 3:
        start = max(since, end - timedelta(days=364))
        got = rows_of(fetch_window(index, start, end, url=FI_URL if fixed_income else TRI_URL))
        series.update(got)
        empty = empty + 1 if not got else 0
        end = start - timedelta(days=1)
        time.sleep(pause)

    # On a first fetch the whole history must arrive; on an incremental one a short window is normal.
    if newest is None and len(series) < MIN_ROWS:
        raise RuntimeError(f"{index}: only {len(series)} rows on a first fetch; nothing written")
    if not series:
        return 0
    return store_series(db, name, SeriesKind.INDEX, series, {
        "scheme_name": f"{index} TRI", "index": index,
        "source": FI_URL if fixed_income else TRI_URL,
        "description": "total return index, dividends reinvested, gross of tax"})


def fetch(db, only: list[str] | None = None, pause: float = 0.5) -> str:
    wanted = [(i, fi) for i, fi in INDEXES if only is None or slug_of(i) in only]
    done = rows = 0
    failed = []
    for index, fixed_income in wanted:
        try:
            n = fetch_one(db, index, fixed_income, pause=pause)
        except Exception as exc:  # one failed index does not stop the others
            failed.append(f"{index} ({exc})")
            continue
        if n:
            done += 1
            rows += n
    return (f"nse_index: {done} of {len(wanted)} series updated, {rows:,} points"
            + (f"; failed: {'; '.join(failed)}" if failed else ""))
