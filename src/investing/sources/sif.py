"""SEBI Specialised Investment Funds: the register, the NAV history and the long/short book.

SIFs may sell short, so `holding.side` is set here and null for ordinary funds.

  amfiindia.com/api/sif-latest-nav      the register: Sd_Id (SIF-33), ISIN, strategy, plan, option
  amfiindia.com/api/sif-nav-history     daily NAVs per plan, up to five years a request
  api.kuvera.in fund_schemes + fund_portfolio_holdings   month-end portfolio and terms

SIF plan numbers are offset into a reserved band (SIF-33 is scheme_code 9000033) so they do not
collide with mutual fund codes.

Weights are unsigned and `side` carries the direction, so SUM(weight) means gross as it does for
ordinary funds. Cash and net-current-asset rows keep their sign so a payable cancels a receivable.
"""
import json
import re
import time
import urllib.request
from datetime import UTC, date, datetime, timedelta

from ..core import egress
from ..core.clock import IST, today_ist
from ..core.enums import AssetClass, Plan, Side
from ..db import day, undate, writing
from . import UA, cache_dir, kuvera

CACHE = cache_dir("sif")
LATEST = "https://www.amfiindia.com/api/sif-latest-nav?type="
HISTORY = "https://www.amfiindia.com/api/sif-nav-history"
KUVERA_SCHEME = "https://api.kuvera.in/mf/api/v5/fund_schemes/{code}.json"
KUVERA_HOLDINGS = "https://api.kuvera.in/mf/api/v5/fund_portfolio_holdings/{code}.json"

SIF_BASE = 9_000_000   # SIF-33 -> 9000033; a band clear of real scheme codes
CATEGORY = "SIF"       # fund.category, so `WHERE category='SIF'` is the whole universe
SINCE = date(2025, 1, 1)   # no SIF predates SEBI's framework
OVERLAP_DAYS = 7           # AMFI revises recent NAVs; the merge is keyed by date

# --- Position classification. A name here is the name the site shows.
DERIV = re.compile(r"\bfuture|\boption|\$\$", re.I)
NCA = re.compile(r"net current|\bnca\b|\bcash\b|margin|\btreps\b|\brepo\b|receivable|payable|interest rate swap", re.I)
DEBT = re.compile(r"NCD & Bonds|govt stock|g-?sec|\bgoi\b|republic of|\btbill\b|\bdtb\b|treasury bill|"
                  r"\bcd\b$|certificate of deposit|commercial paper|\bsdl\b|state development", re.I)


def clean_position(name: str) -> str:
    n = re.sub(r"\b(Ltd\.?|Limited)\b", "", name)
    n = re.sub(r"\s*\$\$\s*$", "", n)
    n = re.sub(r"\s+Ordinary Shares\b", "", n, flags=re.I)
    n = re.sub(r"(\s+-\s+NCD & Bonds)+", " - NCD & Bonds", n, flags=re.I)
    n = re.sub(r"\s+\.(?=\s|$)", "", n)
    return re.sub(r"\s{2,}", " ", n).strip(" .")
# --- end classification


def clean_name(nav_name: str) -> str:
    """AMFI's NavName without the plan and option boilerplate."""
    n = re.sub(r"\s*[-\u2013]?\s*(direct\s*plan|direct|growth\s*option|growth|option|plan)\s*", " ", nav_name, flags=re.I)
    n = re.sub(r"\s*[-\u2013]\s*$", "", n).strip(" -\u2013")
    n = re.sub(r"\s{2,}", " ", n).replace("Ex- Top", "Ex-Top").replace("Long - Short", "Long-Short").replace("Long- Short", "Long-Short")
    return n.title().replace("Sif", "SIF").replace("Wsif", "WSIF").replace("Isif", "iSIF").replace("Qsif", "qsif").replace("Dynasif", "DynaSIF").replace("Redhex", "RedHex") if n.isupper() else n


STRATEGY_PREFIX = re.compile(r"^.*?investment strategies\s*-\s*", re.I)


def strategy_of(category: str | None) -> str:
    s = STRATEGY_PREFIX.sub("", category or "").replace(" Fund", "").strip()
    return s or "Long-short"


