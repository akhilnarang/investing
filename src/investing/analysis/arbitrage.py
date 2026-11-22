"""The arbitrage category's longterm documents, in the same shape `longterm` writes for the others.

The benchmark is the Nifty 1D Rate Index. An arbitrage fund is hedged, so its return is the
cash-futures basis and cash is the comparison, not an equity index.
"""
import json
import logging
import re
import sqlite3
from datetime import date

from ..core.enums import AnalysisKind, Plan
from ..db import undate, writing

# The TER document's key function.
from ..sources.ter import normalise
from . import nav
from .metrics import (
    calendar_years,
    detail_series,
    history_years,
    max_drawdown,
    rnd,
    rolling_excess,
    rolling_summary,
    since_inception,
    sip_xirr,
)

logger = logging.getLogger(__name__)

SUB_CATEGORY = "Arbitrage"
# AMFI uses both "Hybrid Schemes -" and "Hybrid Scheme -".
CATEGORY_LIKE = "Hybrid Scheme% - Arbitrage Fund"
BENCHMARK = ("NIFTY 1D RATE INDEX TRI", "nifty-1d-rate-index")

TRAILING_DIRECT = (1, 3, 5, 7, 10)
TRAILING_REGULAR = (1, 3, 5, 7, 10, 15, 20)
ROLLING = (3, 5)
FLOOR = 12.0            # the rolling-5y floor other categories use
SIP_YEARS = 10.0
# No history gate: returns differ little across arbitrage funds and cost is known from day one.
# A window longer than the fund is left blank.
MIN_YEARS = {Plan.DIRECT: 0.0, Plan.REGULAR: 0.0}
DEFAULT_SORT = "ber"

# Growth plans only, no FoFs. Sundaram spells IDCW out as "income distribution".
NOT_GROWTH = re.compile(r"idcw|dividend|payout|reinvest|bonus|income distribution", re.I)
LAYERED = re.compile(r"\bfof\b|fund of fund|\betf\b|omni", re.I)
STALE_DAYS = 60         # this far behind the category's newest NAV day, a scheme is discontinued

# normalise() drops "growth" but not "Gr" ("SBI Arbitrage Fund - Direct Plan - Gr").
TER_ABBREVIATION = re.compile(r"\bgr\b", re.I)

NOTES = [
    "The benchmark is the Nifty 1D Rate Index, the overnight rate compounded daily: what leaving "
    "the money in cash would have earned. An arbitrage fund buys a stock and sells its future, so "
    "the equity exposure is hedged out and what is left is the cash-futures basis, an implied "
    "funding rate. Cash is therefore the honest comparison, not an equity index.",
    "The basis is not a constant. It widens when futures are in demand and collapses when they are "
    "not, so a year of thin spreads shows up directly in the return and no single trailing number "
    "describes the range.",
    "Returns are growth-NAV returns and therefore net of TER. Backward-looking and "
    "survivorship-biased: a scheme that merged away is not in the list.",
    "Every arbitrage fund on sale is listed, including the ones launched this year, because the "
    "expense ratio is the thing that separates them and it is knowable on a fund's first day. A "
    "return column is blank wherever the fund is younger than the window: a fund with four months "
    "of NAVs has no three-year number, and no shorter number is shown in its place.",
]


# ---------------------------------------------------------------- data

def ter_table(db: sqlite3.Connection) -> tuple[dict, str | None]:
    row = db.execute("SELECT body FROM analysis WHERE kind='ter' AND slug=''").fetchone()
    if row is None:
        return {}, None
    doc = json.loads(row["body"])
    return doc.get("schemes") or {}, (doc.get("meta") or {}).get("month")


def benchmark_series(db: sqlite3.Connection, name: str) -> dict[date, float]:
    return {undate(d): v for d, v in db.execute("SELECT d, v FROM series WHERE name=? ORDER BY d", (name,))}


