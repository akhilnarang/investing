"""US equity benchmarks in rupees, from Yahoo's chart endpoint and the USD/INR already in the database.

Three daily series, each in USD and multiplied by the last ECB fix on or before the day:

  * sp500-tr-inr         ^SP500TR, the S&P 500 total return index itself, since 1988.
  * nasdaq-100-tr-inr    QQQ's dividend-adjusted close, since Yahoo has no ^XNDX history. The ETF's
                         expense ratio makes it slightly easier to beat than the index.
  * us-total-market-tr-inr   VTI's dividend-adjusted close, CRSP US Total Market, 0.03% a year.

The whole USD history is refetched every run: Yahoo rescales earlier adjusted closes at each
ex-dividend date, so appending would splice two adjustment bases.

USD/INR is read from the `series` table that `fx.py` writes.
"""
import bisect
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime

from ..core.clock import today_ist
from ..core.enums import SeriesKind
from ..db import undate
from . import UA, cache_dir, store_series

YAHOO = ("https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
         "?period1={p1}&period2={p2}&interval=1d")
WINDOW_YEARS = 10       # daily points survive a span this long; a whole-history request comes back monthly
MAX_FX_GAP_DAYS = 10    # do not carry an FX fix forward further than this
MIN_ROWS = 500          # a real series; less than this is a truncated or refused response
MAX_STALE_DAYS = 7      # a newest observation older than this means a stale feed

SERIES = {
    "sp500-tr-inr": {
        "symbol": "^SP500TR", "adjusted": False, "since": 1988,
        "scheme_name": "S&P 500 TR in rupees", "index": "S&P 500 TR INR",
        "what": "S&P 500 total return index (dividends reinvested), USD, converted at the ECB USD/INR"},
    "nasdaq-100-tr-inr": {
        "symbol": "QQQ", "adjusted": True, "since": 1999,
        "scheme_name": "Nasdaq-100 TR in rupees (QQQ, dividends reinvested)",
        "index": "NASDAQ 100 TR INR",
        "what": "QQQ dividend-adjusted close as the Nasdaq-100 total return (net of the ETF's expense, "
                "0.20% a year and 0.18% from December 2025, and its tracking difference), USD, "
                "converted at the ECB USD/INR"},
    "us-total-market-tr-inr": {
        "symbol": "VTI", "adjusted": True, "since": 2001,
        "scheme_name": "US total market TR in rupees (VTI, dividends reinvested)",
        "index": "US TOTAL MARKET TR INR",
        "what": "VTI dividend-adjusted close as the CRSP US Total Market total return "
                "(net of 0.03% a year), USD, converted at the ECB USD/INR"},
}


def get_json(url: str, tries: int = 5) -> dict:
    """Yahoo answers 429 to eager clients: wait 20, 40, 80, 160 seconds, or its Retry-After capped."""
    last = None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if attempt == tries - 1:
                break
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            wait = (min(int(retry_after), 300) if retry_after and retry_after.isdigit()
                    else (20 * 2 ** attempt if exc.code == 429 else 3 * (attempt + 1)))
            time.sleep(wait + random.uniform(0, 3))
        except Exception as exc:  # any transport or decode failure is retried
            last = exc
            if attempt == tries - 1:
                break
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{url}: {last!r}")


def window(symbol: str, year: int, until_year: int, adjusted: bool) -> dict[date, float]:
    p1 = int(datetime(year, 1, 1, tzinfo=UTC).timestamp())
    p2 = int(datetime(until_year + 1, 1, 1, tzinfo=UTC).timestamp())
    payload = get_json(YAHOO.format(symbol=urllib.parse.quote(symbol), p1=p1, p2=p2))
    chart = payload.get("chart") or {}
    # Yahoo throttles with HTTP 200, no result and an error object.
    if chart.get("error"):
        raise RuntimeError(f"{symbol} {year}-{until_year}: Yahoo returned {chart['error']}")
    result = chart.get("result") or []
    if not result:
        return {}
    got = result[0]
    stamps = got.get("timestamp") or []
    indicators = got.get("indicators") or {}
    quote = (indicators.get("quote") or [{}])[0].get("close") or []
    if adjusted:
        adj = (indicators.get("adjclose") or [{}])[0].get("adjclose")
        # Raw and adjusted closes sit on different scales; splicing them corrupts the series.
        if not adj:
            raise RuntimeError(f"{symbol} {year}-{until_year}: Yahoo returned no adjusted closes")
        quote = adj
    out: dict[date, float] = {}
    for ts, v in zip(stamps, quote, strict=True):
        if v is None or not float(v) > 0:
            continue
        out[datetime.fromtimestamp(ts, tz=UTC).date()] = float(v)
    return out


