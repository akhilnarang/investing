"""Gold and silver in rupees, from the COMEX futures close, the ECB's USD/INR and India's import duty.

    rupee price = USD/oz close x USD/INR / 31.1035 x (10 for gold per 10 g, 1000 for silver per kg)
                  x (1 + customs duty of the day)

Indian gold and silver ETFs are valued at the domestic price, which includes import duty, so the
duty is applied here. DUTY is basic customs duty plus cess, approximate to within a point. GST is
excluded since it is paid on purchase.

The USD leg is Yahoo's COMEX front-month futures close (GC=F, SI=F), not an auction fix. Silver's
basis to the fix is wide; see METALS["silver-inr"]["basis"].

Both feeds serve their whole history in one request, so each run rewrites the full series. A duty
change re-prices every day after it.
"""
import json
import math
import time
from datetime import UTC, date, datetime, timedelta

from ..core import egress
from ..core.clock import today_ist
from ..core.enums import SeriesKind
from ..db import day, writing
from . import UA, Throttled, cache_dir, last_day, store_series
from .fx import ECB, ecb

CACHE = cache_dir("metals")
YAHOO = ("https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
         "?period1=0&period2=9999999999&interval=1d")
FX_SOURCE = ECB.format(ccy="INR") + " and " + ECB.format(ccy="USD")   # cited in meta
OUNCE_G = 31.1035
SINCE = date(2000, 1, 1)

# Effective import duty on gold and silver bars, % (basic customs duty plus cess), by start date.
DUTY = [(date(2000, 1, 1), 0.0), (date(2012, 1, 17), 2.0), (date(2012, 3, 17), 4.0),
        (date(2013, 1, 22), 6.0), (date(2013, 6, 5), 8.0), (date(2013, 8, 13), 10.0),
        (date(2019, 7, 6), 12.5), (date(2021, 2, 2), 10.75), (date(2022, 7, 1), 15.0),
        (date(2024, 7, 24), 6.0),
        (date(2026, 5, 13), 15.0)]   # dated from the step in gold ETF NAVs vs metal x FX

# `basis` is the contract's measured spread against the auction fix, quoted in the stored metadata.
METALS = {"gold-inr": {"symbol": "GC=F", "metal": "gold", "unit": "10 g", "per_ounce": 10 / OUNCE_G,
                       "basis": "median +0.08%, stdev 0.64%, 1.3% of days more than 2% out"},
          "silver-inr": {"symbol": "SI=F", "metal": "silver", "unit": "1 kg",
                         "per_ounce": 1000 / OUNCE_G,
                         "basis": "median -0.01%, stdev 1.59%, 14.9% of days more than 2% out"}}

MAX_FX_GAP_DAYS = 10    # do not carry an FX fix forward further than this
MAX_STALE_DAYS = 14     # refuse to rewrite from a feed whose newest point is older than this
MIN_ROWS = 1000         # a real history; anything less is a truncated or refused response


# Yahoo error codes worth retrying. Anything else is treated as a contract break.
TRANSIENT_CODES = ("too many requests", "rate limit", "service unavailable", "gateway timeout",
                   "request timeout", "temporarily unavailable")


def classify(error: dict) -> RuntimeError:
    """The exception for one Yahoo error object: Throttled only for a recognised transient refusal."""
    code = str((error or {}).get("code") or "")
    description = (error or {}).get("description")
    if any(marker in code.lower() for marker in TRANSIENT_CODES):
        return Throttled(f"Yahoo error {code}: {description}")
    return RuntimeError(f"Yahoo error {code}: {description}; not a transient refusal")


def exchange_today(gmtoffset: int) -> date:
    """Today's date at the exchange, from the offset the payload reports."""
    return (datetime.now(tz=UTC) + timedelta(seconds=gmtoffset)).date()


