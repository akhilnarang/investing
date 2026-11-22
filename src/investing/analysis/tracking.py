"""The `index_tracking` document for `/index-funds`: each plain index fund against its NSE TRI.

Per fund:

  cagr        trailing 1/3/5/10-year CAGR of the fund
  td          tracking difference, fund minus index, percentage points a year, per horizon
  years       calendar-year tracking difference for every full year the fund existed
  te3         tracking error: annualised standard deviation of monthly return differences, last 3 years
  ber, aum    AMFI's official direct-plan BER (Groww's TER where AMFI's name does not key) and Groww's AUM

Rows carry AMFI's scheme name; `short` drops the plan and option words.
"""
import json
import logging
import math
import re
import sqlite3
from datetime import date, timedelta
from itertools import pairwise
from statistics import median, stdev

from ..core.clock import month_ends
from ..core.enums import AnalysisKind, Plan
from ..db import day, undate, writing
from ..web.queries import plan_free_name
from . import nav
from .longterm import (
    DECORATORS,
    benchmark_series,
    index_key,
    is_payout,
    is_plain_index,
    normalise_name,
    scheme_ter,
    ter_table,
    universe,
)
from .metrics import rnd

logger = logging.getLogger(__name__)

LABELS = {
    "nifty_50": "Nifty 50", "nifty_next_50": "Nifty Next 50", "nifty_100": "Nifty 100", "nifty_200": "Nifty 200",
    "nifty_500": "Nifty 500", "nifty_total_market": "Nifty Total Market", "nifty_largemidcap_250": "Nifty LargeMidcap 250",
    "nifty_midcap_150": "Nifty Midcap 150", "nifty_midcap_100": "Nifty Midcap 100",
    "nifty_smallcap_250": "Nifty Smallcap 250", "nifty_smallcap_100": "Nifty Smallcap 100",
    "nifty_microcap_250": "Nifty Microcap 250",
}
# indexes longterm.index_key does not know; tried only when it returns None
EXTRA = [("nifty_total_market", r"nifty\s+total\s+market\b"), ("nifty_200", r"nifty\s*200\b"),
         ("nifty_midcap_100", r"nifty\s+midcap\s+100\b"), ("nifty_smallcap_100", r"nifty\s+smallcap\s+100\b")]
# strategy or blended products that share a parent index's name
NOT_PLAIN = re.compile(r"multicap|50\s*25\s*25|shariah|g\s*sec|sdl|\bplus\b|bharat|sensex|\bbse\b|arbitrage|hybrid|"
                       r"debt|conservative|\bfof\b|etf|top\s*\d+|select|\d+\s*:\s*\d+", re.I)
HORIZONS = (1, 3, 5, 10)
MIN_YEARS = 1.0
OPTION_TAIL = re.compile(r"[\s-]*\bgrowth\b.*$", re.I)


def index_of(name: str) -> str | None:
    n = normalise_name(name)
    if NOT_PLAIN.search(n) or DECORATORS.search(n):
        return None
    key = index_key(name)
    if key:
        return key
    for extra, pattern in EXTRA:
        if re.search(pattern, n):
            return extra
    return None


def series_name(key: str) -> str:
    return key.replace("_", "-")


def short(name: str) -> str:
    return OPTION_TAIL.sub("", plan_free_name(name)).strip(" -")


def ret(s: dict[date, float], a: date, b: date) -> float | None:
    va, vb = nav.nav_on(s, a), nav.nav_on(s, b)
    if va is None or vb is None or va <= 0:
        return None
    return (vb / va - 1) * 100


