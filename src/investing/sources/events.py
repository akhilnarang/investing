"""Corporate actions for every NSE stock in the company table, from Yahoo Finance.

One request per `<SYMBOL>.NS` (monthly interval, `events=div,splits`) into corporate_action.

A "split" is any share-count ratio Yahoo records: splits, bonuses (a 1:1 bonus is 2:1) and
consolidations (1:10 is 0.1). Demergers and rights issues are not covered.

Yahoo answers HTTP 200 with an error object both when throttling and for an unknown symbol. Only a
not-found error marks a symbol missing; anything else raises and is retried next run.
"""
import json
import logging
import time
import urllib.parse
from datetime import UTC, date, datetime

from ..core import egress
from ..core.clock import today_ist
from ..db import day, writing
from . import UA, Throttled, cache_dir

logger = logging.getLogger(__name__)

CACHE = cache_dir("events")
CHART = ("https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
         "?period1={p1}&period2={p2}&interval=1mo&events=div,splits")
MAX_AGE_DAYS = 7      # a symbol refetched no more often than this
SINCE = datetime(2000, 1, 1, tzinfo=UTC)


def chart_url(symbol: str) -> str:
    p2 = int(datetime.now(UTC).timestamp()) + 86400
    return CHART.format(symbol=urllib.parse.quote(symbol + ".NS"),
                        p1=int(SINCE.timestamp()), p2=p2)


def parse(payload: dict | None) -> dict | None:
    """Splits and dividends from one chart payload; None when Yahoo has no such symbol.

    No such symbol is a 404 or a chart.error saying "Not Found". Any other chart.error raises.
    """
    if payload is None:
        return None                                   # a 404: Yahoo has no such symbol
    chart = payload.get("chart") or {}
    error = chart.get("error")
    if error:
        code = str((error or {}).get("code") or "").lower()
        if "not found" not in code and "no data" not in code:
            raise Throttled(f"Yahoo error {error.get('code')}: {error.get('description')}")
        return None
    result = chart.get("result") or []
    if not result:
        return None
    ev = result[0].get("events") or {}
    splits, dividends = [], []
    for v in (ev.get("splits") or {}).values():
        try:
            num, den = v.get("numerator"), v.get("denominator")
            if num is not None and den is not None and float(den):
                ratio = float(num) / float(den)
            else:
                a, b = str(v.get("splitRatio", "1:1")).split(":")
                ratio = float(a) / float(b)
            splits.append((datetime.fromtimestamp(int(v["date"]), tz=UTC).date().isoformat(),
                           round(ratio, 6)))
        except (TypeError, ValueError, ZeroDivisionError, KeyError, IndexError):
            continue          # skip a ratio written as "2" rather than "2:1"
    for v in (ev.get("dividends") or {}).values():
        try:
            dividends.append((datetime.fromtimestamp(int(v["date"]), tz=UTC).date().isoformat(),
                              float(v["amount"])))
        except (TypeError, ValueError, KeyError):
            continue
    return {"splits": sorted(splits), "dividends": sorted(dividends)}


def store(db, isin: str, events: dict) -> int:
    """One symbol's actions. A dividend and a split can share a day, which the kind separates."""
    rows = [(isin, day(date.fromisoformat(d)), "split", ratio, None)
            for d, ratio in events.get("splits") or []]
    rows += [(isin, day(date.fromisoformat(d)), "dividend", None, amount)
             for d, amount in events.get("dividends") or []]
    db.executemany("INSERT OR REPLACE INTO corporate_action (isin, d, kind, ratio, amount) "
                   "VALUES (?,?,?,?,?)", rows)
    return len(rows)


def cached(isin: str, max_age_days: float) -> dict | None:
    """The last answer for this ISIN, while it is fresher than max_age_days."""
    path = CACHE / f"{isin}.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        age = (today_ist() - date.fromisoformat(doc["retrieved"])).days
    except (ValueError, OSError, KeyError):
        return None
    return doc if age < max_age_days else None


def fetch(db, only: list[str] | None = None, max_age_days: float = MAX_AGE_DAYS, limit: int = 0,
          min_gap: float = 2.0) -> str:
    """Every listed stock, oldest-refreshed first, never-fetched ahead of all."""
    wanted = {s.strip().upper() for s in only or []}
    stocks = [(r["isin"], r["symbol"]) for r in db.execute(
        "SELECT isin, symbol FROM company WHERE symbol IS NOT NULL")
        if not wanted or r["symbol"].upper() in wanted]
    if not stocks:
        return "events: no listed stocks in the database"

    def retrieved(isin: str) -> str:
        doc = cached(isin, 10 ** 6)
        return (doc or {}).get("retrieved") or ""

    stocks.sort(key=lambda kv: (retrieved(kv[0]), kv[1]))

    done = fresh = unknown = skipped = actions = 0
    with egress.rotation(min_gap=min_gap, user_agent=UA) as pool:
        for isin, symbol in stocks:
            if not wanted and cached(isin, max_age_days) is not None:
                skipped += 1
                continue
            try:
                payload = pool.get_json(chart_url(symbol), tries=3)
            except egress.NotFound:
                payload = None                       # a 404: Yahoo has no such symbol
            except RuntimeError as exc:
                # Every exit failed; record nothing so the symbol is retried next run.
                logger.info("events: %s: %s; stopping this run", symbol, exc)
                break
            try:
                events = parse(payload)
            except Throttled as exc:
                logger.info("events: %s: %s; stopping this run", symbol, exc)
                break
            if events is None:
                unknown += 1
                events = {"splits": [], "dividends": [], "unknown_to_yahoo": True}
            else:
                fresh += 1
                with writing(db):
                    actions += store(db, isin, events)
            (CACHE / f"{isin}.json").write_text(
                json.dumps({"isin": isin, "symbol": symbol, "retrieved": today_ist().isoformat(),
                            **events}, ensure_ascii=False), encoding="utf-8")
            done += 1
            if limit and done >= limit:
                break
            time.sleep(min_gap / max(1, len(pool.exits)))

    with writing(db):
        db.execute("INSERT OR REPLACE INTO source_state VALUES ('events', ?, ?, ?)",
                   (datetime.now(UTC).isoformat(timespec="seconds"),
                    db.execute("SELECT MAX(d) FROM corporate_action").fetchone()[0],
                    f"{done} symbols refreshed, {unknown} unknown to Yahoo"))
    return (f"events: {done} symbols ({fresh} with a chart, {unknown} unknown to Yahoo), "
            f"{actions:,} corporate actions, {skipped:,} already current")
