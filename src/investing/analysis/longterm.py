"""The category `longterm` documents `/compare/{slug}` serves, except arbitrage.

Rows carry the AMFI name; Groww's display name is not stored. `plan_pair` recovers the
direct-to-regular mapping from the fund table and excludes schemes it cannot match.
"""
import difflib
import json
import logging
import re
import sqlite3
from collections.abc import Iterator
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

TRAILING_DIRECT = (1, 3, 5, 7, 10)
TRAILING_REGULAR = (1, 3, 5, 7, 10, 15, 20)
ROLLING = (3, 5)
FLOOR = 12.0            # the rolling-5y CAGR floor the "share above floor" column counts against
SIP_YEARS = 10.0

# ---------------------------------------------------------------- classification

# Classify a scheme by its name.
PASSIVE = re.compile(r"\bindex\b|\betf\b|bees|passive|\bfof\b|fund of fund|allocator", re.I)
SECTORS = re.compile(
    r"(?P<financial>\bbank(?:ing)?\b|\bbfsi\b|financial services|\bfinancials?\b)|"
    r"(?P<it>\btechnology\b|\bdigital\b|\bit\b)|"
    r"(?P<health>\bpharma\b|\bhealth(?:care)?\b)|"
    r"(?P<infra>\binfra(?:structure)?\b)")
DECORATORS = re.compile(
    r"\b(equal\s*weight|momentum|quality|value|low\s*volatility|alpha|opportunities|"
    r"sector|consumption|healthcare|manufacturing|defence|tourism|digital|innovation|esg)\b", re.I)
PLAIN_INDEX = re.compile(r"\bindex\b", re.I)
LAYERED = re.compile(r"\betf\b|bees|\bfof\b|fund of fund|allocator", re.I)


def normalise_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def index_key(name: str) -> str | None:
    """Which Nifty index a plain index fund tracks, or None if it is a factor or sector variant."""
    n = normalise_name(name)
    if DECORATORS.search(n):
        return None
    for pattern, key in ((r"nifty\s+next\s+50\b", "nifty_next_50"),
                         (r"nifty\s+large\s*midcap\s+250\b", "nifty_largemidcap_250"),
                         (r"nifty\s+microcap\s+250\b", "nifty_microcap_250"),
                         (r"nifty\s+smallcap\s+250\b", "nifty_smallcap_250"),
                         (r"nifty\s+midcap\s+150\b", "nifty_midcap_150"),
                         (r"nifty\s+100\b", "nifty_100"),
                         (r"nifty\s+500\b", "nifty_500"),
                         (r"(?:nifty\s+50|nifty\s+index\s+fund)\b", "nifty_50")):
        if re.search(pattern, n):
            return key
    return None


def active_segment(row: dict) -> str | None:
    """The precise active mandate a scheme name carries, correcting the aggregator's broad bucket."""
    name = normalise_name(row.get("scheme_name") or "")
    if row.get("sub_category") == "International":
        return "international"      # before the passive test: feeders are the product here
    if PASSIVE.search(name):
        return None
    if "dividend yield" in name:
        return "dividend_yield"
    sector = SECTORS.search(name.replace("bank of india", " "))      # the house, not the sector
    if sector:
        assert sector.lastgroup is not None
        return "sector_" + sector.lastgroup
    if "tax saver" in name or "elss" in name:
        return "elss"
    for needle, segment in (("focused", "focused"), ("contra", "contra"), ("value", "value")):
        if needle in name:
            return segment
    if "flexi cap" in name or "flexicap" in name:
        return "flexi_cap"
    if "large and mid" in name or "large mid" in name or "large & mid" in (row.get("scheme_name") or "").lower():
        return "large_midcap"
    if "small cap" in name or "smallcap" in name:
        return "small_cap"
    if "mid cap" in name or "midcap" in name:
        return "mid_cap"
    if "multi cap" in name or "multicap" in name:
        return "multi_cap"
    if "large cap" in name or row.get("sub_category") == "Large Cap":
        return "large_cap"
    if row.get("sub_category") == "Arbitrage":
        return "arbitrage"
    return None


def is_plain_index(name: str) -> bool:
    """An index fund proper, not an ETF, FoF or allocator wrapped around one."""
    return bool(PLAIN_INDEX.search(name)) and not LAYERED.search(name)


# "Dividend Yield" is a SEBI strategy, not a payout option, so it is removed first.
DIVIDEND_YIELD = re.compile(r"dividend\s*yield", re.I)
NOT_GROWTH = re.compile(r"idcw|dividend|payout|reinvest|bonus|income distribution", re.I)


def is_payout(name: str) -> bool:
    return bool(NOT_GROWTH.search(DIVIDEND_YIELD.sub(" ", name or "")))


# ---------------------------------------------------------------- category table

# The mandate a row's name must carry for each category. Groww's buckets hold other mandates too.
MANDATES = {"Flexi Cap": "flexi_cap", "Multi Cap": "multi_cap", "Large Cap": "large_cap",
            "Mid Cap": "mid_cap", "Small Cap": "small_cap", "Large & MidCap": "large_midcap",
            "ELSS": "elss", "Value Oriented": "value"}

