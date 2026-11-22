"""Read queries over a connection, returning plain dicts with ISO date strings.

Row-returning functions cap themselves with limit/offset or a date range. Queries marked SCAN read a
small table; nothing here scans `nav`, `stock_price`, `series` or `holding`.
"""
import json
import re
import sqlite3
from datetime import date, timedelta

from ..analysis import nav
from ..core.enums import Plan
from ..db import day, undate

# Large enough for one entity's whole series.
MAX_LIMIT = 20_000
DEFAULT_LIMIT = 1_000
MAX_ROW_LIMIT = 500
DEFAULT_ROW_LIMIT = 100
# Grouped lists are cut after grouping, so the cap counts schemes, not plan rows.
MAX_SCHEME_ROWS = 2_000
# Plan rows a scheme search reads before grouping.
SCHEME_SCAN_ROWS = 5_000


def _clamp(limit: int | None, default: int, cap: int) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), cap))


def _as_day(d: date | str | None) -> int | None:
    if d is None:
        return None
    return day(d if isinstance(d, date) else date.fromisoformat(d))


def _window(start: date | str | None, end: date | str | None) -> tuple[str, list[int]]:
    """SQL bounds on `d` for whichever of start and end are given."""
    sql, args = "", []
    if (lo := _as_day(start)) is not None:
        sql += " AND d>=?"
        args.append(lo)
    if (hi := _as_day(end)) is not None:
        sql += " AND d<=?"
        args.append(hi)
    return sql, args


# ---------------------------------------------------------------- funds

FUND_COLS = "scheme_code, name, isin_growth, fund_house, category, plan"


def _fund(row: sqlite3.Row) -> dict:
    return dict(row)


def fund(db: sqlite3.Connection, scheme_code: int) -> dict | None:
    row = db.execute(f"SELECT {FUND_COLS}, meta FROM fund WHERE scheme_code=?", (int(scheme_code),)).fetchone()
    if row is None:
        return None
    return dict(row) | {"meta": json.loads(row["meta"])}


def funds_by_isin(db: sqlite3.Connection, isin: str) -> list[dict]:
    """Every plan sharing a growth ISIN. Seeks fund_isin_idx."""
    return [_fund(r) for r in db.execute(f"SELECT {FUND_COLS} FROM fund WHERE isin_growth=? ORDER BY scheme_code",
                                         (isin,))]


def _search_rows(db: sqlite3.Connection, q: str, plan: str | None, rows: int, offset: int) -> list[dict]:
    sql = (f"SELECT {FUND_COLS}, (name LIKE ? ESCAPE '\\') AS by_name FROM fund "
           "WHERE (name LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\')")
    like = f"%{q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
    args: list = [like, like, like]
    if plan:
        sql += " AND plan=?"
        args.append(plan)
    sql += " ORDER BY by_name DESC, name, scheme_code LIMIT ? OFFSET ?"
    args += [rows, max(0, offset)]
    return [_fund(r) for r in db.execute(sql, args)]


def search_funds(db: sqlite3.Connection, q: str, *, plan: str | None = None,
                 limit: int | None = None, offset: int = 0) -> list[dict]:
    """Substring match on the scheme name or category, name matches first.

    The category match lets "arbitrage" or "gilt" find funds whose names omit it. SCAN.
    """
    return _search_rows(db, q, plan, _clamp(limit, DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT), offset)


def funds_in_category(db: sqlite3.Connection, category: str, *, plan: str | None = None,
                      limit: int | None = None, offset: int = 0) -> list[dict]:
    sql = f"SELECT {FUND_COLS} FROM fund WHERE category=?"
    args: list = [category]
    if plan:
        sql += " AND plan=?"
        args.append(plan)
    sql += " ORDER BY name, scheme_code LIMIT ? OFFSET ?"
    args += [_clamp(limit, DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT), max(0, offset)]
    return [_fund(r) for r in db.execute(sql, args)]


def categories(db: sqlite3.Connection) -> list[dict]:
    """SCAN of the fund table."""
    return [dict(r) for r in db.execute(
        "SELECT category, plan, COUNT(*) AS funds FROM fund WHERE category IS NOT NULL "
        "GROUP BY category, plan ORDER BY category, plan")]


