"""Active share and tilt/selection attribution, the sidecars `/compare/{slug}` shows.

Active share (`active_share`, slug = direct category slug) is 100 minus the overlap, the sum of
min(w_fund, w_index). Index weights come from an index fund's disclosed portfolio. Only positive
equity rows count; futures legs are dropped. Cash and foreign stocks count as fully active.

Attribution (`attribution`, slugs `<category>-10y` and `<category>-regular-15y`) regresses monthly
fund returns on the Nifty 100, Midcap 150 and Smallcap 250 TRIs by OLS with an intercept. The betas
are the fund's cap mix, compounded monthly as a passive portfolio:

    excess    = fund CAGR - benchmark TRI CAGR
    tilt      = mix CAGR  - benchmark TRI CAGR    (what the fitted exposure alone earned)
    selection = fund CAGR - mix CAGR              (picking, timing, fees, and anything the three
                                                   indexes cannot see)

Selection is net of fees. Betas are unconstrained, so a beta sum well below 1 shows up as negative
tilt. Below an R-squared of about 0.85 both halves are loose.
"""
import json
import logging
import re
import sqlite3
from datetime import date, timedelta

from ..core.clock import month_end, month_ends
from ..core.enums import AnalysisKind, AssetClass, Plan
from ..db import day, undate, writing
from . import nav
from .longterm import CATEGORIES, benchmark_series

logger = logging.getLogger(__name__)

# Index funds whose disclosed portfolio stands in for each benchmark's weights: (label, direct scheme
# code, Groww slug, name, minimum holdings). The minimum rejects a half-parsed portfolio.
PROXIES: dict[str, tuple[str, int, str, str, int]] = {
    "nifty-100": ("Nifty 100", 147666, "axis-nifty-100-index-fund-direct-growth",
                  "Axis Nifty 100 Index Fund Direct Growth", 40),
    "nifty-largemidcap-250": ("Nifty LargeMidcap 250", 152482,
                              "icici-prudential-nifty-largemidcap-250-index-fund-direct-growth",
                              "ICICI Prudential Nifty LargeMidcap 250 Index Fund Direct Growth", 40),
    "nifty-midcap-150": ("Nifty Midcap 150", 147622, "motilal-oswal-nifty-midcap-150-index-fund-direct-growth",
                         "Motilal Oswal Nifty Midcap 150 Index Fund Direct Growth", 40),
    "nifty-smallcap-250": ("Nifty Smallcap 250", 148519, "nippon-india-nifty-smallcap-250-index-fund-direct-growth",
                           "Nippon India Nifty Smallcap 250 Index Fund Direct Growth", 40),
    "nifty-500": ("Nifty 500", 147625, "motilal-oswal-nifty-500-index-fund-direct-growth",
                  "Motilal Oswal Nifty 500 Index Fund Direct Growth", 40),
    # `str.title` of NIFTY IT, as the stored documents spell it.
    "nifty-it": ("Nifty It", 150468, "icici-prudential-nifty-it-index-fund-direct-growth",
                 "ICICI Prudential Nifty IT Index Fund Direct Growth", 10),
    "nifty-healthcare": ("Nifty Healthcare", 153594, "dsp-nifty-healthcare-index-fund-direct-growth",
                         "DSP Nifty Healthcare Index Fund Direct Growth", 20),
}
# International is left out: against a Nifty 500 proxy every fund would read about 100.
ACTIVE_SHARE = tuple(slug for sub, (slug, bench) in CATEGORIES.items()
                     if bench in PROXIES and sub != "International")
# Diversified equity categories only. Regular plans have the longer history.
ATTRIBUTED = ("large-cap", "large-midcap", "mid-cap", "small-cap", "flexi-cap", "multi-cap",
              "value-oriented", "elss", "focused", "contra", "dividend-yield")
WINDOWS = {Plan.DIRECT: 10, Plan.REGULAR: 15}
FACTORS = [("large", "nifty-100"), ("mid", "nifty-midcap-150"), ("small", "nifty-smallcap-250")]


def bench_of(slug: str) -> str:
    return next(b for s, b in CATEGORIES.values() if s == slug)


def longterm_doc(db: sqlite3.Connection, slug: str) -> dict | None:
    row = db.execute("SELECT body FROM analysis WHERE kind=? AND slug=?",
                     (AnalysisKind.LONGTERM, slug)).fetchone()
    return None if row is None else json.loads(row["body"])


