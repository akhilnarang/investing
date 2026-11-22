"""/portfolio: a Zerodha Coin tradebook audit that runs in the browser (static/portfolio.js).

The tradebook never reaches the server. These GET endpoints serve only public reference data: the
ISIN-to-fund map with category pools and benchmarks, one fund's NAVs, and one benchmark series.
Series are sent compact: first date, day deltas, values.
"""
import hashlib
import json
import re
import sqlite3
from datetime import date, timedelta
from itertools import pairwise

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...analysis import longterm, nav
from ...core.enums import AnalysisKind, Plan
from ...db import undate
from .. import errors
from ..pages import DB, STATIC, page
from ..queries import scheme_key

router = APIRouter()

# Overseas index funds are benchmarked to the index they track, in rupees. OVERSEAS_NOT excludes
# variants that track something else.
OVERSEAS_NOT = re.compile(r"top\s*50|q\s*-?\s*50|next\s*gen|equal\s*weight|low\s*vol|momentum|quality", re.IGNORECASE)
OVERSEAS = ((re.compile(r"nasdaq\s*-?\s*100", re.IGNORECASE), "nasdaq-100-tr-inr"),
            (re.compile(r"s\s*&\s*p\s*500|s\s*and\s*p\s*500|sp\s*500", re.IGNORECASE), "sp500-tr-inr"),
            (re.compile(r"us total|total us|total stock market", re.IGNORECASE), "us-total-market-tr-inr"))
# Option words, stripped so an IDCW plan finds its growth sibling. "Dividend Yield" is a strategy.
OPTION = re.compile(r"\b(growth|idcw|payout|reinvest\w*|option|bonus|dividend(?!\s*yield))\b", re.IGNORECASE)

ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}\d")

# A cache-busting hash for the script.
JS_VERSION = hashlib.sha256((STATIC / "portfolio.js").read_bytes()).hexdigest()[:12]

_MAP: list[dict] = []   # built once per process


def _compact(code: str, name: str, s: dict[date, float]) -> dict:
    days = sorted(s)
    return {"code": code, "name": name, "d0": days[0].isoformat(),
            "dd": [(b - a).days for a, b in pairwise(days)], "v": [round(s[d], 5) for d in days]}


def _bench_of(name: str, has: set[str]) -> str | None:
    if not OVERSEAS_NOT.search(name):
        for pat, stem in OVERSEAS:
            if pat.search(name) and stem in has:
                return stem
    ik = longterm.index_key(name)
    return ik.replace("_", "-") if ik and ik.replace("_", "-") in has else None