def newest_nav_day(db: sqlite3.Connection) -> date | None:
    """The newest NAV date. One MAX seek per fund; a bare MAX(d) scans the whole table."""
    row = db.execute("SELECT MAX((SELECT MAX(d) FROM nav WHERE scheme_code=f.scheme_code)) FROM fund f").fetchone()
    return None if row is None or row[0] is None else undate(row[0])


def first_nav_day(db: sqlite3.Connection) -> date | None:
    """The oldest NAV date. One MIN seek per fund, as in newest_nav_day."""
    row = db.execute("SELECT MIN((SELECT MIN(d) FROM nav WHERE scheme_code=f.scheme_code)) FROM fund f").fetchone()
    return None if row is None or row[0] is None else undate(row[0])


# ---------------------------------------------------------------- NAV

def nav_series(db: sqlite3.Connection, scheme_code: int, *, start=None, end=None,
               limit: int | None = None, offset: int = 0, screen: bool = True) -> list[dict]:
    """A fund's NAVs, oldest first, as [{"date": "YYYY-MM-DD", "nav": float}].

    `screen=True` applies nav.load_db's repairs to the whole series and then windows it, since a
    repair can depend on points outside the window. `screen=False` returns NAVs as AMFI published them.
    """
    n = _clamp(limit, DEFAULT_LIMIT, MAX_LIMIT)
    if not screen:
        where, args = _window(start, end)
        rows = db.execute(f"SELECT d, nav FROM nav WHERE scheme_code=?{where} ORDER BY d LIMIT ? OFFSET ?",
                          [int(scheme_code), *args, n, max(0, offset)])
        return [{"date": undate(r["d"]).isoformat(), "nav": r["nav"]} for r in rows]

    s = nav.load_db(scheme_code, db)
    if not s:
        return []
    lo, hi = _as_day(start), _as_day(end)
    days = [d for d in sorted(s) if (lo is None or day(d) >= lo) and (hi is None or day(d) <= hi)]
    return [{"date": d.isoformat(), "nav": s[d]} for d in days[max(0, offset):max(0, offset) + n]]


# Trailing windows in years.
WINDOWS = (0.5, 1.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0)


def fund_returns(db: sqlite3.Connection, scheme_code: int, *, as_of: date | str | None = None) -> dict | None:
    """Trailing CAGR over WINDOWS, plus inception and the latest NAV.

    None when screening leaves no usable history.
    """
    notes: list[str] = []
    s = nav.load_db(scheme_code, db, notes=notes)
    if not s:
        return None
    latest = max(s)
    ref = latest if as_of is None else (as_of if isinstance(as_of, date) else date.fromisoformat(as_of))
    start = nav.inception(s)
    years = (ref - start).days / 365.25
    return {
        "scheme_code": int(scheme_code),
        "as_of": ref.isoformat(),
        "inception": start.isoformat(),
        "latest_nav": s[latest],
        "latest_date": latest.isoformat(),
        "points": len(s),
        # None for windows longer than the fund's history.
        "cagr": {_window_key(y): nav.cagr(s, y, ref) for y in WINDOWS},
        "since_inception_cagr": nav.cagr(s, years, ref) if years >= 1 else None,
        "notes": notes,
    }


def _window_key(y: float) -> str:
    return f"{int(y * 12)}m" if y < 1 else f"{int(y)}y"


# Absolute returns, not annualised.
SHORT = {"1w": 7, "1m": 30, "3m": 91}


