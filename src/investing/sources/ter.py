"""AMFI's official daily expense ratios, one document in the analysis table.

Base expense ratio and full TER per scheme and plan, newest row in the month for each scheme.

AMFI posts a month house by house over several days, so the previous month is tried when the
current one is incomplete. A month is accepted only when its coverage reaches FLOOR of the last
good document's houses with rows and schemes.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from ..core.clock import today_ist
from ..core.enums import AnalysisKind
from ..db import writing
from . import UA, cache_dir

AMCS_URL = "https://www.amfiindia.com/api/populate-mf"
TER_URL = "https://www.amfiindia.com/api/populate-te-rdata-revised"
MIN_SCHEMES = 500       # absolute floor when there is no previous document to compare against
FLOOR = 0.9             # of the previous document's houses and schemes
OPEN_ENDED = 1


def normalise(name: str) -> str:
    """The scheme key: plan and option words dropped, so direct and regular rows share one key.

    This is the document's primary key; changing it breaks lookups against stored documents."""
    value = (name or "").lower().replace("&", "and")
    value = re.sub(r"\b(direct|regular|plan|growth|option|idcw|dividend|fund|scheme)\b", " ", value)
    value = re.sub(r"\bnifty\s*50\b", "nifty 50", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def get_json(url: str, attempts: int = 5):
    error = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error = exc
            # AMFI answers 429 under concurrency; wait as long as it asks.
            wait = exc.headers.get("Retry-After") if exc.code == 429 else None
            time.sleep(float(wait) if wait and wait.isdigit() else 2.0 * (attempt + 1))
        except Exception as exc:  # any other failure of one attempt is retried
            error = exc
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"AMFI request failed: {url}: {error!r}")


def fetch_amc(amc: dict, month: str) -> list[dict]:
    """One house's whole month, over as many pages as it says it has."""
    def page(number: int):
        query = urllib.parse.urlencode({"MF_ID": amc["mfId"], "Month": month, "strCat": -1,
                                        "strType": OPEN_ENDED, "page": number, "pageSize": 100})
        return get_json(f"{TER_URL}?{query}")

    first = page(1)
    rows = list(first.get("data") or [])
    meta = first.get("meta") or {}
    total, pages = int(meta.get("total") or 0), int(meta.get("pageCount") or 0)
    for number in range(2, pages + 1):
        rows.extend(page(number).get("data") or [])
    if total and len(rows) != total:
        raise RuntimeError(f"partial TER response for {amc['mfName']}: {len(rows)} of {total}")
    return rows


def latest_rows(rows: list[dict]) -> dict[str, dict]:
    """The newest daily row per scheme: AMFI publishes one row per day and the month is cumulative."""
    latest: dict[str, dict] = {}
    for row in rows:
        key = normalise(row.get("Scheme_Name", ""))
        if not key or (not row.get("D_TER") and not row.get("R_TER")):
            continue
        current = latest.get(key)
        if current is None or (row.get("TER_Date") or "") > (current.get("TER_Date") or ""):
            latest[key] = row
    return latest


def acceptable(schemes: int, amcs_with_rows: int, previous: dict | None,
               min_schemes: int = MIN_SCHEMES, floor: float = FLOOR) -> str | None:
    """None when a month may replace the stored document, else why not.

    Compares against `previous["amcs_with_rows"]`, not `previous["amcs"]`: some listed houses
    have no TER rows."""
    if schemes < min_schemes:
        return f"only {schemes} schemes, below the floor of {min_schemes}"
    if previous:
        want_amcs = previous.get("amcs_with_rows") or 0
        want_schemes = previous.get("schemes") or 0
        if amcs_with_rows < floor * want_amcs:
            return f"only {amcs_with_rows} fund houses answered against {want_amcs} last time"
        if schemes < floor * want_schemes:
            return f"only {schemes} schemes against {want_schemes} last time"
    return None


def scheme_row(row: dict) -> dict:
    direct_ber, direct_ter = float(row.get("D_BER") or 0), float(row.get("D_TER") or 0)
    regular_ber, regular_ter = float(row.get("R_BER") or 0), float(row.get("R_TER") or 0)
    # An ETF has one plan, which AMFI files under regular. Without this an ETF's cost reads zero.
    is_etf = "ETF" in (row.get("SchemeCat_Desc") or "").upper()
    fallback = is_etf and direct_ter <= 0 < regular_ter
    return {"scheme_name": row.get("Scheme_Name"), "category": row.get("SchemeCat_Desc"),
            "ter_date": row.get("TER_Date"), "direct_ber": direct_ber, "direct_ter": direct_ter,
            "regular_ber": regular_ber, "regular_ter": regular_ter,
            "effective_ber": regular_ber if is_etf and direct_ber <= 0 < regular_ber else direct_ber,
            "effective_ter": regular_ter if fallback else direct_ter,
            "etf_single_plan_fallback": fallback,
            "nsdl_scheme_code": row.get("NSDLSchemeCode")}


def previous_meta(db) -> dict | None:
    row = db.execute("SELECT body FROM analysis WHERE kind=? AND slug=''", (AnalysisKind.TER,)).fetchone()
    return (json.loads(row[0]).get("meta") or None) if row else None


def fetch(db, month: str | None = None, workers: int = 2, min_schemes: int = MIN_SCHEMES) -> str:
    amcs = get_json(AMCS_URL)
    if not isinstance(amcs, list) or len(amcs) < 40:
        raise RuntimeError("AMFI's fund house list is incomplete; the stored TER document is kept")
    today = today_ist()
    months = [month] if month else [
        today.strftime("%m-%Y"),
        (today.replace(day=1) - timedelta(days=1)).strftime("%m-%Y"),   # AMFI posts a month late
    ]
    previous = previous_meta(db)

    rejected = []
    for candidate in months:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Bind month now, not by closure over the loop variable.
            batches = list(pool.map(lambda amc, month=candidate: fetch_amc(amc, month), amcs))
        rows = [row for batch in batches for row in batch]
        latest = latest_rows(rows)
        amcs_with_rows = sum(1 for batch in batches if batch)
        why = acceptable(len(latest), amcs_with_rows, previous, min_schemes)
        if why is None:
            break
        rejected.append(f"{candidate}: {why}")
    else:
        raise RuntimeError("AMFI TER coverage too thin for every month tried ("
                           + "; ".join(rejected) + "); the stored document is kept")

    body = {
        "meta": {"retrieved_at": datetime.now(UTC).isoformat(), "month": candidate,
                 "source": TER_URL, "source_kind": "official AMFI", "amcs": len(amcs),
                 "amcs_with_rows": amcs_with_rows, "daily_rows": len(rows), "schemes": len(latest)},
        "schemes": {key: scheme_row(row) for key, row in latest.items()},
    }
    text = json.dumps(body, ensure_ascii=False)
    (cache_dir("ter") / f"amfi-ter-{candidate}.json").write_text(text, encoding="utf-8")
    with writing(db):
        db.execute("INSERT OR REPLACE INTO analysis (kind, slug, as_of, body) VALUES (?,'',?,?)",
                   (AnalysisKind.TER, body["meta"]["retrieved_at"], text))
    return (f"ter: {len(latest):,} schemes for {candidate} from {amcs_with_rows} of {len(amcs)} "
            f"fund houses, {len(rows):,} daily rows"
            + (f" (skipped {'; '.join(rejected)})" if rejected else ""))