def build_map(db: sqlite3.Connection) -> dict:
    series = {r["name"]: json.loads(r["meta"]) for r in
              db.execute("SELECT name, meta FROM series_meta WHERE kind IN ('index', 'metal', 'cpi')")}
    has = set(series)

    categories: dict[str, dict] = {}
    pool: dict[int, str] = {}
    built = None
    for r in db.execute("SELECT slug, as_of, json_extract(body, '$.plan') AS plan, "
                        "json_extract(body, '$.sub_category') AS label, "
                        "json_extract(body, '$.benchmark.scheme_code') AS bench, "
                        "json_extract(body, '$.benchmark.name') AS bench_name, "
                        "json_extract(body, '$.funds[0].category') AS grp, "
                        "(SELECT json_group_array(json_extract(value, '$.scheme_code')) "
                        " FROM json_each(body, '$.funds')) AS codes "
                        "FROM analysis WHERE kind=? ORDER BY slug", (AnalysisKind.LONGTERM,)):
        key = r["slug"].removesuffix("-regular")
        c = categories.setdefault(key, {"label": r["label"], "group": r["grp"] or "", "bench": r["bench"],
                                        "bench_name": r["bench_name"], "pool": {}})
        codes = [int(x) for x in json.loads(r["codes"])]
        c["pool"][r["plan"]] = codes
        for code in codes:
            pool.setdefault(code, key)
        built = max(built or "", r["as_of"] or "")

    rows = db.execute("SELECT scheme_code, name, fund_house, category, plan, isin_growth, "
                      "json_extract(meta, '$.isin_div_reinvestment') AS isin_re FROM fund").fetchall()
    def fam(r) -> str:   # scheme key without plan or option words
        return scheme_key(OPTION.sub(" ", r["name"]), r["fund_house"])
    family = {fam(r): pool[r["scheme_code"]] for r in reversed(rows) if r["scheme_code"] in pool}
    direct = {(fam(r), longterm.is_payout(r["name"])): r["scheme_code"] for r in rows if r["plan"] == Plan.DIRECT}

    amfi: list[str] = []
    funds: dict[str, dict] = {}
    isin: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    for r in rows:
        code, name = r["scheme_code"], r["name"]
        if r["category"] not in amfi:
            amfi.append(r["category"])
        e: dict = {"n": name, "p": r["plan"], "o": "idcw" if longterm.is_payout(name) else "growth",
                   "a": amfi.index(r["category"])}
        key = pool.get(code) or family.get(fam(r))
        if key:
            e["c"] = key
        if code in pool:
            e["k"] = 1
        if r["plan"] == Plan.REGULAR:
            twin = direct.get((fam(r), longterm.is_payout(name)))
            if twin:
                e["d"] = str(twin)
        b = _bench_of(name, has)
        if b:
            e["b"] = b
        elif not key and (r["category"] or "").startswith(("Equity", "Other Scheme - Index")):
            e["b"] = "nifty-500"   # uncategorised equity scheme
        if longterm.PASSIVE.search(name) and not re.search(r"\bactive\b", name, re.IGNORECASE):
            e["pv"] = 1
        funds[str(code)] = e
        for i in {r["isin_growth"], r["isin_re"]}:
            if not i or not ISIN.fullmatch(i):
                continue   # AMFI uses "-" and other placeholders for a missing ISIN
            if i in isin and isin[i] != str(code):
                ambiguous.setdefault(i, [isin[i]]).append(str(code))
            isin[i] = str(code)
    for i in ambiguous:
        isin.pop(i, None)   # the page reports these as unvalued

    indexes = {name: {"n": m.get("scheme_name") or name,
                      "f": undate(db.execute("SELECT MIN(d) FROM series WHERE name=?", (name,)).fetchone()[0]).isoformat()}
               for name, m in series.items()}
    return {"built": built, "categories": categories, "amfi_categories": amfi, "funds": funds,
            "isin": isin, "ambiguous": ambiguous, "indexes": indexes}


@router.get("/portfolio", response_class=HTMLResponse, include_in_schema=False)
def portfolio_page(request: Request, db: sqlite3.Connection = DB) -> HTMLResponse:
    return page(request, db, "portfolio.html", "portfolio", js_version=JS_VERSION)


@router.get("/api/v1/portfolio/map", tags=["portfolio"])
def portfolio_map(db: sqlite3.Connection = DB) -> dict:
    """The public mapping from every ISIN to its fund, category pools and benchmarks."""
    if not _MAP:
        _MAP.append(build_map(db))
    return _MAP[0]


@router.get("/api/v1/portfolio/nav/{scheme_code}", tags=["portfolio"])
def portfolio_nav(scheme_code: int, db: sqlite3.Connection = DB) -> dict:
    """One fund's screened NAV history: `d0`, then day gaps `dd`, then values `v`."""
    s = nav.load_db(scheme_code, db)
    if not s:
        raise errors.not_found("NAV history for this scheme")
    name = db.execute("SELECT name FROM fund WHERE scheme_code=?", (scheme_code,)).fetchone()
    return _compact(str(scheme_code), name[0] if name else "", s)


@router.get("/api/v1/portfolio/index/{name}", tags=["portfolio"])
def portfolio_index(name: str, db: sqlite3.Connection = DB) -> dict:
    """One benchmark series in the compact form of /nav, with CPI interpolated to daily values."""
    meta = db.execute("SELECT kind, meta FROM series_meta WHERE name=? AND kind != 'fx'", (name,)).fetchone()
    if meta is None:
        raise errors.not_found("benchmark")
    s = {undate(d): v for d, v in db.execute("SELECT d, v FROM series WHERE name=? ORDER BY d", (name,))}
    if meta["kind"] == "cpi":
        days, monthly, s = sorted(s), s, {}
        for a, b in pairwise(days):
            span = (b - a).days
            for k in range(span):
                s[a + timedelta(days=k)] = monthly[a] + (monthly[b] - monthly[a]) * k / span
        s[days[-1]] = monthly[days[-1]]
    return _compact(name, json.loads(meta["meta"]).get("scheme_name") or name, s)