def short_returns(db: sqlite3.Connection, codes: list[int | str]) -> dict:
    """1W, 1M and 3M absolute return per fund, and each fund's rank among `codes`.

    All funds end on the same day, so a fund that has not published that day's NAV is blank rather
    than ranked over a different window. The start may fall up to four days early for holidays.
    """
    series = {str(c): nav.load_db(c, db) for c in codes}
    loaded = [s for s in series.values() if s]
    if not loaded:
        return {"as_of": None, "funds": {}, "counts": {}}
    # The newest day at least half the funds have a NAV for.
    as_of = next(d for d in sorted({max(s) for s in loaded}, reverse=True)
                 if 2 * sum(d in s for s in loaded) >= len(loaded))
    funds: dict[str, dict] = {}
    for code, s in series.items():
        now = s.get(as_of) if s else None
        row = {}
        for key, days in SHORT.items():
            then = nav.nav_on(s, as_of - timedelta(days=days), back=4) if s and now else None
            row[key] = (now / then - 1) * 100 if now and then else None
        funds[code] = row
    counts = {}
    for key in SHORT:
        ranked = sorted((r[key], c) for c, r in funds.items() if r[key] is not None)[::-1]
        counts[key] = len(ranked)
        for i, (_, c) in enumerate(ranked, 1):
            funds[c][f"{key}_rank"] = i
    return {"as_of": as_of.isoformat(), "funds": funds, "counts": counts}


# ---------------------------------------------------------------- stocks

def stock(db: sqlite3.Connection, isin: str) -> dict | None:
    row = db.execute("SELECT isin, symbol, name, slug FROM company WHERE isin=?", (isin,)).fetchone()
    return dict(row) if row else None


def stock_by_symbol(db: sqlite3.Connection, symbol: str) -> list[dict]:
    """A list because a symbol can map to more than one ISIN over time."""
    return [dict(r) for r in db.execute(
        "SELECT isin, symbol, name, slug FROM company WHERE symbol=? ORDER BY isin", (symbol.upper(),))]


def search_stocks(db: sqlite3.Connection, q: str, *, limit: int | None = None, offset: int = 0) -> list[dict]:
    """SCAN of the company table. Symbol matches sort first."""
    pat = f"%{q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
    return [dict(r) for r in db.execute(
        "SELECT isin, symbol, name, slug FROM company WHERE symbol LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
        "ORDER BY (symbol LIKE ? ESCAPE '\\') DESC, name, isin LIMIT ? OFFSET ?",
        (pat, pat, pat, _clamp(limit, DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT), max(0, offset)))]


def stock_prices(db: sqlite3.Connection, isin: str, *, start=None, end=None,
                 limit: int | None = None, offset: int = 0) -> list[dict]:
    where, args = _window(start, end)
    rows = db.execute(f"SELECT d, close FROM stock_price WHERE isin=?{where} ORDER BY d LIMIT ? OFFSET ?",
                      [isin, *args, _clamp(limit, DEFAULT_LIMIT, MAX_LIMIT), max(0, offset)])
    return [{"date": undate(r["d"]).isoformat(), "close": r["close"]} for r in rows]


def corporate_actions(db: sqlite3.Connection, isin: str, *, start=None, end=None,
                      limit: int | None = None, offset: int = 0) -> list[dict]:
    where, args = _window(start, end)
    rows = db.execute(f"SELECT d, kind, ratio, amount FROM corporate_action WHERE isin=?{where} "
                      "ORDER BY d DESC, kind LIMIT ? OFFSET ?",
                      [isin, *args, _clamp(limit, DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT), max(0, offset)])
    return [{"date": undate(r["d"]).isoformat(), "kind": r["kind"], "ratio": r["ratio"], "amount": r["amount"]}
            for r in rows]


# ---------------------------------------------------------------- named series

def series_list(db: sqlite3.Connection, kind: str | None = None) -> list[dict]:
    sql = "SELECT name, kind, meta FROM series_meta"
    args: list = []
    if kind:
        sql += " WHERE kind=?"
        args.append(kind)
    return [{"name": r["name"], "kind": r["kind"], "meta": json.loads(r["meta"])}
            for r in db.execute(sql + " ORDER BY name", args)]


def series_meta(db: sqlite3.Connection, name: str) -> dict | None:
    row = db.execute("SELECT name, kind, meta FROM series_meta WHERE name=?", (name,)).fetchone()
    if row is None:
        return None
    return {"name": row["name"], "kind": row["kind"], "meta": json.loads(row["meta"])}