POOLED: dict[str, dict] = {
    "Focused": {"segment": "focused", "index_sub": "Flexi Cap",
                "subs": ("Flexi Cap", "Large Cap", "Large & MidCap", "Mid Cap", "Small Cap",
                         "Multi Cap", "Value Oriented", "Thematic")},
    "Contra": {"segment": "contra", "index_sub": "Flexi Cap",
               "subs": ("Value Oriented", "Flexi Cap", "Thematic")},
    "Dividend Yield": {"segment": "dividend_yield", "index_sub": "Flexi Cap",
                       "subs": ("Thematic", "Flexi Cap", "Value Oriented")},
    # sector funds: filed under Sectoral or Thematic. `index_pattern` admits only index funds on the
    # benchmark itself, not every sector index fund.
    "Banking & Financial Services": {"segment": "sector_financial", "subs": ("Sectoral", "Thematic"),
                                     "index_sub": "Sectoral",
                                     "index_pattern": r"nifty financial services(?! ex)"},
    "Technology": {"segment": "sector_it", "subs": ("Sectoral", "Thematic"), "index_sub": "Sectoral",
                   "index_pattern": r"nifty it\b"},
    "Pharma & Healthcare": {"segment": "sector_health", "subs": ("Sectoral", "Thematic"),
                            "index_sub": "Sectoral", "index_pattern": r"nifty healthcare"},
    "Infrastructure": {"segment": "sector_infra", "subs": ("Sectoral", "Thematic"),
                       "index_sub": "Sectoral", "index_pattern": r"nifty infrastructure"},
    # hybrids: the whole bucket (segment None), against a synthetic equity/gilt blend.
    "Aggressive Hybrid": {"segment": None, "subs": ("Aggressive Hybrid",), "index_sub": "Aggressive Hybrid"},
    "Balanced Advantage": {"segment": None, "subs": ("Dynamic Asset Allocation",), "index_sub": None},
    "Multi Asset": {"segment": None, "subs": ("Multi Asset Allocation",), "index_sub": None},
    # international: no regular-plan mapping, so no regular document.
    "International": {"segment": "international", "subs": ("International",), "index_sub": None,
                      "allow_fof": True, "regular": False},
}

# debt: the whole bucket per SEBI category. Duration categories are benchmarked on the Nifty 10 yr
# G-Sec, the rest on the Nifty 1D Rate Index; those are NSE's two long public fixed-income series.
DEBT_CASH = ("Liquid", "Overnight", "Money Market", "Ultra Short Duration", "Low Duration", "Floater",
             "Short Duration", "Medium Duration", "Corporate Bond", "Banking and PSU", "Credit Risk")
DEBT_GSEC = ("Gilt", "Long Duration", "Medium to Long Duration", "Dynamic Bond", "Target Maturity")
for _k in DEBT_CASH + DEBT_GSEC:
    _subs = ("Gilt", "Gilt with 10 year Constant Duration") if _k == "Gilt" else (_k,)
    POOLED[_k] = {"segment": None, "subs": _subs, "index_sub": None, "debt": True}
# target-maturity funds are all index products
POOLED["Target Maturity"]["all_products"] = True
# commodities: every product is an ETF or a FoF into one
for _metal in ("Gold", "Silver"):
    POOLED[_metal] = {"segment": None, "subs": (_metal,), "index_sub": None, "allow_fof": True,
                      "all_products": True, "commodity": True, "metal": _metal.lower()}

# sub-category label -> (slug, benchmark series name).
CATEGORIES: dict[str, tuple[str, str]] = {
    "Large Cap": ("large-cap", "nifty-100"),
    "Large & MidCap": ("large-midcap", "nifty-largemidcap-250"),
    "Mid Cap": ("mid-cap", "nifty-midcap-150"),
    "Small Cap": ("small-cap", "nifty-smallcap-250"),
    "Flexi Cap": ("flexi-cap", "nifty-500"),
    "Multi Cap": ("multi-cap", "nifty-500"),
    "Value Oriented": ("value-oriented", "nifty-500"),
    "ELSS": ("elss", "nifty-500"),
    "Focused": ("focused", "nifty-500"),
    "Contra": ("contra", "nifty-500"),
    "Dividend Yield": ("dividend-yield", "nifty-500"),
    "Banking & Financial Services": ("banking-financial-services", "nifty-financial-services"),
    "Technology": ("technology", "nifty-it"),
    "Pharma & Healthcare": ("pharma-healthcare", "nifty-healthcare"),
    "Infrastructure": ("infrastructure", "nifty-infrastructure"),
    "Aggressive Hybrid": ("aggressive-hybrid", "65-35-nifty-500-gilt"),
    "Balanced Advantage": ("balanced-advantage", "50-50-nifty-500-gilt"),
    "Multi Asset": ("multi-asset", "50-50-nifty-500-gilt"),
    "International": ("international", "nifty-500"),
    "Gold": ("gold", "gold-inr"),
    "Silver": ("silver", "silver-inr"),
}
CATEGORIES |= {k: (re.sub(r"[^a-z0-9]+", "-", k.lower()).strip("-"),
                   "nifty-1d-rate-index" if k in DEBT_CASH else "nifty-10-yr-benchmark-g-sec")
               for k in DEBT_CASH + DEBT_GSEC}