def yahoo_json(symbol: str, max_age_hours: float = 6.0) -> dict:
    """Today's chart for one contract, from the cache when it is fresh. Error payloads are not cached."""
    cached = CACHE / f"{symbol}.json"
    if cached.exists() and (time.time() - cached.stat().st_mtime) < max_age_hours * 3600:
        try:
            return json.loads(cached.read_text())
        except ValueError:
            pass                                  # corrupt cache: re-fetch
    with egress.rotation(user_agent=UA, quiet=True) as pool:
        payload = pool.get_json(YAHOO.format(symbol=symbol), tries=3)
    error = (payload.get("chart") or {}).get("error")
    if error:
        raise classify(error)
    cached.write_text(json.dumps(payload, ensure_ascii=False))
    return payload


def duty_on(d: date) -> float:
    rate = 0.0
    for start, pct in DUTY:
        if d >= start:
            rate = pct
    return rate


def usd_inr() -> dict[date, float]:
    inr, usd = ecb("INR"), ecb("USD")
    rates = {d: inr[d] / usd[d] for d in inr if d in usd and usd[d] > 0}
    if len(rates) < 5000:
        raise RuntimeError(f"ECB USD/INR: only {len(rates)} days; nothing written")
    return rates


def usd_records(payload: dict) -> list[tuple[date, float]]:
    """(date, USD close) sorted by date, without non-finite values or duplicate days.

    The current exchange-local date is dropped: Yahoo's bar for it carries the live price as its
    close, and the next run that day would not overwrite it.
    """
    chart = payload.get("chart") or {}
    error = chart.get("error")
    if error:
        raise classify(error)
    result = chart.get("result") or []
    if not result:
        return []
    quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
    timestamps, closes = result[0].get("timestamp") or [], quote.get("close") or []
    if len(timestamps) != len(closes):
        # The arrays are positional; a length mismatch would shift every date after it.
        raise RuntimeError(f"Yahoo chart has {len(timestamps)} timestamps and {len(closes)} closes")
    offset = result[0].get("meta", {}).get("gmtoffset")
    cutoff = exchange_today(offset if isinstance(offset, int) else 0)
    out: dict[date, float] = {}
    for ts, close in zip(timestamps, closes, strict=True):
        if close is None:
            continue                              # a session Yahoo has a bar for but no close
        try:
            d = datetime.fromtimestamp(int(ts), tz=UTC).date()
            price = float(close)
        except (TypeError, ValueError, OSError):
            continue
        if d >= cutoff:
            continue                              # the session in progress, not a close
        if math.isfinite(price) and price > 0:
            out[d] = price
    return sorted(out.items())


def rupee_series(usd_prices: list[tuple[date, float]], fx: dict[date, float], per_ounce: float,
                 since: date = SINCE, max_gap: int = MAX_FX_GAP_DAYS) -> dict[date, float]:
    """Rupee price per unit for each quoted day, on the latest ECB fix at most `max_gap` days before it."""
    fx_days = sorted(d for d, v in fx.items() if math.isfinite(v) and v > 0)
    out: dict[date, float] = {}
    i, last = 0, None
    for d, usd in usd_prices:
        if d < since:
            continue
        while i < len(fx_days) and fx_days[i] <= d:
            last = fx_days[i]
            i += 1
        if last is None or (d - last).days > max_gap:
            continue
        out[d] = usd * fx[last] * per_ounce * (1 + duty_on(d) / 100)
    return out


def check_recency(newest_usd: date, newest_fx: date, today: date,
                  max_stale: int = MAX_STALE_DAYS) -> str | None:
    """None when both feeds are current enough to rewrite the series, else the reason not to."""
    if (today - newest_usd).days > max_stale:
        return f"newest futures close is {newest_usd}, more than {max_stale} days old"
    if (newest_usd - newest_fx).days > max_stale:
        return (f"newest ECB fix is {newest_fx}, more than {max_stale} days behind the futures "
                f"close of {newest_usd}")
    return None