# ---------------------------------------------------------------- active share

FUT = re.compile(r"(future|fut\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[- ]?20\d\d)", re.IGNORECASE)


def _norm(n: str) -> str:
    n = (n or "").strip().lower()
    n = re.sub(r"\b(limited|ltd|ltd\.|the)\b", "", n)
    return re.sub(r"[^a-z0-9]+", "", n)


def portfolio(db: sqlite3.Connection, code: int, on: date | None = None) -> dict | None:
    """The newest disclosed portfolio of a scheme on or before `on`, as equity weights by ISIN.

    Kuvera rows carry a market value and AMC disclosures do not, which identifies the source.
    """
    row = db.execute("SELECT MAX(d) FROM holding WHERE scheme_code=? AND d<=?",
                     (code, day(on) if on else 1 << 30)).fetchone()
    if row[0] is None:
        return None
    rows = db.execute("SELECT COALESCE(company, name) AS label, isin, weight, value, asset_class "
                      "FROM holding WHERE scheme_code=? AND d=?", (code, row[0])).fetchall()
    weights: dict[str, float] = {}
    names: dict[str, str] = {}
    for r in rows:
        key = r["isin"] or _norm(r["label"])
        if key and r["label"] and key not in names:
            names[key] = r["label"]
        pct = r["weight"] or 0
        if (r["asset_class"] or "").lower() != AssetClass.EQUITY or pct <= 0 or FUT.search(r["label"]):
            continue
        if key:
            weights[key] = weights.get(key, 0) + pct
    return {"d": undate(row[0]), "weights": weights, "names": names,
            "source": "kuvera" if any(r["value"] is not None for r in rows) else "amc"}


def compare(fund: dict[str, float], index: dict[str, float], names: dict[str, str] | None = None) -> dict:
    names = names or {}
    common = sum(min(fund.get(k, 0.0), index.get(k, 0.0)) for k in fund)
    diffs = {k: fund.get(k, 0.0) - index.get(k, 0.0) for k in set(fund) | set(index)}
    over = sorted(((v, k) for k, v in diffs.items() if v > 0), reverse=True)[:5]
    under = sorted(((v, k) for k, v in diffs.items() if v < 0))[:5]
    in_index = sum(v for k, v in fund.items() if k in index)
    ranked = sorted(((v, k) for k, v in fund.items()), reverse=True)
    return {
        "equity_pct": round(sum(fund.values()), 2),
        "in_index_pct": round(in_index, 2),
        "off_index_pct": round(sum(fund.values()) - in_index, 2),
        "overlap_pct": round(common, 2),
        "active_share_pct": round(100 - common, 2),
        "holdings": len(fund),
        "top_pct": round(ranked[0][0], 2) if ranked else 0.0,
        "top5_pct": round(sum(v for v, _ in ranked[:5]), 2),
        "top10_pct": round(sum(v for v, _ in ranked[:10]), 2),
        "top_holdings": [{"stock": k, "name": names.get(k), "pct": round(v, 2)} for v, k in ranked[:10]],
        "top_overweights": [{"stock": k, "name": names.get(k), "diff_pp": round(v, 2)} for v, k in over],
        "top_underweights": [{"stock": k, "name": names.get(k), "diff_pp": round(v, 2)} for v, k in under],
    }