def series_points(db: sqlite3.Connection, name: str, *, start=None, end=None,
                  limit: int | None = None, offset: int = 0) -> list[dict]:
    where, args = _window(start, end)
    rows = db.execute(f"SELECT d, v FROM series WHERE name=?{where} ORDER BY d LIMIT ? OFFSET ?",
                      [name, *args, _clamp(limit, DEFAULT_LIMIT, MAX_LIMIT), max(0, offset)])
    return [{"date": undate(r["d"]).isoformat(), "value": r["v"]} for r in rows]


# ---------------------------------------------------------------- holdings

HOLDING_COLS = ("h.name AS security_name, h.isin, h.company, h.weight, h.value, h.quantity, "
                "h.sector, h.asset_class, h.side")


def _holding(r: sqlite3.Row) -> dict:
    d = dict(r)
    if "d" in d:
        d["date"] = undate(d.pop("d")).isoformat()
    return d


def holders_of(db: sqlite3.Connection, isin: str, *, on: date | str | None = None,
               start=None, end=None, limit: int | None = None, offset: int = 0) -> list[dict]:
    """Which funds held this ISIN, and when, with weights, newest first. Seeks holding_isin_idx."""
    where, args = ("", [])
    if on is not None:
        where, args = " AND h.d=?", [_as_day(on)]
    else:
        if (lo := _as_day(start)) is not None:
            where += " AND h.d>=?"
            args.append(lo)
        if (hi := _as_day(end)) is not None:
            where += " AND h.d<=?"
            args.append(hi)
    rows = db.execute(
        "SELECT h.d, h.scheme_code, f.name AS fund_name, f.fund_house, f.category, f.plan, "
        "h.name AS security_name, h.weight, h.value, h.quantity, h.sector, h.side "
        f"FROM holding h LEFT JOIN fund f ON f.scheme_code=h.scheme_code WHERE h.isin=?{where} "
        "ORDER BY h.d DESC, h.weight DESC, h.scheme_code, h.name, h.side_key LIMIT ? OFFSET ?",
        [isin, *args, _clamp(limit, DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT), max(0, offset)])
    return [_holding(r) for r in rows]


def holding_dates_for_isin(db: sqlite3.Connection, isin: str, *, limit: int | None = None) -> list[str]:
    """The disclosure dates this ISIN appears on."""
    return [undate(r[0]).isoformat() for r in db.execute(
        "SELECT DISTINCT d FROM holding WHERE isin=? ORDER BY d DESC LIMIT ?",
        (isin, _clamp(limit, DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT)))]


def isin_ownership(db: sqlite3.Connection, isin: str, *, limit: int | None = None) -> list[dict]:
    """Total disclosed weight and holder count per date. Reads only this ISIN's index range."""
    return [{"date": undate(r["d"]).isoformat(), "funds": r["funds"],
             "total_weight": r["total_weight"], "total_value": r["total_value"]}
            for r in db.execute(
                "SELECT d, COUNT(*) AS funds, SUM(weight) AS total_weight, SUM(value) AS total_value "
                "FROM holding WHERE isin=? GROUP BY d ORDER BY d DESC LIMIT ?",
                (isin, _clamp(limit, DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT)))]


def portfolio(db: sqlite3.Connection, scheme_code: int, *, on: date | str | None = None,
              limit: int | None = None, offset: int = 0) -> dict:
    """A fund's disclosed portfolio on a date, heaviest first.

    With no date, uses the newest disclosure. The date used is returned.
    """
    d = _as_day(on)
    if d is None:
        row = db.execute("SELECT MAX(d) FROM holding WHERE scheme_code=?", (int(scheme_code),)).fetchone()
        d = row[0] if row else None
    if d is None:
        return {"scheme_code": int(scheme_code), "date": None, "holdings": []}
    rows = db.execute(
        f"SELECT {HOLDING_COLS}, c.symbol FROM holding h LEFT JOIN company c ON c.isin=h.isin "
        "WHERE h.scheme_code=? AND h.d=? "
        "ORDER BY h.weight DESC NULLS LAST, h.name, h.isin_key, h.side_key LIMIT ? OFFSET ?",
        (int(scheme_code), d, _clamp(limit, DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT), max(0, offset)))
    return {"scheme_code": int(scheme_code), "date": undate(d).isoformat(), "holdings": [dict(r) for r in rows]}