def _unreachable(exc: BaseException) -> bool:
    """Whether this is the upstream refusing to answer, rather than answering with something wrong.

    Throttled, or a transport failure that the egress pool has wrapped in a RuntimeError (hence the
    string match). "no usable JSON" is not included: it also covers a feed that changed shape.
    """
    if isinstance(exc, Throttled):
        return True
    text = str(exc)
    return YAHOO.split("/v8/")[0] in text and any(
        marker in text for marker in ("HTTPError", "URLError", "TimeoutError", "RemoteDisconnected"))


def fetch_one(db, name: str, fx: dict[date, float], since: date = SINCE) -> int:
    spec = METALS[name]
    prices = usd_records(yahoo_json(spec["symbol"]))
    if not prices:
        raise RuntimeError(f"{name}: empty {spec['symbol']} chart; nothing written")
    why = check_recency(prices[-1][0], max(fx), today_ist())
    if why:
        raise RuntimeError(f"{name}: {why}; existing series preserved")

    newest = last_day(db, name)
    if newest is not None and prices[-1][0] <= newest:
        return 0                                  # nothing newer than the database

    series = rupee_series(prices, fx, spec["per_ounce"], since)
    if len(series) < MIN_ROWS:
        raise RuntimeError(f"{name}: only {len(series)} rows; nothing written")
    # store_series upserts; drop older rows so they do not contradict the metadata's "first".
    with writing(db):
        db.execute("DELETE FROM series WHERE name=? AND d<?", (name, day(min(series))))
    return store_series(db, name, SeriesKind.METAL, series, {
        "scheme_name": f"{spec['metal'].title()} price in rupees",
        "description": f"per {spec['unit']}, COMEX {spec['symbol']} futures close x USD/INR "
                       f"x customs duty",
        "index": f"{spec['metal'].upper()} INR (COMEX futures basis)",
        "source": YAHOO.format(symbol=spec["symbol"]), "fx_source": FX_SOURCE,
        "usd_basis": f"COMEX {spec['symbol']} front-month futures close, not an auction fix; "
                     f"against the auction fix: {spec['basis']}",
        "duty_schedule": [{"from": f"{d:%Y-%m-%d}", "pct": p} for d, p in DUTY],
        "note": "COMEX front-month futures close (Yahoo) x ECB-derived USD/INR, per unit, grossed "
                "up by the customs duty of the day (approximate schedule); GST excluded; the "
                "domestic IBJA price differs by local premium. A futures close is not the auction "
                "fix and carries a basis to it. Gold tracks the fix closely (median +0.08%, stdev 0.64%, 1.3% "
                "of days more than 2% out); silver's basis is materially wider (median -0.01%, "
                "stdev 1.59%, 14.9% of days more than 2% out), so a single silver day is not a "
                "fix. Before 2012 the duty was a small specific one (about Rs 100 to 300 per 10 g "
                "of gold), modelled here as zero, so 2000-2011 domestic levels are understated by "
                "roughly 1-3%",
        "fx_gap_days": MAX_FX_GAP_DAYS})


def fetch(db, only: list[str] | None = None, since: date = SINCE) -> str:
    """The rupee series for each metal.

    An unreachable USD feed keeps the stored series and is named in the summary line. A feed that
    answers with something unparseable or stale raises.
    """
    fx = usd_inr()
    parts, rows, unreachable = [], 0, []
    for name in METALS:
        if only is not None and name not in only:
            continue
        try:
            n = fetch_one(db, name, fx, since=since)
        except RuntimeError as exc:
            if not _unreachable(exc):
                raise
            unreachable.append(name)
            parts.append(f"{name} unreachable, kept stored series")
            continue
        rows += n
        parts.append(f"{name} {n:,}" if n else f"{name} up to date")
    latest = today_ist()
    newest = [last_day(db, n) for n in METALS if only is None or n in only]
    newest = max([d for d in newest if d], default=latest)
    note = f", {len(unreachable)} upstream unreachable" if unreachable else ""
    return f"metals: {', '.join(parts)}, {rows:,} points, latest {newest.isoformat()}{note}"
