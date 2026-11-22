"""Manager tenure, monthly TER history and the stated benchmark of every Groww fund, from its scheme page.

One page per fund, distilled into a single `analysis` document (kind fund_facts, slug ''), keyed by
direct scheme code:

    {"meta": {...}, "funds": {"122639": {"managers": [{"name", "since"}], "ter_history": [["YYYY-MM", ter]],
                                         "benchmark", "turnover", "launch", "search_id", "mix"?}}}

`mix` is the sovereign / bank and PSU / corporate / cash split of a debt-heavy portfolio, from the
Kuvera holdings. Kuvera has no credit ratings for Indian debt, so the split is by issuer name.

Pages are refetched at most weekly. Groww can truncate a page's expense history, so older months in
the stored document are carried forward.
"""
import gzip
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from ..analysis.longterm import is_payout
from ..core import egress
from ..core.enums import AnalysisKind, AssetClass
from ..db import undate, writing
from . import UA, cache_dir

URL = "https://groww.in/v1/api/data/mf/web/v2/scheme/search/{slug}"
MAX_AGE_DAYS = 7
MIN_SHARE = 0.9       # refuse a document with fewer Groww funds than this share of the stored one
MAX_FAILED = 0.1      # or one built while more than this share of the attempted pages failed


def iso_day(text: str | None) -> str | None:
    """The calendar part of Groww's timestamps, as the UTC day.

    date_from is midnight IST written as UTC (2023-06-11T18:30:00.000Z for the 12th)."""
    if not text:
        return None
    text = text.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text[:26] if "T" in text else text, fmt).date().isoformat()
        except ValueError:
            continue
    return text[:10] if text[:4].isdigit() else None


def facts(page: dict) -> dict | None:
    managers: dict[str, str | None] = {}
    for m in page.get("fund_manager_details") or []:
        name = (m.get("person_name") or "").strip()
        since = iso_day(m.get("date_from"))
        if not name:
            continue
        cur = managers.get(name)
        if name not in managers or (since and (cur is None or since < cur)):
            managers[name] = since
    by_month: dict[str, tuple[str, float]] = {}
    for row in page.get("historic_fund_expense") or []:
        d = iso_day(row.get("as_on_date"))
        ter = row.get("expense_ratio")
        if not d or ter is None:
            continue
        if d[:7] not in by_month or d > by_month[d[:7]][0]:
            by_month[d[:7]] = (d, float(ter))     # the month's last record stands for the month
    history = [[m, round(v[1], 3)] for m, v in sorted(by_month.items())]
    if not managers and not history:
        return None
    return {
        "managers": [{"name": n, "since": s}
                     for n, s in sorted(managers.items(), key=lambda kv: (kv[1] or "9999", kv[0]))],
        "ter_history": history,
        "benchmark": page.get("benchmark_name") or page.get("benchmark"),
        "turnover": page.get("portfolio_turnover"),
        "launch": iso_day(page.get("launch_date")),
        "search_id": page.get("search_id"),
    }


SOVEREIGN = re.compile(r"india \(republic of\)|govt stock|government of india|\bgoi\b|\bg-?sec\b|t-?bill|treasury|state government|"
                       r"\bsdl\b|state development loan|\bstate govt\b|sovereign", re.IGNORECASE)
CASH = re.compile(r"\btreps\b|reverse repo|net current assets|net receivables|cash|margin|\brepo\b|interest rate swap|"
                  r"\bfutures?\b|\boption", re.IGNORECASE)
PSU_BANK = re.compile(r"\bbank\b|nabard|national bank for agriculture|small industries dev|\bsidbi\b|power finance|\bpfc\b|"
                      r"\brec\b|rural electrification|railway finance|\birfc\b|export import|exim|\bhudco\b|housing and urban|"
                      r"national housing|\bnhb\b|\bnhpc\b|\bntpc\b|power grid|\bnhai\b|indian oil|\bongc\b|\bgail\b|"
                      r"\bnlc\b|\bsail\b|food corporation|\bpgcil\b", re.IGNORECASE)
