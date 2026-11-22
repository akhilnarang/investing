"""Server-rendered HTML pages, built from the same queries as the JSON API.

/ipo/rhp and /ipo/allotment are routed from investing.ipo.
"""
import hashlib
import re
import sqlite3
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..analysis.attribution import attribution_slug, fund_facts
from ..core.enums import AnalysisKind, Plan
from ..db import get_db, undate
from ..nse import slb
from . import errors, ratelimit
from . import queries as q
from .views import currency as currency_view
from .views import sif as sif_view

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STATIC = Path(__file__).parent / "static"
# Static URLs carry a hash of everything under static/, so a changed file gets a new URL.
STATIC_VERSION = hashlib.sha256(b"".join(
    f.relative_to(STATIC).as_posix().encode() + f.read_bytes() for f in sorted(STATIC.rglob("*")) if f.is_file()
)).hexdigest()[:12]
TEMPLATES.env.globals["static_version"] = STATIC_VERSION

router = APIRouter(include_in_schema=False)

# The category sources/sif.py writes.
SIF_CATEGORY = "SIF"

# Analysis slugs of the regular plan end with this; the direct plan has no suffix.
REGULAR_SUFFIX = "-regular"

# The fetchers store a readable label in meta.scheme_name.
def _label(meta: dict) -> str:
    m = meta.get("meta") or {}
    return m.get("scheme_name") or m.get("index") or meta["name"]


_STATS_CACHE: list[dict] = []


def _stats(db: sqlite3.Connection) -> dict:
    """The footer and hub counts, computed once per process. Rough totals, so never refreshed."""
    if _STATS_CACHE:
        return _STATS_CACHE[0]

    def one(sql: str, *args: Any) -> int:
        return db.execute(sql, args).fetchone()[0]

    first = q.first_nav_day(db)
    stats = {
        "funds": f"{one('SELECT COUNT(*) FROM fund'):,}",
        "stocks": f"{one('SELECT COUNT(*) FROM company'):,}",
        "portfolios": f"{one('SELECT COUNT(*) FROM (SELECT 1 FROM holding GROUP BY scheme_code, d)'):,}",
        "sifs": f"{one('SELECT COUNT(*) FROM fund WHERE category=?', SIF_CATEGORY):,}",
        "first_nav": first.year if first is not None else "-",
    }
    _STATS_CACHE.append(stats)
    return stats


def page(request: Request, db: sqlite3.Connection, name: str, section: str, **ctx: Any) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request=request, name=name,
                                      context={"stats": _stats(db), "section": section, **ctx})


DB = Depends(get_db)


# ---------------------------------------------------------------- hub

@router.get("/", response_class=HTMLResponse)
def hub(request: Request, db: sqlite3.Connection = DB) -> HTMLResponse:
    return page(request, db, "hub.html", "")


# ---------------------------------------------------------------- mutual funds

@router.get("/mf", response_class=HTMLResponse)
def mf(request: Request, db: sqlite3.Connection = DB,
       q_: str | None = Query(None, alias="q", max_length=100),
       category: str | None = None,
       plan: Plan | None = None) -> HTMLResponse:
    # Lists show one row per scheme; ?plan= gives the flat per-plan listing.
    results: list[dict] = []
    grouped = plan is None
    if category:
        results = (q.schemes_in_category(db, category) if grouped
                   else q.funds_in_category(db, category, plan=plan, limit=q.MAX_ROW_LIMIT))
    elif q_ and len(q_.strip()) >= 2:
        results = (q.search_schemes(db, q_.strip()) if grouped
                   else q.search_funds(db, q_.strip(), plan=plan, limit=q.MAX_ROW_LIMIT))
    return page(request, db, "mf.html", "mf", q=q_ or category, results=results, grouped=grouped,
                plan=plan, category=category,
                categories=q.scheme_categories(db) if not (q_ or category) else [])