# Asset class by the row's own sub-category (not the category being built); anything else is Equity.
ASSET_CLASS = ({k: "Debt" for k in DEBT_CASH + DEBT_GSEC}
               | {k: "Debt" for k in ("Gilt with 10 year Constant Duration",)}
               | {k: "Hybrid" for k in ("Aggressive Hybrid", "Dynamic Asset Allocation",
                                        "Multi Asset Allocation", "Arbitrage", "Equity Savings",
                                        "Conservative Hybrid", "Balanced Hybrid")}
               | {"Gold": "Commodities", "Silver": "Commodities"})

# ---------------------------------------------------------------- notes

_DEBT_NOTES = [
    "SEBI's debt categories date from 2018; a fund's history before that belongs to whatever it was "
    "called then, often with a different mandate, so the long horizons compare today's label with "
    "yesterday's fund.",
    "Survivorship bias is heavier in debt than in equity: the six Franklin schemes wound up in 2020 "
    "and the credit-risk funds that broke in 2018 and 2020 are gone from the list, so the category "
    "looks safer than it was.",
    "Returns are before tax; since April 2023 debt-fund gains are taxed as income whatever the "
    "holding period, which is what makes the gap over cash the number to look at.",
]
CASH_NOTE = ("The benchmark is the Nifty 1D Rate Index, the overnight rate compounded daily: what "
             "leaving the money in cash would have earned. Anything above it is the reward for the "
             "duration and credit risk the fund took; NSE does not serve the category's own index "
             "publicly.")
GSEC_NOTE = ("The benchmark is the Nifty 10 yr Benchmark G-Sec index, a total-return series of the "
             "on-the-run ten-year government bond: what taking pure duration risk with no credit "
             "risk earned. NSE does not serve the category's own index publicly.")
BLEND_NOTES = [
    "The benchmark is synthetic: Nifty 500 TRI and a gilt fund's regular-plan NAV, rebalanced "
    "monthly, because CRISIL's hybrid indices are proprietary and NSE's bond indices are not served "
    "publicly. The gilt fund's fee makes the blend a touch easier to beat.",
    "A balanced-advantage fund moves its equity share with the market and a multi-asset fund holds "
    "gold or arbitrage as well; a fixed mix is the yardstick an investor could have held, not the "
    "fund's own mandate.",
]
SECTOR_NOTES = [
    "Sector funds are compared with their sector's total-return index, not with the broad market; a "
    "sector that lagged the Nifty 500 can still have every fund beating its own index, and the other "
    "way round.",
    "Sector index funds are included only where they track the benchmark itself.",
]
INTERNATIONAL_NOTES = [
    "Returns are in rupees, as the investor received them: a weaker rupee flatters every overseas "
    "fund. The currency and the market are not separated here.",
    "The benchmark is the Nifty 500 TRI on purpose: the question is whether going abroad beat "
    "staying home, not whether a feeder tracked its own index.",
    "Most overseas schemes stopped taking fresh money after SEBI's 2022 industry-wide limit, so the "
    "pool is the schemes AMFI still lists.",
]
UNGATED_NOTE = ("Every fund in the category is listed, including the ones launched this year. A "
                "return column is blank wherever the fund is younger than the window: a fund with "
                "four months of NAVs has no three-year number, and no shorter number is shown in "
                "its place. The History column says how much history each one has.")


def metal_copy(metal: str) -> list[str]:
    """The commodity notes. Keep the wording in step with what `sources/metals.py` stores in series_meta."""
    symbol = "GC=F" if metal == "gold" else "SI=F"
    basis = ("a median 0.08% away from it with a 0.64% standard deviation and 1.3% of days more than "
             "2% out, so it tracks the old fix closely"
             if metal == "gold" else
             "a median 0.01% away from it but with a 1.59% standard deviation and 15% of days more "
             "than 2% out, so a single day of it must not be read as a fix")
    return [
        f"The benchmark is the {metal} price in rupees: the COMEX front-month futures close in "
        f"dollars (Yahoo's {symbol}) times the ECB's USD/INR rate, grossed up by the customs duty of "
        "the day. Indian ETFs are valued at the domestic IBJA price, which carries that duty plus a "
        "local premium of a few tenths of a percent, so small gaps are the premium, not the fund.",
        f"That dollar leg is a futures close, not an auction fix, and carries a basis to the fix: {basis}.",
        "Everything here is a wrapper: ETFs hold the metal, FoFs hold the ETF and add their own fee "
        "on top of the ETF's. The difference between a fund and the metal is what you pay for "
        "convenience; nobody is picking anything.",
        "ETF rows use the fund's NAV, not its market price; an ETF bought on the exchange can trade "
        "at a premium or discount to that NAV, sometimes a large one for silver.",
        "Duty cuts show up as a one-day drop in the domestic price that the dollar series does not "
        "have; the schedule reproduces the big ones but not to the day.",
    ]