def _num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def code_of(sd_id: str) -> int:
    return SIF_BASE + int(sd_id.split("-")[1])


def direct_growth(r: dict) -> bool:
    """Houses label plan and option inconsistently, so all three fields are read."""
    blob = f"{r.get('NavName', '')} {r.get('Plan', '')} {r.get('Option', '')}".lower()
    return "direct" in blob and "growth" in blob and not re.search(r"idcw|dividend|payout|reinvest", blob)


# --- fetching -----------------------------------------------------------------------------------

def get_json(url: str, attempts: int = 4, timeout: int = 45):
    """AMFI's SIF endpoints, which are small and do not rate-limit; Kuvera goes through the pool."""
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # any failure of one attempt is retried
            error = exc
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"{url}: {error!r}")


def register(refresh: bool = True) -> list[dict]:
    """Every SIF plan AMFI lists, cached raw so a delisted plan is still on record."""
    cached = CACHE / "sif-latest-nav.json"
    if refresh or not cached.exists():
        payload = get_json(LATEST)
        rows = [s for block in payload.get("data") or []
                for cat in block.get("categories") or []
                for group in cat.get("groups") or []
                for s in group.get("schemes") or []]
        if len(rows) < 20:
            raise RuntimeError(f"AMFI's SIF register has only {len(rows)} records; refusing to load it")
        cached.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return json.loads(cached.read_text(encoding="utf-8"))


def history(sd_id: str, start: date, end: date) -> dict[date, float]:
    url = (f"{HISTORY}?query_type=historical_period&from_date={start:%Y-%m-%d}"
           f"&to_date={end:%Y-%m-%d}&sd_id={sd_id}")
    payload = get_json(url)
    out: dict[date, float] = {}
    for group in (payload.get("data") or {}).get("nav_groups") or []:
        for rec in group.get("historical_records") or []:
            try:
                out[date.fromisoformat(rec["date"][:10])] = float(rec["nav"])
            except (KeyError, ValueError, TypeError):
                continue
    return out


def kuvera_portfolio(pool, kuvera_code: str) -> tuple[dict, list[dict]]:
    """One SIF's Kuvera scheme record and its month-end rows, cached raw before they are parsed."""
    cached = CACHE / f"kuvera-{kuvera_code}.json"
    scheme = pool.get_json(KUVERA_SCHEME.format(code=kuvera_code))
    scheme = scheme[0] if isinstance(scheme, list) and scheme else (scheme or {})
    payload = pool.get_json(KUVERA_HOLDINGS.format(code=kuvera_code))
    rows = (payload.get(kuvera_code) if isinstance(payload, dict) else payload) or []
    cached.write_text(json.dumps({"scheme": scheme, "holdings": rows,
                                  "retrieved_at": datetime.now(UTC).isoformat()},
                                 ensure_ascii=False), encoding="utf-8")
    return scheme, rows


# --- classification -----------------------------------------------------------------------------

def positions(rows: list[dict]) -> list[dict]:
    """One Kuvera portfolio, classified into the rows worth storing.

    Dropped: percentage_to_aum <= 0, which are positions Kuvera still lists after the fund exited
    them (they carry value_in_mn 0 too), and derivative legs with no value, whose direction is
    unknowable.
    """
    sector_of = {}
    for r in rows:
        name = (r.get("company_name") or "").strip()
        if r.get("sector_name") and name:
            sector_of[re.sub(r"\b(Ltd\.?|Limited)\b", "", name).strip(" .")] = r["sector_name"]
    out = []
    for r in rows:
        pct = r.get("percentage_to_aum") or 0.0
        if pct <= 0:
            continue
        val = r.get("value_in_mn")
        raw = (r.get("company_name") or r.get("security_name") or "").strip()
        short = (val or 0) < 0
        if NCA.search(raw):
            # Cash, TREPS, margin, receivables and swaps: no side, signed weight.
            out.append({"name": clean_position(raw), "isin": r.get("holding_isin"), "company": None,
                        "weight": -pct if short else pct, "value": val, "sector": None,
                        "asset_class": AssetClass.CASH, "side": None})
            continue
        deriv = bool(DERIV.search(raw))
        if deriv and not val:
            continue
        cls = (r.get("security_asset_class") or "").lower()
        bare = clean_position(re.sub(r"^future on\s+", "", raw, flags=re.I))
        sector = (r.get("sector_name") or sector_of.get(bare)
                  or ("Derivatives" if deriv else ("Debt" if DEBT.search(raw) else None)))
        out.append({"name": clean_position(raw), "isin": r.get("holding_isin"),
                    "company": (r.get("company_name") or "").strip() or None,
                    "weight": pct, "value": val, "sector": sector,
                    "asset_class": AssetClass.DERIVATIVE if deriv else (
                        cls or (AssetClass.DEBT if sector == "Debt" else AssetClass.OTHER)),
                    "side": Side.SHORT if short else Side.LONG})
    return out