@router.get("/mf/{scheme_code}", response_class=HTMLResponse)
def fund_page(request: Request, scheme_code: int, db: sqlite3.Connection = DB,
              on: date | None = None) -> HTMLResponse:
    f = q.fund(db, scheme_code)
    if f is None:
        raise errors.not_found("fund")

    r = q.fund_returns(db, scheme_code)
    returns = None
    if r:
        # Only windows the fund is old enough to have.
        windows = [w for w in r["cagr"] if r["cagr"][w] is not None] or list(r["cagr"])[:3]
        returns = {"windows": windows, "cagr": r["cagr"], "as_of": r["as_of"],
                   "short": q.short_returns(db, [scheme_code])["funds"].get(str(scheme_code)) or {}}

    series = q.nav_series(db, scheme_code, limit=q.MAX_LIMIT)
    # Flag a last NAV well behind the newest NAV in the table (not the clock, so holidays do not count).
    newest = q.newest_nav_day(db)
    nav = None
    if series:
        last = date.fromisoformat(series[-1]["date"])
        behind = (newest - last).days if newest else 0
        nav = {"latest": series[-1]["nav"], "latest_date": series[-1]["date"],
               "first_date": series[0]["date"], "count": len(series),
               "stale_days": behind if behind > 30 else None}

    # Use the date q.portfolio reports, which differs from `on` when none was given.
    book = q.portfolio(db, scheme_code, on=on.isoformat() if on else None, limit=q.MAX_ROW_LIMIT)
    dates = q.portfolio_dates(db, scheme_code, limit=13)
    return page(request, db, "fund.html", "sif" if f.get("category") == SIF_CATEGORY else "mf",
                fund=f, returns=returns, nav=nav, portfolio=book["holdings"],
                portfolio_date=book["date"], sibling=q.sibling_plan(db, scheme_code),
                other_dates=[d for d in dates if d != book["date"]][:12])


# ---------------------------------------------------------------- stocks

@router.get("/stocks", response_class=HTMLResponse)
def stocks(request: Request, db: sqlite3.Connection = DB,
           q_: str | None = Query(None, alias="q", max_length=100)) -> HTMLResponse:
    results = q.search_stocks(db, q_.strip(), limit=q.MAX_ROW_LIMIT) if q_ and len(q_.strip()) >= 2 else []
    return page(request, db, "stocks.html", "stocks", q=q_, results=results)


@router.get("/stocks/{isin}", response_class=HTMLResponse)
def stock_page(request: Request, isin: str, db: sqlite3.Connection = DB,
               on: date | None = None,
               by: str = Query("value", pattern="^(value|weight)$")) -> HTMLResponse:
    s = q.stock(db, isin)
    if s is None:
        raise errors.not_found("stock")
    closes = q.stock_prices(db, isin, limit=q.MAX_LIMIT)
    prices = ({"latest": closes[-1]["close"], "latest_date": closes[-1]["date"],
               "first_date": closes[0]["date"], "count": len(closes)} if closes else None)

    ownership = q.isin_ownership(db, isin, limit=24)
    on_iso = on.isoformat() if on else (ownership[0]["date"] if ownership else None)
    holders = q.holders_of(db, isin, on=on_iso, limit=q.MAX_ROW_LIMIT) if on_iso else []
    # Values add up across funds; weights do not, so totals are in rupees and weight is per fund.
    holders.sort(key=lambda h: (h["value"] if by == "value" else h["weight"]) or 0, reverse=True)

    latest: dict[str, Any] = {}
    if holders:
        current = next((o for o in ownership if o["date"] == on_iso), None)
        # A few funds disclose fortnightly, so some dates hold only a partial set. Compare only
        # against a date with at least half as many funds.
        previous = next((o for o in ownership
                         if o["date"] < (on_iso or "") and current
                         and o["funds"] >= current["funds"] * 0.5), None)
        latest = {
            "funds": len(holders),
            "value_cr": sum(h["value"] or 0 for h in holders) / 10,   # Kuvera reports millions
            "top": max(holders, key=lambda h: h["weight"] or 0),
            "previous": previous["date"] if previous else None,
            "change": (current["funds"] - previous["funds"]) if current and previous else None,
        }

    by_house: dict[str, dict] = {}
    for h in holders:
        row = by_house.setdefault(h["fund_house"] or "",
                                  {"fund_house": h["fund_house"], "funds": 0, "value_cr": 0.0})
        row["funds"] += 1
        row["value_cr"] += (h["value"] or 0) / 10

    return page(request, db, "stock.html", "stocks", stock=s, prices=prices,
                actions=q.corporate_actions(db, isin, limit=50), ownership=ownership,
                holders=holders, on=on_iso, by=by, latest=latest,
                houses=sorted(by_house.values(), key=lambda r: r["value_cr"], reverse=True)[:12])


# ---------------------------------------------------------------- slb

@router.get("/slb", response_class=HTMLResponse)
def slb_page(request: Request, db: sqlite3.Connection = DB,
             q_: str | None = Query(None, alias="q", max_length=30),
             rate: float = Query(0.07, ge=0, le=0.5)) -> HTMLResponse:
    # Each search is a live NSE call. The symbol is validated before it goes into NSE's URL.
    symbol = (q_ or "").strip().upper()
    data, error, wait = None, None, None
    if symbol and not re.fullmatch(r"[A-Z0-9&-]{1,20}", symbol):
        error = "A ticker is letters, digits, & or -, like RELIANCE or M&M."
    elif symbol:
        wait = ratelimit.retry_after(ratelimit.NSE, request)
        if wait is not None:
            error = f"Too many live lookups from this address. Try again in {wait} seconds."
        else:
            try:
                data = slb.quote(symbol, rate)
            except (OSError, ValueError, KeyError) as e:
                error = f"NSE did not answer usefully ({type(e).__name__}). Try again in a minute."
    response = page(request, db, "slb.html", "slb", q=symbol, rate=rate, data=data, error=error)
    if wait is not None:
        response.status_code = 429
        response.headers["Retry-After"] = str(wait)
    return response