ISSUER_NOISE = re.compile(r"\s*-\s*(ncd|bonds?|commercial paper|certificate of deposit|cd|cp)(\s*&\s*bonds?)?\s*$|"
                          r"\b(limited|ltd\.?|private|pvt\.?)\b|\(earlier[^)]*\)|\d+(\.\d+)?%|\b20\d\d\b", re.IGNORECASE)


def portfolio_mix(rows: list[dict], portfolio_date: str | None) -> dict | None:
    """rows: {name, company, weight, asset_class}, one disclosed portfolio. None for an equity fund;
    a hybrid gets its debt side."""
    total = sum(float(r["weight"] or 0) for r in rows)

    def is_equity(r: dict) -> bool:
        return (r["asset_class"] or "").lower() == AssetClass.EQUITY
    equity = sum(float(r["weight"] or 0) for r in rows if is_equity(r))
    if total < 50 or total - equity < total * 0.25:
        return None
    mix = {"sovereign": 0.0, "psu_bank": 0.0, "corporate": 0.0, "cash": 0.0, "equity": equity}
    issuers: dict[str, float] = {}
    for r in rows:
        w = float(r["weight"] or 0)
        name = r["company"] or r["name"] or ""
        if is_equity(r) or w <= 0:
            continue
        if SOVEREIGN.search(name):
            mix["sovereign"] += w
        elif CASH.search(name):
            mix["cash"] += w
        else:
            mix["psu_bank" if PSU_BANK.search(name) else "corporate"] += w
            key = ISSUER_NOISE.sub("", name).strip(" -,") or name
            issuers[key] = issuers.get(key, 0) + w
    top = sorted(issuers.items(), key=lambda kv: -kv[1])[:5]
    return {**{k: round(v, 1) for k, v in mix.items()}, "instruments": len(rows),
            "top_issuers": [{"name": n.title() if n.isupper() else n, "pct": round(w, 1)} for n, w in top],
            "portfolio_date": portfolio_date}


def mixes(db) -> dict[str, dict]:
    """Each Kuvera-mapped growth plan's latest Kuvera portfolio, excluding IDCW plans and SIFs.

    Kuvera rows are the ones with a value; AMC archive rows lack a dependable asset class."""
    out: dict[str, dict] = {}
    latest = db.execute(
        "SELECT h.scheme_code, f.name, MAX(h.d) FROM holding h JOIN kuvera_code k USING (scheme_code) "
        "JOIN fund f USING (scheme_code) WHERE h.value IS NOT NULL AND f.category != 'SIF' "
        "GROUP BY h.scheme_code").fetchall()
    for code, name, d in latest:
        if is_payout(name):
            continue
        rows = [dict(zip(("name", "company", "weight", "asset_class"), r, strict=True)) for r in db.execute(
            "SELECT name, company, weight, asset_class FROM holding WHERE scheme_code=? AND d=?", (code, d))]
        mix = portfolio_mix(rows, undate(d).isoformat())
        if mix:
            out[str(code)] = mix
    return out


def read_page(path: Path) -> dict | None:
    try:
        page = json.loads(gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes())
    except (OSError, ValueError, EOFError):
        return None
    return page if isinstance(page, dict) and "scheme_code" in page else None


def document(pages: dict[str, dict], mix: dict[str, dict], retrieved: str | None,
             previous: dict | None = None, generated_from: str = "") -> dict:
    """pages: direct scheme code -> Groww page. previous: the stored document, whose months before a
    page's earliest record are carried forward."""
    funds: dict[str, dict] = {}
    old = (previous or {}).get("funds") or {}
    for code, page in pages.items():
        f = facts(page)
        if f is None:
            continue
        before = old.get(code, {}).get("ter_history") or []
        if before and f["ter_history"]:
            f["ter_history"] = [m for m in before if m[0] < f["ter_history"][0][0]] + f["ter_history"]
        funds[code] = f
    groww = len(funds)
    for code, m in mix.items():
        funds.setdefault(code, {})["mix"] = m
    return {"meta": {"source": "Groww scheme pages", "url": "https://groww.in/mutual-funds",
                     "retrieved": retrieved, "generated_from": generated_from, "funds": groww,
                     "with_portfolio_mix": len(mix)},
            "funds": dict(sorted(funds.items()))}