HOUSE_WORDS = {"ICICIPRUDENTIAL": "ICICI Prudential", "SBI": "SBI", "TATA": "Tata",
               "EDELWEISS": "Edelweiss", "QUANT": "Quant", "BANDHAN": "Bandhan",
               "MIRAEASSET": "Mirae Asset", "360ONE": "360 ONE", "KOTAKMAHINDRA": "Kotak Mahindra",
               "THEWEALTHCOMPANY": "The Wealth Company", "ADITYABIRLA": "Aditya Birla Sun Life"}


def house_of(kuvera_house: str | None) -> str | None:
    """Kuvera's AMC label ("ICICIPRUDENTIALMUTUALFUND_MF_SIF") as the name the site shows."""
    if not kuvera_house:
        return None
    bare = re.sub(r"_MF_SIF$|_SIF$|_MF$", "", kuvera_house.strip())
    bare = re.sub(r"MUTUAL\s*FUND$", "", bare, flags=re.I).strip("_ ")
    pretty = HOUSE_WORDS.get(bare.upper().replace(" ", ""))
    return f"{pretty or bare.title()} Mutual Fund"


def exposures(rows: list[dict]) -> dict:
    """Long, short, net and gross exposure in % of AUM, over the legs that are exposure.

    Only equity and derivative rows count; debt and cash are not directional exposure.
    """
    counted = [r for r in rows if r["asset_class"] in (AssetClass.EQUITY, AssetClass.DERIVATIVE)]
    long_total = sum(r["weight"] for r in counted if r["side"] == Side.LONG)
    short = sum(r["weight"] for r in counted if r["side"] == Side.SHORT)
    equity = sum(r["weight"] for r in counted if r["side"] == Side.LONG and r["asset_class"] == AssetClass.EQUITY)
    return {"long_pct": round(long_total, 1), "long_equity_pct": round(equity, 1),
            "long_derivatives_pct": round(long_total - equity, 1),
            "short_pct": round(short, 1), "net_pct": round(long_total - short, 1),
            "gross_pct": round(long_total + short, 1),
            "equity_holdings": sum(1 for r in counted
                                   if r["side"] == Side.LONG and r["asset_class"] == AssetClass.EQUITY),
            "cash_pct": round(sum(r["weight"] for r in rows if r["side"] is None), 1)}


def concentration(rows: list[dict]) -> dict:
    """Share of the book in the largest long positions, among equity and derivative legs."""
    # Round each weight before summing so the total matches the weights as shown.
    longs = sorted((round(r["weight"], 2) for r in rows
                    if r["side"] == Side.LONG and r["asset_class"] in (AssetClass.EQUITY, AssetClass.DERIVATIVE)),
                   reverse=True)
    return {"top_pct": longs[0] if longs else 0.0,
            "top5_pct": round(sum(longs[:5]), 2), "top10_pct": round(sum(longs[:10]), 2)}