def category_notes(sub: str) -> list[str]:
    spec = POOLED.get(sub, {})
    if spec.get("metal"):
        return metal_copy(spec["metal"])
    if sub in DEBT_CASH:
        return [CASH_NOTE, *_DEBT_NOTES]
    if sub in DEBT_GSEC:
        notes = [GSEC_NOTE, *_DEBT_NOTES]
        if sub == "Target Maturity":
            notes.insert(1, "These are index funds with a fixed maturity date; they roll down the "
                            "curve and their behaviour changes as maturity nears, which no "
                            "constant-maturity benchmark reproduces.")
        return notes
    if sub in ("Aggressive Hybrid", "Balanced Advantage", "Multi Asset"):
        return list(BLEND_NOTES)
    if sub in ("Banking & Financial Services", "Technology", "Pharma & Healthcare", "Infrastructure"):
        return list(SECTOR_NOTES)
    if sub == "International":
        return list(INTERNATIONAL_NOTES)
    return []


# ---------------------------------------------------------------- the database side

def universe(db: sqlite3.Connection) -> list[dict]:
    """Every scheme row. `overseas_pool` needs the ones Groww does not list."""
    out = []
    for r in db.execute("SELECT scheme_code, name, fund_house, plan, category, meta FROM fund"):
        meta = json.loads(r["meta"])
        out.append({"scheme_code": r["scheme_code"], "scheme_name": r["name"],
                    "fund_house": r["fund_house"], "plan": r["plan"], "category": r["category"],
                    "plan_type": meta.get("plan_type"), "sub_category": meta.get("sub_category"),
                    "aum": meta.get("aum"), "exit_load": meta.get("exit_load"),
                    "search_id": meta.get("groww_id"), "min_sip": meta.get("min_sip"),
                    "min_lumpsum": meta.get("min_investment"),
                    "available_for_investment": meta.get("available_for_investment")})
    return out


# Groww does not list most overseas schemes, which are closed to new money, so the pool comes from
# AMFI's category plus a name test.
OVERSEAS_CATEGORY = re.compile(r"overseas|investing overseas", re.I)
INTERNATIONAL_NAME = re.compile(
    r"nasdaq|s&p ?500|\bu\.?s\.?\b|global|international|world|overseas|off-?shore|"
    r"emerging market|europe|china|japan|asia|asean|developed market|hang seng|msci|"
    r"\bfang\b|nyse|brazil|taiwan", re.I)
# Overseas equity only. "gold mining" is equity.
NOT_EQUITY_ABROAD = re.compile(r"treasury|bond|debt|reit|real estate|gold\b(?! mining)|liquid|income", re.I)


def overseas_pool(rows: list[dict]) -> Iterator[dict]:
    for row in rows:
        name = row["scheme_name"]
        if row["plan"] != Plan.DIRECT or is_payout(name):
            continue
        if re.search(r"\betf\b", name, re.I) and not re.search(r"fund of fund|\bfof\b", name, re.I):
            continue
        # Groww's International bucket is taken as is; the name test is for rows with no bucket.
        if row.get("sub_category") == "International":
            yield row
            continue
        abroad = OVERSEAS_CATEGORY.search(row.get("category") or "") or INTERNATIONAL_NAME.search(name)
        if abroad and not NOT_EQUITY_ABROAD.search(name):
            yield {**row, "sub_category": "International"}


def select_pool(rows: list[dict], sub: str) -> tuple[list[dict], str | None]:
    """The rows to compare for `sub`, and the mandate they must carry (None: take the whole bucket)."""
    if sub == "International":
        return list(overseas_pool(rows)), POOLED[sub]["segment"]
    spec = POOLED.get(sub)
    if not spec:
        return [r for r in rows if r["sub_category"] == sub and r["plan"] == Plan.DIRECT], MANDATES.get(sub)
    index_ok = re.compile(spec["index_pattern"], re.I) if spec.get("index_pattern") else None
    pool = []
    for r in rows:
        if r["sub_category"] not in spec["subs"] or r["plan"] != Plan.DIRECT:
            continue
        name = r["scheme_name"]
        if spec.get("all_products"):
            pool.append(r)
        elif r["sub_category"] == spec["index_sub"] and is_plain_index(name):
            if index_ok is None or index_ok.search(name):
                pool.append(r)
        elif (spec["segment"] is None or active_segment(r) == spec["segment"]) and not is_plain_index(name):
            pool.append(r)
    return pool, spec["segment"]


