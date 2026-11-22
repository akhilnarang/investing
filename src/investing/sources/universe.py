"""Groww's direct-plan universe, merged into the fund rows AMFI already created.

Groww's search endpoint adds what AMFI's NAV report lacks: AUM, exit load, minimum investment,
sub-category and Groww's own expense ratio. `scheme_code` is the AMFI code, so the join is exact.

Codes AMFI does not list are skipped. Groww's `launch_date` is unreliable and not used; inception
comes from the first NAV. Use `ter.py`'s TER, not `groww_expense_ratio`.
"""
import json
import time
import urllib.request

from ..db import writing
from . import UA, cache_dir

URL = "https://groww.in/v1/api/search/v1/derived/scheme?q=&page={page}&size=100"
PAGE_SIZE = 100
MIN_SCHEMES = 1000      # refuse to merge a short universe

# Groww field -> fund meta key.
FIELDS = {
    "aum": "aum", "exit_load": "exit_load", "sub_category": "sub_category",
    "min_investment_amount": "min_investment", "min_sip_investment": "min_sip",
    "expense_ratio": "groww_expense_ratio", "fund_manager": "fund_manager",
    "risk_rating": "risk_rating", "search_id": "groww_id", "plan_type": "plan_type",
    "available_for_investment": "available_for_investment",
}


def get(page: int, attempts: int = 4) -> dict:
    request = urllib.request.Request(URL.format(page=page), headers={"User-Agent": UA})
    error: Exception = RuntimeError("response did not contain a content array")
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("content") is not None:
                return payload
        except Exception as exc:  # any failure of one attempt is retried
            error = exc
        time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"universe page {page} failed after {attempts} attempts: {error!r}")


def deduplicate(rows: list[dict]) -> tuple[list[dict], int]:
    """One row per AMFI scheme code, preferring the row open to investment, then the larger AUM.

    Pagination repeats rows, and a renamed scheme is listed under two search_ids with one code."""

    def score(row: dict) -> tuple[bool, float]:
        return bool(row.get("available_for_investment")), float(row.get("aum") or 0)

    by_search = {row.get("search_id") or row.get("id"): row for row in rows
                 if row.get("search_id") or row.get("id")}
    by_code: dict[str, dict] = {}
    for row in by_search.values():
        code = str(row.get("scheme_code") or "").strip()
        if not code:
            continue
        incumbent = by_code.get(code)
        if incumbent is None or score(row) > score(incumbent):
            by_code[code] = row
    return list(by_code.values()), len(by_search)


def merge(db, rows: list[dict]) -> tuple[int, int]:
    """Merge Groww's fields into each fund's existing meta blob, leaving AMFI's keys alone."""
    have = {code: meta for code, meta in db.execute("SELECT scheme_code, meta FROM fund")}
    merged = unknown = 0
    with writing(db):
        for row in rows:
            try:
                code = int(str(row.get("scheme_code") or "").strip())
            except ValueError:
                continue
            if code not in have:
                unknown += 1        # not in AMFI's report
                continue
            meta = json.loads(have[code])
            meta.update({key: row.get(field) for field, key in FIELDS.items()})
            meta["groww_source"] = URL
            db.execute("UPDATE fund SET meta=? WHERE scheme_code=?",
                       (json.dumps(meta, ensure_ascii=False), code))
            merged += 1
    return merged, unknown


def fetch(db, min_schemes: int = MIN_SCHEMES, pause: float = 0.15) -> str:
    first = get(0)
    total = int(first.get("total_results") or 0)
    if total < min_schemes:
        raise RuntimeError(f"Groww reported only {total} results; nothing merged")
    rows = list(first["content"])
    for page in range(1, (total + PAGE_SIZE - 1) // PAGE_SIZE):
        content = get(page).get("content") or []
        if not content:
            raise RuntimeError(f"universe page {page} was empty; nothing merged")
        rows += content
        time.sleep(pause)
    if len(rows) != total:
        raise RuntimeError(f"Groww reported {total} results, the pages returned {len(rows)}")

    schemes, search_records = deduplicate(rows)
    if len(schemes) < min_schemes:
        raise RuntimeError(f"only {len(schemes)} unique AMFI scheme codes; nothing merged")
    (cache_dir("universe") / "universe.json").write_text(
        json.dumps(schemes, ensure_ascii=False), encoding="utf-8")

    merged, unknown = merge(db, schemes)
    return (f"universe: {merged:,} of {len(schemes):,} schemes merged into fund meta"
            f" ({total - search_records} page repeats, {search_records - len(schemes)} renamed"
            f" aliases removed)"
            + (f", {unknown} code(s) AMFI has no fund row for" if unknown else ""))