def store_holdings(db, code: int, rows: list[dict], portfolio_date: str) -> int:
    """The book for one month-end. Names go in exactly as clean_position() produced them.

    A SIF can hold one security both long and short with no ISIN, so side_key is in the primary
    key. Rows agreeing on name, ISIN and side (such as several lots of one T-bill) are summed.
    """
    d = day(date.fromisoformat(portfolio_date))
    merged: dict[tuple, dict] = {}
    for r in rows:
        key = (r["name"], r["isin"] or "", r["side"] or "")
        if key in merged:
            m = merged[key]
            for f in ("weight", "value"):
                if r[f] is not None:
                    m[f] = (m[f] or 0) + r[f]
        else:
            merged[key] = dict(r)
    stored = [(code, d, r["name"], r["isin"], r["company"], r["weight"], r["value"],
               None, r["sector"], r["asset_class"], r["side"],
               r["isin"] or "", r["side"] or "") for r in merged.values()]
    db.executemany(
        "INSERT OR REPLACE INTO holding (scheme_code, d, name, isin, company, weight, value, "
        "quantity, sector, asset_class, side, isin_key, side_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        stored)
    return len(stored)


# --- the fetch ----------------------------------------------------------------------------------

def newest_nav(db, code: int) -> date | None:
    row = db.execute("SELECT MAX(d) FROM nav WHERE scheme_code=?", (code,)).fetchone()
    return undate(row[0]) if row and row[0] is not None else None


def brand_of(r: dict) -> str:
    """First word of the plan name on AMFI ('Altiva', 'qsif', 'Magnum'); SIFName as the fallback."""
    first = re.sub(r"[^a-z0-9]+", " ", (r.get("NavName") or "").lower()).split()
    return first[0] if first else (r.get("SIFName") or "").lower().replace(" sif", "").strip()


def resolve(r: dict, entries: list[dict], shortlist: int = 6) -> dict | None:
    """The Kuvera code whose ISIN equals this plan's, among the entries sharing its brand.

    Each candidate costs a request, so only entries whose first word is the brand are tried.
    """
    isin = r.get("ISINPO") or ""
    if not isin:
        return None
    brand = brand_of(r)
    want = kuvera.tokens(r.get("NavName") or "")
    same_brand = [e for e in entries if kuvera.tokens(e["name"])
                  and (re.sub(r"[^a-z0-9]+", " ", e["name"].lower()).split() or [""])[0] == brand]
    ranked = sorted(same_brand, key=lambda e: -len(want & kuvera.tokens(e["name"]))
                    / max(len(want | kuvera.tokens(e["name"])), 1))
    for e in ranked[:shortlist]:
        try:
            candidate_isin = kuvera.scheme_isin(e["code"])
        except Exception:
            # Misses are not stored, so a failed lookup is retried next run.
            continue
        if candidate_isin == isin:
            return {"kuvera_code": e["code"], "isin": isin, "kuvera_name": e["name"]}
    return None


def mapped_code(db, r: dict, entries: list[dict] | None) -> tuple[str | None, list[dict] | None, bool]:
    """This SIF's Kuvera code: the one kept in kuvera_code, or a fresh match stored there.

    A match costs requests against a rate-limited API, so it is stored. Misses are not.
    """
    code = code_of(r["Sd_Id"])
    row = db.execute("SELECT kuvera_code FROM kuvera_code WHERE scheme_code=?", (code,)).fetchone()
    if row:
        return row["kuvera_code"], entries, False
    if entries is None:
        entries = kuvera.kuvera_list()
    got = resolve(r, entries)
    if not got:
        return None, entries, False
    with writing(db):
        db.execute("INSERT OR REPLACE INTO kuvera_code VALUES (?,?,?,?,?)",
                   (code, got["kuvera_code"], got["isin"], got["kuvera_name"],
                    datetime.now(UTC).isoformat()))
    return got["kuvera_code"], entries, True


def fetch(db, since: date | None = None, all_plans: bool = False, portfolios: bool = True,
          min_gap: float = 0.35) -> str:
    """The register, then each plan's NAVs from where the database left off, then the books."""
    since = since or SINCE
    today = today_ist()
    plans = [r for r in register() if all_plans or direct_growth(r)]
    if not plans:
        return "sif: AMFI listed no direct-growth plans"

    funds = navs = 0
    for r in plans:
        sd_id = r.get("Sd_Id")
        if not sd_id:
            continue
        code = code_of(sd_id)
        last = newest_nav(db, code)
        start = max(since, last - timedelta(days=OVERLAP_DAYS)) if last else since
        series = history(sd_id, start, today) if start <= today else {}
        meta = {"sif_code": sd_id, "amfi": r, "strategy": strategy_of(r.get("category")),
                "category": r.get("category"), "source": "amfi-sif", "source_url": HISTORY,
                "retrieved_at": datetime.now(UTC).isoformat()}
        with writing(db):
            db.execute(
                "INSERT INTO fund (scheme_code, name, isin_growth, fund_house, category, plan, meta) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(scheme_code) DO UPDATE SET "
                "name=excluded.name, isin_growth=excluded.isin_growth, "
                "fund_house=excluded.fund_house, category=excluded.category, plan=excluded.plan, "
                "meta=excluded.meta",
                (code, clean_name(r.get("NavName") or sd_id), r.get("ISINPO") or None,
                 r.get("SIFName"), CATEGORY,
                 Plan.DIRECT if "direct" in (r.get("Plan") or r.get("NavName") or "").lower() else Plan.REGULAR,
                 json.dumps(meta, ensure_ascii=False)))
            db.executemany("INSERT OR REPLACE INTO nav (scheme_code, d, nav) VALUES (?,?,?)",
                           [(code, day(d), v) for d, v in series.items()])
        funds += 1
        navs += len(series)

    books = longs = shorts = cash = resolved = 0
    if portfolios:
        # Kuvera rate-limits per public address.
        with egress.rotation(min_gap=min_gap, user_agent=UA) as pool:
            kuvera.POOL = pool      # the listing and ISIN lookups share this rotation
            try:
                entries: list[dict] | None = None
                for r in plans:
                    if not r.get("Sd_Id"):
                        continue
                    sd_id = r["Sd_Id"]
                    kcode, entries, fresh = mapped_code(db, r, entries)
                    resolved += fresh
                    if not kcode:
                        continue
                    try:
                        scheme, rows = kuvera_portfolio(pool, kcode)
                    except Exception:  # one fund failing does not stop the run
                        continue
                    dates = {d for h in rows if (d := h.get("portfolio_date"))}
                    if not dates:
                        continue
                    pdate = max(dates)
                    # Keep only the newest month-end in case a stray older row is present.
                    book = positions([h for h in rows
                                      if (h.get("portfolio_date") or pdate) == pdate])
                    if not book:
                        continue
                    # Terms and exposure figures go in fund.meta beside AMFI's keys.
                    code = code_of(sd_id)
                    house = house_of(scheme.get("fund_house"))
                    aum = _num(scheme.get("aum"))
                    terms = {"kuvera_code": kcode, "portfolio_date": pdate,
                             "fund_house": house, "manager": (scheme.get("fund_manager") or "").split(";")[0].strip() or None,
                             "ter": _num(scheme.get("expense_ratio")), "ter_date": scheme.get("expense_ratio_date"),
                             "aum_cr": round(aum / 10, 1) if aum is not None else None,
                             "volatility": _num(scheme.get("volatility")),
                             "lock_in_days": scheme.get("lock_in_period"),
                             "portfolio_turnover": scheme.get("portfolio_turnover"),
                             "exposure": exposures(book),
                             "detail": {"book": concentration(book)}}
                    with writing(db):
                        store_holdings(db, code, book, pdate)
                        meta = json.loads(db.execute("SELECT meta FROM fund WHERE scheme_code=?",
                                                     (code,)).fetchone()[0])
                        db.execute("UPDATE fund SET fund_house=COALESCE(?, fund_house), meta=? "
                                   "WHERE scheme_code=?",
                                   (house, json.dumps({**meta, **terms}, ensure_ascii=False), code))
                    books += 1
                    longs += sum(1 for h in book if h["side"] == Side.LONG)
                    shorts += sum(1 for h in book if h["side"] == Side.SHORT)
                    cash += sum(1 for h in book if h["side"] is None)
            finally:
                kuvera.POOL = None

    with writing(db):
        db.execute("INSERT OR REPLACE INTO source_state VALUES ('sif', ?, ?, ?)",
                   (datetime.now(IST).isoformat(timespec="seconds"),
                    db.execute("SELECT MAX(d) FROM nav WHERE scheme_code>=?", (SIF_BASE,)).fetchone()[0],
                    f"{funds} SIFs, {books} portfolios"))
    return (f"sif: {funds} funds, {navs:,} NAVs, {books} portfolios "
            f"({longs} long, {shorts} short, {cash} cash)"
            + (f", {resolved} newly mapped to Kuvera" if resolved else ""))