def classify(pool: list[dict], sub: str, bench_key: str | None) -> tuple[list[dict], list[dict]]:
    """Split the pool into rows to evaluate and rows excluded for what they are, not for their age.

    The exclusion reasons are fixed strings; callers match on them.
    """
    spec = POOLED.get(sub, {})
    mandate = None if sub in POOLED else MANDATES.get(sub)
    keep, excluded = [], []
    for row in pool:
        name = row["scheme_name"]
        is_index = is_plain_index(name)
        if is_index and bench_key and not spec.get("index_pattern") and not spec.get("all_products") \
                and index_key(name) != bench_key:
            excluded.append({"name": name,
                             "excluded": "index fund on a different index than the category's benchmark"})
            continue
        if spec.get("all_products"):
            is_index = False
        elif spec.get("allow_fof"):
            # feeders stay; passive ones are marked passive
            is_index = bool(re.search(r"passive|index|etf", name, re.I))
        elif PASSIVE.search(name) and not is_index:
            excluded.append({"name": name, "excluded": "ETF / FoF / passive allocator"})
            continue
        if not is_index and mandate and active_segment(row) != mandate:
            excluded.append({"name": name,
                             "excluded": f"filed under {sub} but not a {sub.lower()} mandate "
                                         "(focused / retirement / children's / solution scheme)"})
            continue
        keep.append({**row, "passive": is_index} if is_index else row)
    return keep, excluded


def ter_table(db: sqlite3.Connection) -> tuple[dict, str | None]:
    """The official AMFI expense ratios, from the stored `ter` document."""
    row = db.execute("SELECT body FROM analysis WHERE kind=? AND slug=''",
                     (AnalysisKind.TER,)).fetchone()
    if row is None:
        return {}, None
    doc = json.loads(row["body"])
    return doc.get("schemes") or {}, (doc.get("meta") or {}).get("month")


# Words normalise() keeps but the TER feed lacks: ICICI's "Cumulative", AMFI's "(formerly ...)", "Gr".
TER_NOISE = re.compile(r"\(\s*(?:formerly|erstwhile)[^)]*\)?|\bcumulative\b|\bgr\b", re.I)
# Fuzzy match floor for AMFI typos ("AGGRESIVE", "Heathcare"). The worst correct match scores 0.933
# and the best wrong one 0.912; lowering it matches different schemes.
TER_FUZZY_FLOOR = 0.93


def ter_key(name: str) -> str:
    return normalise(TER_NOISE.sub(" ", name or ""))


def scheme_ter(ters: dict, name: str) -> dict:
    """The official expense ratios for this scheme, or {} when nothing here can say which it is."""
    key = ter_key(name)
    hit = ters.get(key)
    if hit is not None:
        return hit
    for near in difflib.get_close_matches(key, ters, 3, TER_FUZZY_FLOOR):
        # Digits must match: "Nifty SDL Sep 2025" and "Sep 2027" are different schemes.
        if re.findall(r"\d+", key) == re.findall(r"\d+", near):
            return ters[near]
    return {}


def benchmark_series(db: sqlite3.Connection, name: str) -> dict[date, float]:
    return {undate(d): v for d, v in
            db.execute("SELECT d, v FROM series WHERE name=? ORDER BY d", (name,))}


# Plan and option words dropped before comparing names. Kotak says "Standard Plan", Bandhan "Plan B".
_PLAN_WORDS = re.compile(r"\b(direct|regular|standard|plan|growth|option|opt|fund|scheme|[ab])\b", re.I)
# Same-house name match floor, for AMFI typos ("REGULAT", "Paln"). The nearest wrong match is 0.79.
# Payout rows must be filtered out first: Baroda's IDCW row is closer than its regular growth row.
FUZZY_FLOOR = 0.80


def _plan_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _PLAN_WORDS.sub(" ", (name or "").lower().replace("&", "and")))


def regular_rows(db: sqlite3.Connection) -> dict[str | None, list[dict]]:
    """Every regular growth row, by fund house, with its newest NAV day.

    The NAV day breaks ties where AMFI lists a scheme twice after a merger (HSBC, Tata, Mirae, PGIM,
    Sundaram).
    """
    out: dict[str | None, list[dict]] = {}
    for r in db.execute("SELECT f.scheme_code, f.name, f.fund_house, MAX(n.d) AS last_nav "
                        "FROM fund f LEFT JOIN nav n ON n.scheme_code = f.scheme_code "
                        "WHERE f.plan='regular' GROUP BY f.scheme_code"):
        if is_payout(r["name"]):
            continue
        out.setdefault(r["fund_house"], []).append(dict(r))
    return out


def plan_pair(row: dict, regulars: dict[str | None, list[dict]]) -> dict | None:
    """The regular growth row of this direct row's scheme, matched by name within the fund house.

    None for renames with no lineage in the data (HDFC Medium to Long Term Fund, formerly HDFC Income
    Fund); those are reported as excluded.
    """
    pool = regulars.get(row["fund_house"]) or []
    if not pool:
        return None
    key = _plan_key(row["scheme_name"])
    candidates = [c for c in pool if _plan_key(c["name"]) == key]
    if not candidates:
        scored = [(difflib.SequenceMatcher(None, key, _plan_key(c["name"])).ratio(), c) for c in pool]
        best = max(score for score, _ in scored)
        if best < FUZZY_FLOOR:
            return None
        candidates = [c for score, c in scored if score == best]
    return max(candidates, key=lambda c: (c["last_nav"] or 0, c["scheme_code"]))


# ---------------------------------------------------------------- per-fund record

