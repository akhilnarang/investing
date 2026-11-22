"""The SIF tracker behind /sif: each SIF's launch, short-window returns, terms and book.

Built at request time from SIF NAVs, the terms and exposure sources/sif.py keeps in fund.meta, and
the long/short book in holding.
"""
import calendar
import json
import re
import sqlite3
import statistics
from datetime import date

from ...analysis.nav import nav_on
from ...core.clock import today_ist
from ...core.enums import AssetClass, Side
from ...db import day, undate

CATEGORY = "SIF"
STALE_PORTFOLIO_DAYS = 35   # a month-end book older than this has missed a disclosure
BIG_POSITION = 10           # % of assets in one long that gets flagged


def months_back(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def _houses(db: sqlite3.Connection) -> dict[str, str]:
    """AMC name by ISIN issuer prefix (the first seven characters).

    AMFI's SIF register names the brand ("Apex SIF"), not the fund house, so the house is taken from
    the AMC's ordinary schemes with the same prefix.
    """
    best: dict[str, tuple[int, str]] = {}
    for prefix, house, n in db.execute(
            "SELECT substr(isin_growth, 1, 7), fund_house, COUNT(*) FROM fund "
            "WHERE category IS NOT ? AND isin_growth LIKE 'INF%' AND fund_house IS NOT NULL "
            "GROUP BY 1, 2", (CATEGORY,)):
        if n > best.get(prefix, (0, ""))[0]:
            best[prefix] = (n, house)
    return {p: h for p, (_, h) in best.items()}


def _short_house(house: str | None) -> str:
    return re.sub(r"\s*Mutual Fund$", "", house or "").strip()


def _navs(db: sqlite3.Connection, code: int | None = None) -> dict[int, dict[date, float]]:
    sql = ("SELECT n.scheme_code, n.d, n.nav FROM nav n JOIN fund f ON f.scheme_code = n.scheme_code "
           "WHERE f.category = ?")
    args: list = [CATEGORY]
    if code is not None:
        sql += " AND n.scheme_code = ?"
        args.append(code)
    out: dict[int, dict[date, float]] = {}
    for c, d, v in db.execute(sql + " ORDER BY n.scheme_code, n.d", args):
        out.setdefault(c, {})[undate(d)] = v
    return out


def _row(f: sqlite3.Row, series: dict[date, float], houses: dict[str, str], today: date) -> dict:
    meta = json.loads(f["meta"])
    first, last = min(series), max(series)
    nav_last = series[last]

    def ret(months: int) -> float | None:
        start = months_back(last, months)
        if start < first:
            return None
        v = nav_on(series, start)
        return (nav_last / v - 1) * 100 if v else None

    age = (last - first).days
    house = houses.get((f["isin_growth"] or "")[:7]) or meta.get("fund_house") or f["fund_house"]
    exp = meta.get("exposure")
    return {
        "code": f["scheme_code"], "sif_code": meta.get("sif_code"), "name": f["name"],
        "house": _short_house(house), "strategy": meta.get("strategy") or "Long-short",
        "category": meta.get("category"),
        "launch": first, "age_days": (today - first).days, "nav": nav_last, "nav_date": last,
        "nav_points": len(series),
        "returns": {"1m": ret(1), "3m": ret(3), "6m": ret(6), "1y": ret(12)},
        "since": (nav_last / series[first] - 1) * 100,
        "since_annualised": ((nav_last / series[first]) ** (365.25 / age) - 1) * 100 if age >= 365 else None,
        "ter": meta.get("ter"), "ter_date": meta.get("ter_date"), "aum": meta.get("aum_cr"),
        "manager": meta.get("manager"), "volatility": meta.get("volatility"),
        "lock_in_days": meta.get("lock_in_days"), "turnover": meta.get("portfolio_turnover"),
        "portfolio_date": meta.get("portfolio_date"), "exposure": exp,
        "concentration": (meta.get("detail") or {}).get("book") or {},
    }


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def tracker(db: sqlite3.Connection) -> dict:
    today = today_ist()
    houses = _houses(db)
    navs = _navs(db)
    funds = [_row(f, navs[f["scheme_code"]], houses, today)
             for f in db.execute("SELECT scheme_code, name, isin_growth, fund_house, meta FROM fund "
                                 "WHERE category = ?", (CATEGORY,))
             if navs.get(f["scheme_code"])]
    funds.sort(key=lambda f: (f["launch"], f["code"]), reverse=True)
    # Name the biggest long in the table.
    for f in funds:
        rows = _book(db, f)
        top = [r for r in rows if r["side"] == Side.LONG and r["asset_class"] in (AssetClass.EQUITY, AssetClass.DERIVATIVE)]
        f["biggest"] = top[0]["name"] if top else None
        f["shorts"] = sum(1 for r in rows if r["side"] == Side.SHORT
                          and r["asset_class"] in (AssetClass.EQUITY, AssetClass.DERIVATIVE))

    ters = [f["ter"] for f in funds if f["ter"] is not None]
    nets = [f["exposure"]["net_pct"] for f in funds if f["exposure"]]
    strategies: dict[str, int] = {}
    for f in funds:
        strategies[f["strategy"]] = strategies.get(f["strategy"], 0) + 1
    oldest = min(funds, key=lambda f: f["launch"]) if funds else None
    return {
        "funds": funds,
        "as_of": max((f["nav_date"] for f in funds), default=None),
        "oldest": oldest,
        "any_year_old": any(f["age_days"] >= 365 for f in funds),
        "have_1y": any(f["returns"]["1y"] is not None for f in funds),
        "ter": {"median": _median(ters), "lo": min(ters, default=None), "hi": max(ters, default=None),
                "n": len(ters)},
        "net": {"median": _median(nets), "lo": min(nets, default=None), "hi": max(nets, default=None),
                "n": len(nets)},
        "strategies": sorted(strategies.items(), key=lambda kv: -kv[1]),
    }


def _book(db: sqlite3.Connection, f: dict) -> list[dict]:
    """The book on the date sources/sif.py recorded with the exposure figures.

    Not the newest date in holding: the AMC importer also writes SIF portfolios, without sides.
    """
    if not f["portfolio_date"]:
        return []
    return [dict(r) for r in db.execute(
        "SELECT name, weight, sector, asset_class, side FROM holding WHERE scheme_code = ? AND d = ? "
        "ORDER BY weight DESC, name, side", (f["code"], day(date.fromisoformat(f["portfolio_date"]))))]


def _bare(name: str) -> str:
    """A position's underlying: "Future on Infosys" -> "Infosys"."""
    n = re.sub(r"^Future on\s+", "", name, flags=re.IGNORECASE)
    return re.sub(r"[\s.]+$", "", re.sub(r"\s*\(.*\)\s*$", "", n)).strip()


def detail(db: sqlite3.Connection, code: int, funds: list[dict]) -> dict | None:
    """One fund's panel: its NAV path, the shape of its days, and its whole long/short book."""
    f = next((x for x in funds if x["code"] == code), None)
    if f is None:
        return None
    series = _navs(db, code)[code]
    days = sorted(series)
    navs = [series[d] for d in days]

    peak, peak_day, dd, dd_day, dd_from = navs[0], days[0], 0.0, None, None
    for d, v in zip(days, navs, strict=True):
        if v > peak:
            peak, peak_day = v, d
        fall = (v / peak - 1) * 100
        if fall < dd:
            dd, dd_day, dd_from = fall, d, peak_day
    # Rounded to the 2dp shown, so ties resolve by date.
    moves = [(round((navs[i] / navs[i - 1] - 1) * 100, 2), days[i]) for i in range(1, len(navs))]
    up = sum(1 for i in range(1, len(navs)) if navs[i] > navs[i - 1])

    rows = _book(db, f)
    for r in rows:
        r["counted"] = r["asset_class"] in (AssetClass.EQUITY, AssetClass.DERIVATIVE)
    longs = [r for r in rows if r["side"] == Side.LONG]
    shorts = [r for r in rows if r["side"] == Side.SHORT]
    mix: dict[str, float] = {}
    for r in longs + shorts:
        key = r["sector"] or "Unclassified"
        # Sum the 2dp weights as shown.
        w = round(r["weight"], 2)
        mix[key] = mix.get(key, 0.0) + (w if r["side"] == Side.LONG else -w)
    short_names = {_bare(r["name"]) for r in shorts}
    paired = sorted({_bare(r["name"]) for r in longs} & short_names)
    stale = (today_ist() - date.fromisoformat(f["portfolio_date"])).days if f["portfolio_date"] else 0

    i = next(j for j, x in enumerate(funds) if x["code"] == code)
    return {
        **f,
        "points": [{"date": d.isoformat(), "value": v / navs[0] * 100} for d, v in zip(days, navs, strict=True)],
        "drawdown": dd if dd_day else None, "drawdown_day": dd_day, "drawdown_from": dd_from,
        "best": max(moves, default=None), "worst": min(moves, default=None),
        "up_days": up, "move_days": len(moves),
        "longs": longs, "shorts": shorts,
        "cash": sum(r["weight"] for r in rows if r["side"] is None),
        "sectors": sorted(mix.items(), key=lambda kv: -kv[1]),
        "paired": paired, "stale_days": stale if stale > STALE_PORTFOLIO_DAYS else None,
        "big": (f["concentration"].get("top_pct") or 0) >= BIG_POSITION,
        "prev": funds[i - 1]["code"] if i > 0 else None,
        "next": funds[i + 1]["code"] if i + 1 < len(funds) else None,
    }