def plan_pairs(db: sqlite3.Connection) -> tuple[list[dict], dict[str, dict]]:
    """The arbitrage growth schemes: direct rows, and the regular row for each fund house.

    Pairs on fund house, since each house runs one arbitrage fund and AMFI's names carry
    per-plan typos (Sundaram's "Prinicpal" and "Principal").
    """
    # The newest NAV day identifies discontinued plans such as Bandhan's "Plan B - Growth".
    rows = [dict(r) for r in db.execute(
        "SELECT f.scheme_code, f.name, f.fund_house, f.plan, f.meta, MAX(n.d) AS last_nav "
        "FROM fund f LEFT JOIN nav n ON n.scheme_code = f.scheme_code "
        "WHERE f.category LIKE ? GROUP BY f.scheme_code ORDER BY f.name", (CATEGORY_LIKE,))]
    live = max((r["last_nav"] for r in rows if r["last_nav"] is not None), default=0) - STALE_DAYS
    direct, regular = [], {}
    for row in rows:
        if NOT_GROWTH.search(row["name"]) or LAYERED.search(row["name"]):
            continue
        if row["last_nav"] is None or row["last_nav"] < live:
            continue
        key = row["fund_house"]
        if row["plan"] == Plan.DIRECT:
            direct.append({**row, "key": key})
        elif key in regular:
            # House pairing needs one row per plan.
            raise ValueError(f"{key} has two regular growth rows: {regular[key]['name']!r} and "
                             f"{row['name']!r}. The plan pairing needs a name key again.")
        else:
            regular[key] = row
    return direct, regular


def evaluate(db: sqlite3.Connection, row: dict, plan: str, as_of: date, trailing: tuple[int, ...], min_years: float,
             ters: dict, bench: dict[date, float]) -> dict:
    """One fund's record, in the shape `longterm.evaluate` writes."""
    meta = json.loads(row["meta"])
    scheme = ters.get(normalise(TER_ABBREVIATION.sub(" ", row["name"])), {})
    notes: list[str] = []
    series = nav.load_db(row["scheme_code"], db, notes=notes)
    # Under nav.MIN_POINTS there is no screened series; the raw days give the fund's age only.
    raw = series if series is not None else {
        undate(d): v for d, v in
        db.execute("SELECT d, nav FROM nav WHERE scheme_code=? ORDER BY d", (row["scheme_code"],))}
    history = history_years(series, as_of)
    item = {
        "name": row["name"], "scheme_code": str(row["scheme_code"]), "plan": plan,
        "direct_code": str(row["direct_code"]),
        # BER, not TER: AMFI's TER annualises one day's costs, so a small fund can read 44%.
        "ber": scheme.get("regular_ber") if plan == Plan.REGULAR else scheme.get("effective_ber"),
        # From sources/universe.py's Groww merge; 0.0 when Groww does not list the fund, as in longterm.
        "aum": float(meta.get("aum") or 0.0),
        "sip_allowed": meta.get("sip_allowed"), "lumpsum_allowed": meta.get("lumpsum_allowed"),
        "exit_load": meta.get("exit_load"), "history_years": round(history, 2),
        "fund_house": row["fund_house"], "search_id": meta.get("groww_id"),
        "category": "Hybrid", "sub_category": meta.get("sub_category") or SUB_CATEGORY,
        "min_sip": meta.get("min_sip"), "min_lumpsum": meta.get("min_investment"),
        "inception": str(nav.inception(raw)) if raw else None,
        "nav_notes": notes,
    }
    if series is None or history < min_years:
        # Listed without returns.
        item["history_years"] = round(history_years(raw, as_of), 2)
        item["too_short"] = ("no usable NAV history" if series is None
                             else f"only {history:.1f}y of {plan}-plan history")
        return item

    item["cagr"] = {str(y): rnd(nav.cagr(series, y, as_of)) for y in trailing}
    item["cagr_since_inception"] = rnd(since_inception(series, as_of))
    item["rolling"] = {str(y): rolling_summary(series, y, as_of, FLOOR) for y in ROLLING}
    item["max_drawdown_5y"] = rnd(max_drawdown(series, as_of, 5), 1)
    item["max_drawdown_life"] = rnd(max_drawdown(series, as_of, 40), 1)
    item["sip_xirr"] = rnd(sip_xirr(series, SIP_YEARS, as_of))
    item["calendar_years"] = calendar_years(series, as_of)
    item.update(detail_series(series, as_of))
    item["vs_benchmark_5y"] = rolling_excess(series, bench, 5, as_of, max(1, int(history - 5)))
    item["benchmark_cagr"] = {str(y): rnd(nav.cagr(bench, y, as_of)) for y in trailing}
    item["benchmark_sip_xirr"] = rnd(sip_xirr(bench, SIP_YEARS, as_of))
    item["nav_source"] = "amfi"
    item["nav_retrieved"] = str(as_of)
    return item