def evaluate(db: sqlite3.Connection, row: dict, plan: str, as_of: date, trailing: tuple[int, ...],
             ters: dict, bench: dict[date, float], code: int) -> dict:
    """One fund's record. `code` is the series to measure: the direct row's own, or its paired
    regular row's. The name and identity are always the direct row's.
    """
    scheme = scheme_ter(ters, row["scheme_name"])
    notes: list[str] = []
    series = nav.load_db(code, db, notes=notes)
    # Under nav.MIN_POINTS there is no screened series; the raw days give the fund's age only.
    raw = series if series is not None else {
        undate(d): v for d, v in
        db.execute("SELECT d, nav FROM nav WHERE scheme_code=? ORDER BY d", (int(code),))}
    item = {
        "name": row["scheme_name"], "scheme_code": str(code), "plan": plan,
        "direct_code": str(row["scheme_code"]),
        # BER, not TER: AMFI's TER annualises one day's costs, so a small fund can read 44%.
        "ber": scheme.get("regular_ber") if plan == Plan.REGULAR else scheme.get("effective_ber"),
        "aum": float(row.get("aum") or 0.0),
        "exit_load": row.get("exit_load"),
        "history_years": round(history_years(series if series is not None else raw, as_of), 2),
        "fund_house": row.get("fund_house"), "search_id": row.get("search_id"),
        "category": ASSET_CLASS.get(row.get("sub_category") or "", "Equity"),
        "sub_category": row.get("sub_category"),
        "min_sip": row.get("min_sip"), "min_lumpsum": row.get("min_lumpsum"),
        "inception": str(nav.inception(raw)) if raw else None,
        "nav_notes": notes,
    }
    if row.get("passive"):
        item["passive"] = True
    if series is None:
        item["too_short"] = "no usable NAV history"
        return item

    history = item["history_years"]
    item["cagr"] = {str(y): rnd(nav.cagr(series, y, as_of)) for y in trailing}
    item["cagr_since_inception"] = rnd(since_inception(series, as_of))
    item["rolling"] = {str(y): rolling_summary(series, y, as_of, FLOOR) for y in ROLLING}
    item["max_drawdown_5y"] = rnd(max_drawdown(series, as_of, 5), 1)
    item["max_drawdown_life"] = rnd(max_drawdown(series, as_of, 40), 1)
    item["sip_xirr"] = rnd(sip_xirr(series, SIP_YEARS, as_of))
    item["calendar_years"] = calendar_years(series, as_of)
    item.update(detail_series(series, as_of, ROLLING))
    if bench:
        item["vs_benchmark_5y"] = rolling_excess(series, bench, 5, as_of, max(1, int(history - 5)))
        item["benchmark_cagr"] = {str(y): rnd(nav.cagr(bench, y, as_of)) for y in trailing}
        item["benchmark_sip_xirr"] = rnd(sip_xirr(bench, SIP_YEARS, as_of))
    item["nav_source"] = "amfi"
    item["nav_retrieved"] = str(as_of)
    return item


# ---------------------------------------------------------------- sorting

# Default sort: the longest horizon enough funds reach, else BER.
COVERAGE = 1 / 3
MIN_RANKED = 5
SHORTEST_RANKED = 3


def default_sort(funds: list[dict], horizons: tuple[int, ...]) -> str:
    for h in sorted(horizons, reverse=True):
        if h < SHORTEST_RANKED:
            break
        covered = sum((f.get("history_years") or 0) >= h for f in funds)
        if covered >= max(MIN_RANKED, len(funds) * COVERAGE):
            return str(h)
    return "ber"


def sort_funds(funds: list[dict], sort: str) -> None:
    """Best first on the sort column, cheapest first on BER, blanks last.

    Matches the order `category.html`'s sort script gives on a click.
    """
    if sort == "ber":
        funds.sort(key=lambda f: (f.get("ber") is None, f.get("ber") or 0, f["name"]))
        return

    def key(f: dict):
        v = (f.get("cagr") or {}).get(sort)
        if v is None or (f.get("history_years") or 0) < float(sort):
            return (1, 0.0, f["name"])
        return (0, -v, f["name"])

    funds.sort(key=key)


# ---------------------------------------------------------------- sources and metal decomposition

def duty_schedule(meta: dict) -> list[tuple[date, float]]:
    return [(date.fromisoformat(s["from"]), s["pct"]) for s in meta.get("duty_schedule") or []]


def duty_on(schedule: list[tuple[date, float]], d: date) -> float:
    rate = 0.0
    for start, pct in schedule:
        if d >= start:
            rate = pct
    return rate