def evaluate(row: dict, series: dict[date, float], index: dict[date, float], as_of: date, ters: dict) -> dict:
    start = min(series)
    cagr, td = {}, {}
    for h in HORIZONS:
        if (as_of - start).days < 365.25 * h - 10:
            cagr[str(h)] = None
            td[str(h)] = None
            continue
        f, i = nav.cagr(series, h, as_of), nav.cagr(index, h, as_of)
        cagr[str(h)] = rnd(f)
        td[str(h)] = rnd(f - i) if f is not None and i is not None else None
    years = {}
    for y in range(start.year + 1, as_of.year):
        a, b = date(y - 1, 12, 31), date(y, 12, 31)
        if start > a:
            continue
        rf, ri = ret(series, a, b), ret(index, a, b)
        if rf is not None and ri is not None:
            years[str(y)] = rnd(rf - ri)
    ends = month_ends(as_of.replace(day=1) - timedelta(days=1), 37)
    diffs = []
    for a, b in pairwise(ends):
        if a < start:
            continue
        rf, ri = ret(series, a, b), ret(index, a, b)
        if rf is not None and ri is not None:
            diffs.append(rf - ri)
    te3 = rnd(stdev(diffs) * math.sqrt(12)) if len(diffs) >= 12 else None
    # AMFI's BER, which is stable; its TER adds one day's costs annualised. Groww's TER is the fallback.
    official = scheme_ter(ters, row["scheme_name"]).get("effective_ber")
    if official is not None:
        ber, source = official, "official AMFI BER"
    else:
        groww = row.get("groww_expense_ratio")
        try:
            ber, source = (float(groww) if groww is not None else None), "Groww TER"
        except (TypeError, ValueError):
            ber, source = None, "Groww TER"
    return {
        "name": row["scheme_name"], "short": short(row["scheme_name"]), "scheme_code": str(row["scheme_code"]),
        "slug": row.get("search_id"), "fund_house": row.get("fund_house"),
        "ber": rnd(ber), "ber_source": source,
        "aum_cr": rnd(row.get("aum"), 0), "since": start.isoformat(),
        "history_years": round((as_of - start).days / 365.25, 1),
        "cagr": cagr, "td": td, "years": years, "te3": te3, "te3_months": len(diffs),
        "beyond_fee_3y": rnd(td["3"] + ber) if td.get("3") is not None and ber is not None else None,
    }


def pool(db: sqlite3.Connection) -> dict[str, list[dict]]:
    """Direct growth plain index funds Groww lists, by tracked index, where that TRI is stored."""
    stored = {r[0] for r in db.execute("SELECT DISTINCT name FROM series_meta")}
    groww = {r["scheme_code"]: json.loads(r["meta"]).get("groww_expense_ratio")
             for r in db.execute("SELECT scheme_code, meta FROM fund WHERE plan=?", (Plan.DIRECT,))}
    out: dict[str, list[dict]] = {}
    for row in universe(db):
        name = row["scheme_name"]
        if row["plan"] != Plan.DIRECT or not row.get("search_id") or not is_plain_index(name) or is_payout(name) \
                or Plan.REGULAR in normalise_name(name):
            continue
        key = index_of(name)
        if key and series_name(key) in stored:
            out.setdefault(key, []).append({**row, "groww_expense_ratio": groww.get(row["scheme_code"])})
    return out