# ---------------------------------------------------------------- sif

@router.get("/sif", response_class=HTMLResponse)
def sif(request: Request, db: sqlite3.Connection = DB, fund: int | None = None) -> HTMLResponse:
    t = sif_view.tracker(db)
    d = sif_view.detail(db, fund, t["funds"]) if fund is not None else None
    if fund is not None and d is None:
        raise errors.not_found("SIF")
    return page(request, db, "sif.html", "sif", t=t, d=d,
                spark=_sparkline(d["points"]) if d else None)


# ---------------------------------------------------------------- currency, metals, inflation

GROUPS = {"fx": "Currencies", "metal": "Metals", "cpi": "Inflation", "index": "Benchmarks"}


@router.get("/currency", response_class=HTMLResponse)
def currency(request: Request, db: sqlite3.Connection = DB,
             amount: float = Query(100_000, ge=1, le=1e12),
             month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
             ccy: str | None = Query(None, max_length=3)) -> HTMLResponse:
    groups: dict[str, list[dict]] = {name: [] for name in GROUPS.values()}
    for meta in q.series_list(db):
        last = db.execute("SELECT d, v FROM series WHERE name=? ORDER BY d DESC LIMIT 1",
                          (meta["name"],)).fetchone()
        count = db.execute("SELECT COUNT(*) FROM series WHERE name=?", (meta["name"],)).fetchone()[0]
        groups[GROUPS.get(meta["kind"], "Benchmarks")].append({
            "name": meta["name"], "label": _label(meta), "count": count,
            "latest": last["v"] if last else None,
            "latest_date": undate(last["d"]).isoformat() if last else None})
    return page(request, db, "currency.html", "currency", groups=groups,
                fx=currency_view.view(db, amount, month, ccy), inr=currency_view.inr)


@router.get("/currency/{name}", response_class=HTMLResponse)
def series_page(request: Request, name: str, db: sqlite3.Connection = DB) -> HTMLResponse:
    meta = q.series_meta(db, name)
    if meta is None:
        raise errors.not_found("series")
    points = [{"date": p["date"], "value": p["value"]}
              for p in q.series_points(db, name, limit=q.MAX_LIMIT)]
    m = meta.get("meta") or {}
    # CPI is annual upstream, spread into monthly levels.
    note = ("Annual inflation rates compounded into monthly index levels, so month-level precision "
            "is not real." if meta["kind"] == "cpi" else m.get("note"))
    change = None
    if len(points) > 260:                       # roughly a year of trading days
        year_ago = points[-261]["value"]
        if year_ago:
            change = (points[-1]["value"] / year_ago - 1) * 100

    return page(request, db, "series.html", "currency",
                meta={"label": _label(meta), "description": m.get("description")},
                points=points, count=len(points), note=note, change=change,
                spark=_sparkline(points), recent=list(reversed(points[-40:])))