def usd_inr(db) -> dict[date, float]:
    """The ECB-derived USD/INR that fx.py writes."""
    fx = {undate(d): v for d, v in db.execute("SELECT d, v FROM series WHERE name='usd-inr'")}
    if len(fx) < MIN_ROWS:
        raise RuntimeError(f"usd-inr holds {len(fx)} days; run the fx source before this one")
    return fx


def convert(usd: dict[date, float], fx: dict[date, float],
            max_gap: int = MAX_FX_GAP_DAYS) -> tuple[dict[date, float], int]:
    """Rupee level per US trading day, at the latest ECB fix at most `max_gap` days before it."""
    fx_days = sorted(fx)
    out: dict[date, float] = {}
    skipped = 0
    for d, v in usd.items():
        i = bisect.bisect_right(fx_days, d) - 1
        if i < 0 or (d - fx_days[i]).days > max_gap:
            skipped += 1
            continue
        out[d] = v * fx[fx_days[i]]
    return out, skipped


def have(db, name: str) -> tuple[int, date | None]:
    n, newest = db.execute("SELECT COUNT(*), MAX(d) FROM series WHERE name=?", (name,)).fetchone()
    return n, (undate(newest) if newest is not None else None)


def fetch_one(db, stem: str, fx: dict[date, float], pause: float = 4.0) -> int:
    spec = SERIES[stem]
    today = today_ist()
    usd: dict[date, float] = {}
    year = spec["since"]
    while year <= today.year:
        until = min(year + WINDOW_YEARS - 1, today.year)
        usd.update(window(spec["symbol"], year, until, spec["adjusted"]))
        year = until + 1
        time.sleep(pause)

    # Every run rewrites the whole history, so refuse a short or stale answer.
    rows, newest = have(db, stem)
    if len(usd) < MIN_ROWS:
        raise RuntimeError(f"{stem}: {len(usd)} USD points; the stored series is kept")
    if rows and (len(usd) < rows - 5 or (newest and max(usd) < newest)):
        raise RuntimeError(f"{stem}: fetched {len(usd)} rows to {max(usd)}, the database holds "
                           f"{rows} to {newest}; kept as it was")
    if (today - max(usd)).days > MAX_STALE_DAYS:
        raise RuntimeError(f"{stem}: newest USD observation is {max(usd)}, more than "
                           f"{MAX_STALE_DAYS} days old; kept as it was")

    (cache_dir("us_indexes") / f"{stem}.json").write_text(
        json.dumps({d.isoformat(): v for d, v in sorted(usd.items())}), encoding="utf-8")
    inr, skipped = convert(usd, fx)
    return store_series(db, stem, SeriesKind.INDEX, inr, {
        "scheme_name": spec["scheme_name"], "index": spec["index"], "symbol": spec["symbol"],
        "what": spec["what"], "adjusted_close": spec["adjusted"],
        "source": f"https://finance.yahoo.com/quote/{spec['symbol']}/history/",
        "fx_source": "usd-inr, the ECB reference rates written by the fx source",
        "fx_gap_days": MAX_FX_GAP_DAYS, "newest_source_day": max(usd).isoformat(),
        "usd_points": len(usd), "days_without_fx": skipped,
        "note": "the whole USD history is refetched each run: Yahoo rescales earlier adjusted "
                "closes on every dividend, so an incremental append would splice two bases"})


def fetch(db, only: list[str] | None = None, pause: float = 4.0) -> str:
    fx = usd_inr(db)
    wanted = [s for s in SERIES if only is None or s in only]
    done, points, failed = 0, 0, []
    for stem in wanted:
        try:
            n = fetch_one(db, stem, fx, pause=pause)
        except Exception as exc:  # one failed series does not stop the others
            failed.append(str(exc))
            continue
        done += 1
        points += n
    return (f"us_indexes: {done} of {len(wanted)} series, {points:,} points in rupees"
            + (f"; failed: {'; '.join(failed)}" if failed else ""))