def active_share(db: sqlite3.Connection, slug: str, index_on: date | None = None,
                 fund_on: dict[str, date] | None = None) -> dict:
    """The `active_share` document for one category. `index_on` and `fund_on` pin portfolio dates."""
    label, proxy_code, proxy_slug, proxy_name, floor = PROXIES[bench_of(slug)]
    proxy = portfolio(db, proxy_code, index_on)
    if proxy is None or len(proxy["weights"]) < floor:
        raise LookupError(f"{slug}: no usable proxy portfolio for scheme {proxy_code}; run the holdings fetch")
    total = sum(proxy["weights"].values())
    index = {k: v * 100 / total for k, v in proxy["weights"].items()}
    payload = longterm_doc(db, slug)
    if payload is None:
        raise LookupError(f"{slug}: no longterm document; run longterm.write_all")
    results: dict[str, dict] = {}
    used: dict[str, int] = {}
    for fund in payload["funds"]:
        if fund.get("passive"):
            continue
        code = str(fund.get("direct_code") or fund["scheme_code"])
        held = portfolio(db, int(code), (fund_on or {}).get(code))
        if held is None or not held["weights"]:
            continue
        r = compare(held["weights"], index, {**proxy["names"], **held["names"]})
        r["source"] = held["source"]
        r["as_of"] = str(held["d"])
        used[held["source"]] = used.get(held["source"], 0) + 1
        results[code] = r
    sources = []
    if used.get("kuvera"):
        sources.append({"name": "Kuvera disclosed portfolios", "url": "https://kuvera.in/mutual-funds",
                        "what": f"month-end holdings, with stock ISINs, of {used['kuvera']} funds and of {proxy_name} "
                                f"as the {label} weight source ({len(index)} constituents)",
                        "retrieved": f"portfolio dated {proxy['d']}"})
    if used.get("amc"):
        sources.append({"name": "Fund house monthly portfolio disclosures", "url": None,
                        "what": f"month-end holdings, with stock ISINs, of {used['amc']} funds",
                        "retrieved": None})
    return {"index": label, "index_proxy": proxy_slug, "index_stocks": len(index),
            "index_portfolio_date": str(proxy["d"]), "sources": sources, "funds": results}


# ---------------------------------------------------------------- attribution