def metal_decomposition(db: sqlite3.Connection, name: str, as_of: date,
                        horizons: tuple[int, ...] = (10, 5, 1)) -> list[str]:
    """How much of the rupee return was the metal, the currency and the duty.

    The dollar leg is the rupee series with `usd-inr` and the `series_meta` duty schedule divided out.
    """
    meta = db.execute("SELECT meta FROM series_meta WHERE name=?", (name,)).fetchone()
    rupee = benchmark_series(db, name)
    fx = benchmark_series(db, "usd-inr")
    if meta is None or not rupee or not fx:
        return []
    schedule = duty_schedule(json.loads(meta["meta"]))
    rdays, fdays = sorted(rupee), sorted(fx)

    def on_or_before(d: date, days: list[date]) -> date | None:
        return max((k for k in days if k <= d), default=None)

    end = on_or_before(as_of, rdays)
    end_fx = on_or_before(as_of, fdays)
    if end is None or end_fx is None:
        return []
    parts = []
    for h in horizons:
        target = (as_of.replace(year=as_of.year - h) if not (as_of.month == 2 and as_of.day == 29)
                  else as_of.replace(year=as_of.year - h, day=28))
        start = on_or_before(target, rdays)
        start_fx = on_or_before(target, fdays)
        if start is None or start_fx is None or (target - start).days > 10:
            continue
        duty_start, duty_end = duty_on(schedule, start), duty_on(schedule, end)

        def cagr(a: float, b: float, years: int = h) -> float:
            return ((b / a) ** (1 / years) - 1) * 100

        total = cagr(rupee[start], rupee[end])
        currency = cagr(fx[start_fx], fx[end_fx])
        duty = cagr(1 + duty_start / 100, 1 + duty_end / 100)
        dollars = cagr(rupee[start] / (fx[start_fx] * (1 + duty_start / 100)),
                       rupee[end] / (fx[end_fx] * (1 + duty_end / 100)))
        parts.append(f"{h}y: {total:+.1f}% a year in rupees = {dollars:+.1f}% metal in dollars, "
                     f"{currency:+.1f}% rupee weakening, {duty:+.1f}% duty change "
                     f"(duty {duty_start:g}% then, {duty_end:g}% now)")
    if not parts:
        return []
    out = ["Where the rupee return came from, compounded: " + "; ".join(parts) + "."]
    ibja = db.execute("SELECT meta FROM series_meta WHERE name=?",
                      (f"ibja-{name.split('-')[0]}",)).fetchone()
    if ibja is not None:
        first = json.loads(ibja["meta"]).get("first")
        out.append(f"IBJA's own rate, the one ETFs are valued at, is archived from {first}; the "
                   "synthetic series is checked against it where they overlap, and the duty "
                   "schedule's steps were dated from gold ETF NAVs, the May 2026 return to 15% "
                   "included.")
    return out


METAL_SOURCE = {
    "gold-inr": ("GC=F", "gold"),
    "silver-inr": ("SI=F", "silver"),
}


def sources(db: sqlite3.Connection, name: str, bench: dict[date, float], series_count: int,
            ter_month: str | None, as_of: date) -> list[dict]:
    """The document's source citations."""
    meta_row = db.execute("SELECT kind, meta FROM series_meta WHERE name=?", (name,)).fetchone()
    meta = json.loads(meta_row["meta"]) if meta_row else {}
    out = [{
        "name": "AMFI NAV history",
        "url": "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx",
        "what": f"daily NAVs for {series_count} scheme series, from the nav table",
        "retrieved": str(as_of),
    }, {
        "name": "AMFI expense ratios",
        "url": "https://www.amfiindia.com/ter-of-mf-schemes",
        "what": "official base and total expense ratios, direct and regular plans, "
                f"for {ter_month or 'the current month'}",
        "retrieved": str(as_of),
    }]
    span = (f"{min(bench)} to {max(bench)} ({len(bench)} days)" if bench else "no rows")
    if name in METAL_SOURCE:
        symbol, metal = METAL_SOURCE[name]
        out.append({
            "name": f"COMEX {metal} futures close (Yahoo Finance)",
            "url": f"https://finance.yahoo.com/quote/{symbol}/history",
            "what": f"daily {metal} front-month futures close in US dollars per troy ounce, {span}. "
                    f"A futures close, not an auction fix. {meta.get('usd_basis', '')}".strip(),
            "retrieved": str(as_of),
        })
        out.append({
            "name": "ECB reference exchange rates",
            "url": "https://data.ecb.europa.eu/data/datasets/EXR",
            "what": "USD/INR derived from the euro reference rates for the rupee and the dollar, daily",
            "retrieved": str(as_of),
        })
    else:
        out.append({
            "name": f"{meta.get('index', meta.get('scheme_name', name))} (NSE Indices)",
            "url": "https://www.niftyindices.com/reports/historical-data",
            "what": f"daily index values, {span}",
            "retrieved": str(as_of),
        })
    out.append({
        "name": "Groww",
        "url": "https://groww.in/mutual-funds",
        "what": "scheme universe and identifiers, AUM, exit-load wording, minimum investment; "
                "not used for any return",
        "retrieved": str(as_of),
    })
    return out


# ---------------------------------------------------------------- the document

def slug_for(sub: str, plan: str) -> str:
    base = CATEGORIES[sub][0]
    return base if plan == Plan.DIRECT else f"{base}-regular"