def portfolio_dates(db: sqlite3.Connection, scheme_code: int, *, limit: int | None = None) -> list[str]:
    """The dates this fund has disclosed a portfolio on."""
    return [undate(r[0]).isoformat() for r in db.execute(
        "SELECT DISTINCT d FROM holding WHERE scheme_code=? ORDER BY d DESC LIMIT ?",
        (int(scheme_code), _clamp(limit, DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT)))]


def position_history(db: sqlite3.Connection, scheme_code: int, isin: str,
                     *, limit: int | None = None) -> list[dict]:
    """One fund's weight in one security over time."""
    return [{"date": undate(r["d"]).isoformat(), "security_name": r["name"], "weight": r["weight"],
             "value": r["value"], "quantity": r["quantity"], "side": r["side"]}
            for r in db.execute(
                "SELECT d, name, weight, value, quantity, side FROM holding WHERE isin=? AND scheme_code=? "
                "ORDER BY d DESC, name, side_key LIMIT ?",
                (isin, int(scheme_code), _clamp(limit, DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT)))]


def overlap(db: sqlite3.Connection, a: int, b: int, *, on: date | str | None = None,
            limit: int | None = None) -> dict:
    """Common holdings of two funds on their latest shared date, with both weights."""
    d = _as_day(on)
    if d is None:
        row = db.execute(
            "SELECT MAX(d) FROM holding WHERE scheme_code=? AND d IN "
            "(SELECT d FROM holding WHERE scheme_code=?)", (int(a), int(b))).fetchone()
        d = row[0] if row else None
    if d is None:
        return {"funds": [int(a), int(b)], "date": None, "common_weight": 0.0, "holdings": []}
    join = ("FROM holding ha JOIN holding hb ON hb.isin=ha.isin AND hb.scheme_code=? AND hb.d=ha.d "
            "WHERE ha.scheme_code=? AND ha.d=? AND ha.isin IS NOT NULL")
    args = (int(b), int(a), d)
    # Summed over all common holdings, not just the returned page.
    common = db.execute(
        f"SELECT COALESCE(SUM(MIN(COALESCE(ha.weight, 0), COALESCE(hb.weight, 0))), 0) {join}", args).fetchone()[0]
    rows = db.execute(
        f"SELECT ha.isin, ha.name, ha.weight AS weight_a, hb.weight AS weight_b {join} "
        "ORDER BY MIN(ha.weight, hb.weight) DESC, ha.isin, ha.name LIMIT ?",
        (*args, _clamp(limit, DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT)))
    return {"funds": [int(a), int(b)], "date": undate(d).isoformat(),
            "common_weight": common, "holdings": [dict(r) for r in rows]}


# ---------------------------------------------------------------- derived analysis

def analysis(db: sqlite3.Connection, kind: str, slug: str = "") -> dict | None:
    """One analysis document, parsed."""
    row = db.execute("SELECT as_of, body FROM analysis WHERE kind=? AND slug=?", (kind, slug)).fetchone()
    return None if row is None else {"as_of": row["as_of"], **json.loads(row["body"])}


def analysis_slugs(db: sqlite3.Connection, kind: str) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT slug, as_of FROM analysis WHERE kind=? ORDER BY slug", (kind,))]


# ---------------------------------------------------------------- plans

# Direct and Regular plans of a scheme share a portfolio but have separate codes and ISINs. Lists
# group them into one row per scheme.
_PLAN_WORD = re.compile(r"\b(direct|regular)\b", re.I)


def scheme_key(name: str, fund_house: str | None = None) -> str:
    """The key a Direct and a Regular plan of one scheme share: the name minus the plan word.

    Bracketed text and option words (Growth, IDCW) stay in the key. Some LIC ULIS plans differ only
    inside brackets.
    """
    v = (name or "").lower().replace("&", "and")
    return f"{fund_house or ''}|{re.sub(r'[^a-z0-9]+', '', _PLAN_WORD.sub(' ', v))}"