def monthly_returns(series: dict[date, float], ends: list[date]) -> list[float] | None:
    navs = []
    for d in ends:
        v = nav.nav_on(series, d)
        if v is None:
            return None
        navs.append(v)
    return [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting, for the 4x4 normal equations."""
    n = len(b)
    m = [[*row, b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        if abs(m[col][col]) < 1e-12:
            raise ValueError("singular design matrix")
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def ols(y: list[float], xs: list[list[float]]) -> tuple[list[float], float]:
    """[intercept, b1..bk] and R-squared."""
    n, k = len(y), len(xs)
    rows = [[1.0] + [xs[j][i] for j in range(k)] for i in range(n)]
    p = k + 1
    xtx = [[sum(rows[i][r] * rows[i][c] for i in range(n)) for c in range(p)] for r in range(p)]
    xty = [sum(rows[i][r] * y[i] for i in range(n)) for r in range(p)]
    beta = solve(xtx, xty)
    fitted = [sum(beta[j] * rows[i][j] for j in range(p)) for i in range(n)]
    ybar = sum(y) / n
    ss_res = sum((y[i] - fitted[i]) ** 2 for i in range(n))
    ss_tot = sum((v - ybar) ** 2 for v in y)
    return beta, (1 - ss_res / ss_tot if ss_tot else 0.0)


def cagr_of(returns: list[float]) -> float:
    growth = 1.0
    for r in returns:
        growth *= 1 + r
    return (growth ** (12 / len(returns)) - 1) * 100


def attribution_slug(slug: str, plan: str) -> str:
    return f"{slug}-{WINDOWS[plan]}y" if plan == Plan.DIRECT else f"{slug}-regular-{WINDOWS[plan]}y"


def _index_source(db: sqlite3.Connection, name: str, role: str) -> dict:
    # series_meta's first/last cover only the last incremental fetch.
    meta_row = db.execute("SELECT meta FROM series_meta WHERE name=?", (name,)).fetchone()
    meta = json.loads(meta_row["meta"]) if meta_row else {}
    lo, hi = db.execute("SELECT MIN(d), MAX(d) FROM series WHERE name=?", (name,)).fetchone()
    span = f"{undate(lo):%d-%m-%Y} to {undate(hi):%d-%m-%Y}" if lo is not None else "none"
    return {"name": f"{meta.get('index', name)} Total Return Index (NSE Indices)",
            "url": "https://www.niftyindices.com/reports/historical-data",
            "what": f"{role}; daily values {span}",
            "retrieved": (meta.get("retrieved_at") or "")[:10] or None}


def attribution(db: sqlite3.Connection, slug: str, plan: str) -> dict:
    """The `attribution` document for one category and plan, on its longterm document's as_of."""
    years = WINDOWS[plan]
    payload = longterm_doc(db, slug if plan == Plan.DIRECT else f"{slug}-regular")
    if payload is None:
        raise LookupError(f"{slug} {plan}: no longterm document; run longterm.write_all")
    as_of = date.fromisoformat(payload["as_of"])
    # The last complete month end on or before as_of.
    newest = as_of if month_end(as_of.year, as_of.month) <= as_of else as_of.replace(day=1) - timedelta(days=1)
    ends = month_ends(newest, years * 12 + 1)
    bench = bench_of(slug)
    factors = {}
    for key, name in [*FACTORS, ("benchmark", bench)]:
        r = monthly_returns(benchmark_series(db, name), ends)
        if r is None:
            raise LookupError(f"{name}: no TRI series covering {ends[0]}..{ends[-1]}")
        factors[key] = r
    bench_cagr = cagr_of(factors["benchmark"])
    xs = [factors[k] for k, _ in FACTORS]
    results = {}
    for fund in payload["funds"]:
        if fund.get("passive"):
            continue
        s = nav.load_db(fund["scheme_code"], db)
        y = monthly_returns(s, ends) if s else None
        if y is None:
            continue
        beta, r2 = ols(y, xs)
        mix = [sum(beta[j + 1] * xs[j][i] for j in range(3)) for i in range(len(y))]
        fund_cagr, mix_cagr = cagr_of(y), cagr_of(mix)
        # keyed by the direct-plan code so the page can join either plan's run
        results[str(fund.get("direct_code") or fund["scheme_code"])] = {
            "window_years": years, "months": len(y),
            "fund_cagr": round(fund_cagr, 2), "benchmark_cagr": round(bench_cagr, 2),
            "mix_cagr": round(mix_cagr, 2),
            "excess_pp": round(fund_cagr - bench_cagr, 2),
            "tilt_pp": round(mix_cagr - bench_cagr, 2),
            "selection_pp": round(fund_cagr - mix_cagr, 2),
            "alpha_monthly_pct": round(beta[0] * 100, 3),
            "beta_large": round(beta[1], 3), "beta_mid": round(beta[2], 3), "beta_small": round(beta[3], 3),
            "beta_sum": round(beta[1] + beta[2] + beta[3], 3), "r_squared": round(r2, 3),
        }
    return {"as_of": str(as_of), "years": years, "benchmark": bench,
            "sources": [_index_source(db, name, "regression factor" if key != "benchmark" else "benchmark")
                        for key, name in [*FACTORS, ("benchmark", bench)]],
            "window_start": str(ends[0]), "window_end": str(ends[-1]),
            "benchmark_cagr": round(bench_cagr, 2),
            "factors": {k: cagr_of(v) for k, v in factors.items()},
            "funds": results}


def write_all(db: sqlite3.Connection) -> str:
    """Write every `active_share` and `attribution` document. Runs after `longterm.write_all`."""
    built = []
    for slug in ACTIVE_SHARE:
        doc = active_share(db, slug)
        built.append((AnalysisKind.ACTIVE_SHARE, slug, doc))
    for slug in ATTRIBUTED:
        for plan in WINDOWS:
            built.append((AnalysisKind.ATTRIBUTION, attribution_slug(slug, plan), attribution(db, slug, plan)))
    rows = []
    for kind, name, doc in built:
        as_of = doc.get("as_of") or doc.get("index_portfolio_date")
        logger.debug("%-13s %-28s %3d funds, as of %s", kind, name, len(doc["funds"]), as_of)
        rows.append((kind, name, as_of, json.dumps(doc, ensure_ascii=False, separators=(",", ":"))))
    with writing(db):
        db.executemany("INSERT OR REPLACE INTO analysis (kind, slug, as_of, body) VALUES (?,?,?,?)", rows)
    return f"wrote {len(rows)} documents"


# ---------------------------------------------------------------- for the page

def fund_facts(db: sqlite3.Connection, codes: list[str], as_of: str | None = None) -> dict[str, dict]:
    """The stored `fund_facts` entries for these direct codes, with the team's tenure.

    json_each picks the codes out so the large document is not parsed per request. Tenure runs from
    the longest-serving manager's start.
    """
    if not codes:
        return {}
    marks = ",".join("?" * len(codes))
    out = {r[0]: json.loads(r[1]) for r in db.execute(
        f"SELECT j.key, j.value FROM analysis a, json_each(a.body, '$.funds') j "
        f"WHERE a.kind=? AND a.slug='' AND j.key IN ({marks})", [AnalysisKind.FUND_FACTS, *codes])}
    for fact in out.values():
        dated = sorted(m["since"] for m in fact.get("managers") or [] if m.get("since"))
        if dated and as_of:
            fact["since"] = dated[0]
            fact["tenure_years"] = (date.fromisoformat(as_of) - date.fromisoformat(dated[0])).days / 365.25
    return out