def document(db: sqlite3.Connection, as_of: date, min_years: float = MIN_YEARS) -> dict:
    """The `index_tracking` document, in the shape `/index-funds` reads."""
    ters, ter_month = ter_table(db)
    ter_row = db.execute("SELECT body FROM analysis WHERE kind='ter' AND slug=''").fetchone()
    ter_retrieved = (json.loads(ter_row["body"]).get("meta") or {}).get("retrieved_at") if ter_row else None
    funds_by_key = pool(db)
    indexes, excluded, counted, tri = {}, [], 0, {}
    for key, label in LABELS.items():
        rows = funds_by_key.get(key)
        if not rows:
            continue
        name = series_name(key)
        index = {d: v for d, v in benchmark_series(db, name).items() if d <= as_of}
        if not index:
            continue
        meta = json.loads(db.execute("SELECT meta FROM series_meta WHERE name=?", (name,)).fetchone()["meta"])
        funds = []
        for row in rows:
            code = row["scheme_code"]
            full = nav.load_db(code, db)
            series = {d: v for d, v in full.items() if d <= as_of} if full else None
            if not series:
                days = db.execute("SELECT COUNT(*) FROM nav WHERE scheme_code=? AND d<=?",
                                  (code, day(as_of))).fetchone()[0]
                excluded.append({"name": row["scheme_name"], "index": label,
                                 "excluded": f"only {days} NAV days so far" if days else "no NAV history on AMFI's portal"})
                continue
            if (as_of - min(series)).days < 365.25 * min_years:
                excluded.append({"name": row["scheme_name"], "index": label,
                                 "excluded": f"only {(as_of - min(series)).days / 365.25:.1f}y of history"})
                continue
            funds.append(evaluate(row, series, index, as_of, ters))
        if not funds:
            continue
        counted += len(funds)
        funds.sort(key=lambda f: (f["td"].get("3") if f["td"].get("3") is not None else -99,
                                  f["td"].get("1") if f["td"].get("1") is not None else -99), reverse=True)
        index_years = {}
        first = min(f["since"][:4] for f in funds)
        for y in range(int(first) + 1, as_of.year):
            r = ret(index, date(y - 1, 12, 31), date(y, 12, 31))
            if r is not None:
                index_years[str(y)] = rnd(r)
        td3 = [f["td"]["3"] for f in funds if f["td"].get("3") is not None]
        bers = [f["ber"] for f in funds if f["ber"] is not None]
        indexes[key] = {
            "label": label, "index_name": meta.get("scheme_name") or meta.get("index"), "series": name,
            "index_cagr": {str(h): rnd(nav.cagr(index, h, as_of)) for h in HORIZONS},
            "index_years": index_years,
            "count": len(funds), "median_td3": rnd(median(td3)) if td3 else None,
            "median_ber": rnd(median(bers)) if bers else None,
            "funds": funds,
        }
        tri[key] = (meta, index)
    sources = [{
        "name": "AMFI NAV history", "url": "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx",
        "what": f"daily NAVs of {counted} index-fund direct plans, straight from AMFI's portal",
        "retrieved": str(as_of),
    }, {
        "name": "AMFI expense ratios", "url": "https://www.amfiindia.com/ter-of-mf-schemes",
        "what": f"official direct-plan base expense ratios (BER) for {ter_month or 'the current month'}",
        "retrieved": (ter_retrieved or "")[:10] or None,
    }]
    for key, (meta, index) in tri.items():
        # From the rows: series_meta's span covers only the latest incremental fetch.
        sources.append({
            "name": f"{meta.get('index', LABELS[key])} Total Return Index (NSE Indices)",
            "url": "https://www.niftyindices.com/reports/historical-data",
            "what": f"daily total-return index values, {min(index)} to {max(index)} ({len(index)} days)",
            "retrieved": (meta.get("retrieved_at") or "")[:10] or None,
        })
    sources.append({
        "name": "Groww", "url": "https://groww.in/mutual-funds",
        "what": "scheme universe and identifiers, AUM; not used for any return",
        "retrieved": str(as_of),
    })
    return {"as_of": as_of.isoformat(), "min_years": min_years, "horizons": list(HORIZONS),
            "ter_month": ter_month, "indexes": indexes, "excluded": excluded, "sources": sources}


def default_as_of(db: sqlite3.Connection) -> date:
    """The Nifty 50 TRI's last day, capped at the 80th percentile of each scheme's newest NAV day.

    AMFI publishes in the evening, so in the morning the index is a day ahead of the funds.
    """
    n50 = db.execute("SELECT MAX(d) FROM series WHERE name='nifty-50'").fetchone()[0]
    lasts = sorted(r[0] for r in db.execute("SELECT MAX(d) FROM nav GROUP BY scheme_code") if r[0] is not None)
    if n50 is None or not lasts:
        raise LookupError("no Nifty 50 TRI or NAV rows; run scripts/refresh.py first")
    return undate(min(n50, lasts[int(len(lasts) * 0.8)]))


def write(db: sqlite3.Connection) -> str:
    """Write the `index_tracking` document as of `default_as_of`."""
    doc = document(db, default_as_of(db))
    for ix in doc["indexes"].values():
        logger.debug("%-22s %3d funds, median 3y tracking difference %s pp, median BER %s",
                     ix["label"], ix["count"], ix["median_td3"], ix["median_ber"])
        last = undate(db.execute("SELECT MAX(d) FROM series WHERE name=?", (ix["series"],)).fetchone()[0])
        # nav.nav_on looks back 12 days; past that the index returns are null.
        if (date.fromisoformat(doc["as_of"]) - last).days > 12:
            logger.warning("the %s TRI ends %s, more than 12 days before as_of; its index returns are "
                           "null and its tracking error is unreliable until it is fetched again",
                           ix["label"], last)
    n = sum(ix["count"] for ix in doc["indexes"].values())
    logger.debug("%d funds across %d indexes, %d excluded, as of %s",
                 n, len(doc["indexes"]), len(doc["excluded"]), doc["as_of"])
    with writing(db):
        db.execute("INSERT OR REPLACE INTO analysis (kind, slug, as_of, body) VALUES (?,?,?,?)",
                   (AnalysisKind.INDEX_TRACKING, "", doc["as_of"],
                    json.dumps(doc, ensure_ascii=False, separators=(",", ":"))))
    return "wrote analysis index_tracking"