def _group_plans(funds: list[dict]) -> list[dict]:
    """Collapse fund rows to one row per scheme, showing the direct plan when there is one."""
    groups: dict[str, dict] = {}
    for f in funds:
        g = groups.setdefault(scheme_key(f["name"], f.get("fund_house")),
                              {"plans": {}, "name": None, "fund_house": f.get("fund_house"),
                               "category": f.get("category")})
        g["plans"].setdefault(f.get("plan") or Plan.REGULAR, f)
    out = []
    for g in groups.values():
        shown = g["plans"].get(Plan.DIRECT) or next(iter(g["plans"].values()))
        out.append(g | {"scheme_code": shown["scheme_code"], "name": plan_free_name(shown["name"]),
                        "isin_growth": shown.get("isin_growth"), "shown_plan": shown.get("plan"),
                        "plans": {p: f["scheme_code"] for p, f in sorted(g["plans"].items())}})
    return sorted(out, key=lambda r: r["name"])


def plan_free_name(name: str) -> str:
    """'HDFC Flexi Cap Fund - Direct Plan - Growth' -> 'HDFC Flexi Cap Fund - Growth'."""
    v = _PLAN_WORD.sub(" ", name or "")
    v = re.sub(r"\bplan\b", " ", v, flags=re.I)
    # Collapse the separator runs left behind, e.g. "Fund -  - Growth".
    v = re.sub(r"\s*[-\u2013]\s*(?=[-\u2013])", "", v)
    v = re.sub(r"\s*[-\u2013]\s*", " - ", v)
    return re.sub(r"\s{2,}", " ", v).strip(" -\u2013")


def schemes_in_category(db: sqlite3.Connection, category: str, *, limit: int | None = None) -> list[dict]:
    """funds_in_category, one row per scheme."""
    rows = [_fund(r) for r in db.execute(
        f"SELECT {FUND_COLS} FROM fund WHERE category=? ORDER BY name, scheme_code", (category,))]
    return _group_plans(rows)[:_clamp(limit, MAX_SCHEME_ROWS, MAX_SCHEME_ROWS)]


def search_schemes(db: sqlite3.Connection, q: str, *, limit: int | None = None) -> list[dict]:
    """search_funds, one row per scheme."""
    return _group_plans(_search_rows(db, q, None, SCHEME_SCAN_ROWS, 0))[:_clamp(limit, MAX_ROW_LIMIT, MAX_ROW_LIMIT)]


def scheme_categories(db: sqlite3.Connection) -> list[dict]:
    """One row per category with a count of schemes, not plans."""
    by_cat: dict[str, dict] = {}
    for r in db.execute("SELECT category, name, fund_house, plan FROM fund WHERE category IS NOT NULL"):
        c = by_cat.setdefault(r["category"], {"category": r["category"], "keys": set(), "plans": set()})
        c["keys"].add(scheme_key(r["name"], r["fund_house"]))
        c["plans"].add(r["plan"])
    return sorted(({"category": c["category"], "schemes": len(c["keys"]),
                    "plans": sorted(p for p in c["plans"] if p)} for c in by_cat.values()),
                  key=lambda c: c["category"])


def sibling_plan(db: sqlite3.Connection, scheme_code: int) -> dict | None:
    """The other plan of this scheme, or None. Matched by scheme_key, since ISINs differ per plan."""
    f = db.execute("SELECT scheme_code, name, fund_house, category, plan FROM fund WHERE scheme_code=?",
                   (int(scheme_code),)).fetchone()
    if f is None:
        return None
    key = scheme_key(f["name"], f["fund_house"])
    for r in db.execute("SELECT scheme_code, name, isin_growth, fund_house, category, plan FROM fund "
                        "WHERE fund_house IS ? AND plan IS NOT ? AND scheme_code != ?",
                        (f["fund_house"], f["plan"], f["scheme_code"])):
        if scheme_key(r["name"], r["fund_house"]) == key:
            return _fund(r)
    return None