def stored(db) -> dict | None:
    row = db.execute("SELECT body FROM analysis WHERE kind=? AND slug=''", (AnalysisKind.FUND_FACTS,)).fetchone()
    return json.loads(row[0]) if row else None


def fetch(db, only: list[int] | None = None, max_age_days: float = MAX_AGE_DAYS,
          min_gap: float = 0.5) -> str:
    cache = cache_dir("fund_facts")
    funds = {str(code): json.loads(meta).get("groww_id") for code, meta in
             db.execute("SELECT scheme_code, meta FROM fund")}
    funds = {code: slug for code, slug in funds.items()
             if slug and (not only or int(code) in {int(c) for c in only})}
    def stale(path: Path) -> bool:
        return not path.exists() or time.time() - path.stat().st_mtime > max_age_days * 86400

    # A .gone marker is a page Groww no longer serves; it is retried on the same weekly schedule.
    due = [code for code in funds if stale(cache / f"{code}.json.gz") and stale(cache / f"{code}.gone")]

    failed: list[str] = []
    gone = 0
    with egress.rotation(min_gap=min_gap, user_agent=UA, quiet=True) as pool:
        for code in due:
            try:
                body = pool.open(URL.format(slug=funds[code]), accept="application/json")
                page = json.loads(body)
                if not isinstance(page, dict) or "scheme_code" not in page:
                    raise ValueError("not a scheme page")
            except (RuntimeError, ValueError) as exc:
                # Groww answers a retired slug with a 308 and no Location; treat it like a 404.
                # A 308 redirect loop carries a different message and stays a failure.
                if isinstance(exc, egress.NotFound) or re.search(
                        r"HTTP ?Error 308: '?Permanent Redirect", str(exc)):
                    (cache / f"{code}.gone").touch()
                    gone += 1
                else:
                    failed.append(f"{code}: {exc}")
                continue
            (cache / f"{code}.json.gz").write_bytes(gzip.compress(body))
        requests = pool.requests

    if due and len(failed) > max(5, MAX_FAILED * len(due)):
        raise RuntimeError(f"fund_facts: {len(failed)} of {len(due)} pages failed ({failed[0]}); "
                           "the stored document is kept")
    if only:
        return f"fund_facts: {requests} pages fetched for {len(funds)} fund(s), document not rebuilt"

    pages, newest = {}, 0.0
    for code in funds:
        path = cache / f"{code}.json.gz"
        page = read_page(path) if path.exists() else None
        if page:
            pages[code] = page
            newest = max(newest, path.stat().st_mtime)
    previous = stored(db)
    retrieved = datetime.fromtimestamp(newest, UTC).date().isoformat() if newest else None
    body = document(pages, mixes(db), retrieved, previous,
                    f"the Groww scheme pages ({URL}) and the Kuvera portfolios in the holding table")
    floor = int(MIN_SHARE * ((previous or {}).get("meta") or {}).get("funds", 0))
    if body["meta"]["funds"] < floor:
        raise RuntimeError(f"fund_facts: only {body['meta']['funds']} funds with Groww facts against "
                           f"{floor} needed; the stored document is kept")
    text = json.dumps(body, separators=(",", ":"))
    with writing(db):
        db.execute("INSERT OR REPLACE INTO analysis (kind, slug, as_of, body) VALUES (?,'',?,?)",
                   (AnalysisKind.FUND_FACTS, body["meta"]["retrieved"], text))
    return (f"fund_facts: {body['meta']['funds']:,} funds, {body['meta']['with_portfolio_mix']} portfolio"
            f" mixes, {requests} of {len(due)} due pages fetched"
            + (f", {gone} gone from Groww" if gone else "")
            + (f", {len(failed)} failed" if failed else ""))