def document(db: sqlite3.Connection, sub: str, plan: str, as_of: date,
             rows: list[dict] | None = None, ters: dict | None = None,
             ter_month: str | None = None,
             regulars: dict[str | None, list[dict]] | None = None) -> dict:
    """One category document. Pass the lookup tables to build them once across categories."""
    if sub not in CATEGORIES:
        raise ValueError(f"unknown category {sub!r}; known: {sorted(CATEGORIES)}")
    bench_name = CATEGORIES[sub][1]
    trailing = TRAILING_DIRECT if plan == Plan.DIRECT else TRAILING_REGULAR
    rows = universe(db) if rows is None else rows
    if ters is None:
        ters, ter_month = ter_table(db)
    regulars = regular_rows(db) if regulars is None else regulars
    bench = benchmark_series(db, bench_name)
    if not bench:
        raise LookupError(f"series {bench_name!r} is empty; run scripts/refresh.py")

    pool, _ = select_pool(rows, sub)
    keep, excluded = classify(pool, sub, bench_name.replace("-", "_"))

    funds = []
    for row in keep:
        code = row["scheme_code"]
        if plan == Plan.REGULAR:
            mapped = plan_pair(row, regulars)
            if mapped is None:
                excluded.append({"name": row["scheme_name"], "excluded": "no regular-plan series mapped"})
                continue
            code = mapped["scheme_code"]
        funds.append(evaluate(db, row, plan, as_of, trailing, ters, bench, code))

    sort = default_sort(funds, trailing)
    sort_funds(funds, sort)

    bench_stats = {
        "cagr": {str(y): rnd(nav.cagr(bench, y, as_of)) for y in trailing},
        "life": rnd(since_inception(bench, as_of)), "inception": str(min(bench)),
        "sip_xirr": rnd(sip_xirr(bench, SIP_YEARS, as_of)),
        "rolling": {str(y): rolling_summary(bench, y, as_of, FLOOR) for y in ROLLING},
        "max_drawdown_5y": rnd(max_drawdown(bench, as_of, 5), 1),
        "max_drawdown_life": rnd(max_drawdown(bench, as_of, 40), 1),
        "calendar_years": calendar_years(bench, as_of),
        **detail_series(bench, as_of, ROLLING),
    }
    notes = category_notes(sub)
    if POOLED.get(sub, {}).get("metal"):
        notes += metal_decomposition(db, bench_name, as_of)
    notes.append(UNGATED_NOTE)
    bench_meta = db.execute("SELECT meta FROM series_meta WHERE name=?", (bench_name,)).fetchone()
    bench_label = json.loads(bench_meta["meta"]).get("scheme_name", bench_name) if bench_meta else bench_name
    return {
        "as_of": str(as_of), "sub_category": sub, "min_years": 0.0, "sort": sort,
        "sip_years": SIP_YEARS, "category_notes": notes, "floor": FLOOR, "ter_month": ter_month,
        "horizons": list(trailing), "plan": plan,
        "sources": sources(db, bench_name, bench, len(funds), ter_month, as_of),
        "benchmark": {"name": bench_label, "scheme_code": bench_name, "kind": "index",
                      "history_years": None, "ber": None},
        "bench_stats": bench_stats, "funds": funds, "excluded": excluded,
    }


def plans_for(sub: str) -> tuple[str, ...]:
    """Which plans this category has a document for. International has no regular-plan mapping."""
    return (Plan.DIRECT,) if POOLED.get(sub, {}).get("regular") is False else (Plan.DIRECT, Plan.REGULAR)


def as_of_for(db: sqlite3.Connection) -> date:
    """The newest day the fund NAVs and every benchmark series share.

    AMFI publishes in the evening, so the funds can trail the index by a day. The fund side is the
    80th percentile of each scheme's newest day, since many schemes stopped publishing long ago.
    """
    lasts = sorted(r[0] for r in db.execute(
        "SELECT MAX(d) FROM nav GROUP BY scheme_code") if r[0] is not None)
    funds_last = lasts[int(len(lasts) * 0.8)] if lasts else None
    bench_last = min((max(s) for name in {v[1] for v in CATEGORIES.values()}
                      if (s := benchmark_series(db, name))), default=None)
    if funds_last is None or bench_last is None:
        raise LookupError("no NAV or benchmark rows; run scripts/refresh.py first")
    return min(undate(funds_last), bench_last)


def write_all(db: sqlite3.Connection) -> str:
    """Write every category's longterm documents. Arbitrage is written by `analysis.arbitrage`."""
    as_of = as_of_for(db)
    rows, ter_month = ter_table(db)
    shared = (universe(db), rows, ter_month, regular_rows(db))
    written = 0
    for sub in CATEGORIES:
        for plan in plans_for(sub):
            doc = document(db, sub, plan, as_of, *shared)
            slug = slug_for(sub, plan)
            blank = sum("too_short" in f for f in doc["funds"])
            logger.debug("%-36s %4d funds (%d unmeasurable), %3d excluded, sort %s",
                         slug, len(doc["funds"]), blank, len(doc["excluded"]), doc["sort"])
            with writing(db):
                db.execute("INSERT OR REPLACE INTO analysis (kind, slug, as_of, body) VALUES (?,?,?,?)",
                           (AnalysisKind.LONGTERM, slug, doc["as_of"],
                            json.dumps(doc, ensure_ascii=False, separators=(",", ":"))))
            written += 1
    logger.debug("as of %s", as_of)
    return f"wrote {written} documents"