def document(db: sqlite3.Connection, plan: str, as_of: date) -> dict:
    trailing = TRAILING_DIRECT if plan == Plan.DIRECT else TRAILING_REGULAR
    min_years = MIN_YEARS[plan]
    ters, ter_month = ter_table(db)
    bench = benchmark_series(db, BENCHMARK[1])
    if not bench:
        raise ValueError(f"series {BENCHMARK[1]} is empty; run scripts/refresh.py --only nse_index")

    direct, regular = plan_pairs(db)
    funds, excluded = [], []
    for row in direct:
        if plan == Plan.DIRECT:
            evaluated = {**row, "direct_code": row["scheme_code"]}
        else:
            mapped = regular.get(row["key"])
            if mapped is None:
                excluded.append({"name": row["name"], "excluded": "no regular-plan series mapped"})
                continue
            # The regular row's code and NAVs under the direct row's name, as in the other documents.
            evaluated = {**mapped, "name": row["name"], "direct_code": row["scheme_code"]}
        item = evaluate(db, evaluated, plan, as_of, trailing, min_years, ters, bench)
        (excluded if "excluded" in item else funds).append(item)

    # Cheapest first; a fund with no BER sorts last.
    funds.sort(key=lambda f: (f["ber"] is None, f["ber"] or 0, f["name"]))

    bench_stats = {
        "cagr": {str(y): rnd(nav.cagr(bench, y, as_of)) for y in trailing},
        "life": rnd(since_inception(bench, as_of)), "inception": str(min(bench)),
        "sip_xirr": rnd(sip_xirr(bench, SIP_YEARS, as_of)),
        "rolling": {str(y): rolling_summary(bench, y, as_of, FLOOR) for y in ROLLING},
        "max_drawdown_5y": rnd(max_drawdown(bench, as_of, 5), 1),
        "max_drawdown_life": rnd(max_drawdown(bench, as_of, 40), 1),
        "calendar_years": calendar_years(bench, as_of),
        **detail_series(bench, as_of),
    }
    return {
        "as_of": str(as_of), "sub_category": SUB_CATEGORY, "min_years": min_years,
        "sort": DEFAULT_SORT,
        "sip_years": SIP_YEARS, "category_notes": NOTES, "floor": FLOOR, "ter_month": ter_month,
        "horizons": list(trailing), "plan": plan,
        "sources": [
            {"name": "AMFI NAV history",
             "url": "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx",
             "what": f"daily NAVs for {len(funds) + len(excluded)} scheme series, from the nav table",
             "retrieved": str(as_of)},
            {"name": "AMFI expense ratios", "url": "https://www.amfiindia.com/ter-of-mf-schemes",
             "what": "official base and total expense ratios, direct and regular plans, "
                     f"for {ter_month}", "retrieved": str(as_of)},
            {"name": f"{BENCHMARK[0]} (NSE Indices)",
             "url": "https://www.niftyindices.com/reports/historical-data",
             "what": f"daily index values, {min(bench)} to {max(bench)} ({len(bench)} days)",
             "retrieved": str(as_of)},
        ],
        "benchmark": {"name": BENCHMARK[0], "scheme_code": BENCHMARK[1], "kind": "index",
                      "history_years": None, "ber": None},
        "bench_stats": bench_stats, "funds": funds, "excluded": excluded,
    }


def write(db: sqlite3.Connection) -> str:
    """Write the direct and regular arbitrage documents, as of the benchmark's newest day."""
    as_of = max(benchmark_series(db, BENCHMARK[1]), default=None)
    if as_of is None:
        raise ValueError(f"series {BENCHMARK[1]} is empty; run scripts/refresh.py --only nse_index")
    written = []
    for plan in Plan:
        doc = document(db, plan, as_of)
        slug = "arbitrage" if plan == Plan.DIRECT else "arbitrage-regular"
        cheapest = doc["funds"][0] if doc["funds"] else None
        # The longest horizon any fund reaches; the 1D rate index starts in 2011.
        longest = str(max((h for h in doc["horizons"]
                           if any((f.get("cagr") or {}).get(str(h)) is not None
                                  for f in doc["funds"])),
                          default=doc["horizons"][0]))
        short = sum("too_short" in f for f in doc["funds"])
        logger.debug("%s: %d funds (%d too young to measure), %d excluded, cash %s%% over %sy%s",
                     slug, len(doc["funds"]), short, len(doc["excluded"]),
                     doc["bench_stats"]["cagr"][longest], longest,
                     f", cheapest {cheapest['name']} at {cheapest['ber']}% BER" if cheapest else "")
        with writing(db):
            db.execute("INSERT OR REPLACE INTO analysis (kind, slug, as_of, body) VALUES (?,?,?,?)",
                       (AnalysisKind.LONGTERM, slug, doc["as_of"],
                        json.dumps(doc, ensure_ascii=False, separators=(",", ":"))))
        written.append(slug)
    return f"wrote {len(written)} documents: {', '.join(written)}"