def _sparkline(points: list[dict], width: int = 700, height: int = 140) -> dict | None:
    """An SVG polyline sampled to about one point per pixel column.

    The low and high come from the full series, not the sample."""
    if len(points) < 2:
        return None
    step = max(1, len(points) // width)
    sample = points[::step]
    if sample[-1] is not points[-1]:
        sample = [*sample, points[-1]]
    lo = min(p["value"] for p in points)
    hi = max(p["value"] for p in points)
    span = (hi - lo) or 1
    coords = " ".join(
        f"{i * width / (len(sample) - 1):.1f},{height - (p['value'] - lo) / span * (height - 4) - 2:.1f}"
        for i, p in enumerate(sample))
    return {"points": coords, "lo": lo, "hi": hi}


# ---------------------------------------------------------------- category comparisons

@router.get("/compare", response_class=HTMLResponse)
def compare(request: Request, db: sqlite3.Connection = DB) -> HTMLResponse:
    # Documents are per plan ('flexi-cap', 'flexi-cap-regular'). One row per category, direct
    # numbers, with the regular plan as a link.
    rows: dict[str, dict] = {}
    as_of = None
    for entry in q.analysis_slugs(db, AnalysisKind.LONGTERM):
        doc = q.analysis(db, AnalysisKind.LONGTERM, entry["slug"])
        if not doc or not doc.get("funds"):
            continue
        as_of = as_of or doc.get("as_of")
        plan = doc.get("plan") or Plan.DIRECT
        label = doc.get("sub_category") or entry["slug"]
        row = rows.setdefault(label, {"label": label, "plans": {}, "funds": 0, "benchmark": None})
        row["plans"][plan] = entry["slug"]
        if plan == Plan.DIRECT or not row["benchmark"]:
            row["funds"] = len(doc["funds"])
            row["benchmark"] = doc.get("benchmark")
        if plan == Plan.DIRECT:
            # Median active share and stock selection over the active funds.
            codes = {str(f.get("direct_code") or f["scheme_code"]) for f in doc["funds"] if not f.get("passive")}
            act = q.analysis(db, AnalysisKind.ACTIVE_SHARE, entry["slug"]) or {}
            attr = q.analysis(db, AnalysisKind.ATTRIBUTION, attribution_slug(entry["slug"], Plan.DIRECT)) or {}
            shares = [v["active_share_pct"] for k, v in (act.get("funds") or {}).items() if k in codes]
            picks = [v["selection_pp"] for k, v in (attr.get("funds") or {}).items() if k in codes]
            row["active_share"] = median(shares) if shares else None
            row["selection"] = median(picks) if picks else None
    for row in rows.values():
        row["slug"] = row["plans"].get(Plan.DIRECT) or next(iter(row["plans"].values()))
    return page(request, db, "compare.html", "compare",
                categories=sorted(rows.values(), key=lambda r: r["label"]), as_of=as_of)


@router.get("/compare/{slug}", response_class=HTMLResponse)
def category(request: Request, slug: str, db: sqlite3.Connection = DB) -> HTMLResponse:
    doc = q.analysis(db, AnalysisKind.LONGTERM, slug)
    if doc is None:
        raise errors.not_found("category")
    # Horizons some fund has a number for. The cagr dicts are keyed by the horizon as a string.
    horizons = [h for h in (doc.get("horizons") or [])
                if any((f.get("cagr") or {}).get(str(h)) is not None for f in doc.get("funds") or [])]
    regular = slug.endswith(REGULAR_SUFFIX)
    other = slug.removesuffix(REGULAR_SUFFIX) if regular else f"{slug}{REGULAR_SUFFIX}"
    # Active share always comes from the direct document (same portfolio). Attribution is per plan,
    # since the regular run covers a longer window. All are keyed by the direct code.
    plan = Plan.REGULAR if regular else Plan.DIRECT
    base = other if regular else slug
    codes = [str(f.get("direct_code") or f["scheme_code"]) for f in doc.get("funds") or []]
    return page(request, db, "category.html", "compare", doc=doc, horizons=horizons,
                other_slug=other if q.analysis(db, AnalysisKind.LONGTERM, other) else None,
                other_plan=Plan.DIRECT if regular else Plan.REGULAR,
                active=q.analysis(db, AnalysisKind.ACTIVE_SHARE, base),
                attribution=q.analysis(db, AnalysisKind.ATTRIBUTION, attribution_slug(base, plan)),
                facts=fund_facts(db, codes, doc.get("as_of")),
                short=q.short_returns(db, [f["scheme_code"] for f in doc.get("funds") or []]))


# ---------------------------------------------------------------- index funds

@router.get("/index-funds", response_class=HTMLResponse)
def index_funds(request: Request, db: sqlite3.Connection = DB) -> HTMLResponse:
    doc = q.analysis(db, AnalysisKind.INDEX_TRACKING)
    if doc is None:
        raise errors.not_found("index tracking document")
    indexes = sorted((doc.get("indexes") or {}).items(), key=lambda kv: -(kv[1].get("count") or 0))
    # Horizons some fund has a number for, keyed as strings.
    horizons = [h for h in (doc.get("horizons") or [])
                if any((f.get("cagr") or {}).get(str(h)) is not None
                       for _, ix in indexes for f in ix.get("funds") or [])]
    return page(request, db, "index_funds.html", "index-funds",
                doc=doc, indexes=indexes, horizons=horizons)


# ---------------------------------------------------------------- ipo

@router.get("/ipo", response_class=HTMLResponse)
def ipo(request: Request, db: sqlite3.Connection = DB) -> HTMLResponse:
    linked = []
    try:
        linked = [dict(r) for r in db.execute(
            "SELECT company_name, isin FROM analysis_jobs WHERE isin IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 25")]
    except sqlite3.OperationalError:
        pass  # the prospectus tables are optional
    return page(request, db, "ipo.html", "ipo", linked=linked)
