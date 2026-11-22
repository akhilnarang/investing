"""/summary: each category's median fund against its benchmark, one table per plan.

Reads the `longterm` documents, plus `active_share` and `attribution` where they exist (domestic equity).
"""
import re
import sqlite3
from collections import Counter
from statistics import median

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...core.enums import AnalysisKind, Plan
from .. import errors
from .. import queries as q
from ..pages import DB, page

router = APIRouter(include_in_schema=False)

WORDS = {3: "three", 5: "five", 7: "seven", 10: "ten", 15: "fifteen", 20: "twenty"}
# Derived from AMFI categories: sectoral and international funds are filed under Equity.
GROUPS = ("Equity", "Sector", "Hybrid", "Abroad", "Commodities", "Debt")


def _group(doc: dict) -> str:
    cat, sub = Counter((f.get("category"), f.get("sub_category")) for f in doc["funds"]).most_common(1)[0][0]
    if sub == "Sectoral":
        return "Sector"
    if sub == "International":
        return "Abroad"
    return cat if cat in GROUPS else "Other"


def _short(name: str) -> str:
    """Title-case NSE's upper-case index names: "NIFTY 1D RATE INDEX TRI" -> "Nifty 1D Rate Index"."""
    name = name.replace(" TRI", "")
    if name.isupper():
        name = " ".join("IT" if w == "IT" else w.capitalize() for w in name.split())
        name = (name.replace("Largemidcap", "LargeMidcap").replace("1d Rate", "1D Rate")
                .replace("G-sec", "G-Sec").replace(" Yr ", " yr "))
    return name.strip()


def _med(values) -> float | None:
    vals = [v for v in values if v is not None]
    return median(vals) if vals else None


def _cagr(f: dict, h: int) -> float | None:
    # Gate on the fund's age: nav.cagr accepts a start NAV up to 12 days inside the window.
    return (f.get("cagr") or {}).get(str(h)) if (f.get("history_years") or 0) >= h else None


def _attribution(db: sqlite3.Connection, slug: str) -> dict | None:
    """The longest-window attribution document for this category and plan ('flexi-cap-10y')."""
    pat = re.compile(rf"^{re.escape(slug)}-(\d+)y$")
    found = [(int(m[1]), e["slug"]) for e in q.analysis_slugs(db, AnalysisKind.ATTRIBUTION) if (m := pat.match(e["slug"]))]
    return q.analysis(db, AnalysisKind.ATTRIBUTION, max(found)[1]) if found else None


def _row(db: sqlite3.Connection, slug: str, doc: dict, base: str, share: dict | None) -> dict:
    funds = doc["funds"]
    active = [f for f in funds if not f.get("passive")]
    all_passive = not active
    if all_passive:  # an all-index category compares the index products themselves
        active = list(funds)
    bench = (doc.get("bench_stats") or {}).get("cagr") or {}
    # The longest horizon the benchmark and at least one fund both reach.
    h = next((h for h in reversed(doc.get("horizons") or [])
              if bench.get(str(h)) is not None and any(_cagr(f, h) is not None for f in active)), None)
    bl = bench.get(str(h)) if h else None
    # Funds younger than the horizon are left out.
    cagrs = [c for f in active if h and (c := _cagr(f, h)) is not None]
    codes = {str(f.get("direct_code") or f["scheme_code"]) for f in active}
    attribution = _attribution(db, slug)
    med_cagr = _med(cagrs)
    benchmark = doc.get("benchmark")
    bench_name = benchmark.get("name", "") if isinstance(benchmark, dict) else (benchmark or "")
    return {
        "slug": slug, "base": base, "label": doc.get("sub_category") or base, "group": _group(doc),
        "horizon": h, "funds": len(cagrs), "all_passive": all_passive,
        "median_cagr": med_cagr, "bench_cagr": bl, "bench": _short(bench_name),
        "excess": med_cagr - bl if med_cagr is not None and bl is not None else None,
        "beat": sum(c > bl for c in cagrs) / len(cagrs) if cagrs and bl is not None else None,
        "hit": _med((f.get("vs_benchmark_5y") or {}).get("hit_rate") for f in active),
        "active_share": _med(v.get("active_share_pct") for k, v in ((share or {}).get("funds") or {}).items()
                             if str(k) in codes),
        "selection": _med(v.get("selection_pp") for k, v in ((attribution or {}).get("funds") or {}).items()
                          if str(k) in codes),
        "selection_years": (attribution or {}).get("years"),
        "as_of": doc.get("as_of"), "sources": doc.get("sources") or [],
    }


def _modal(rows: list[dict]) -> int:
    """The horizon most rows share, longer on a tie."""
    hs = [r["horizon"] for r in rows if r["horizon"]]
    return max(set(hs), key=lambda h: (hs.count(h), h)) if hs else 10


def _table(rows: list[dict], plan: Plan) -> dict | None:
    if not rows:
        return None
    rows.sort(key=lambda r: (GROUPS.index(r["group"]) if r["group"] in GROUPS else len(GROUPS), r["label"]))
    h = _modal(rows)
    return {"plan": plan, "rows": rows, "horizon": h, "words": WORDS.get(h, str(h))}


@router.get("/summary", response_class=HTMLResponse)
def summary(request: Request, db: sqlite3.Connection = DB) -> HTMLResponse:
    plans: dict[str, list[dict]] = {Plan.DIRECT: [], Plan.REGULAR: []}
    for entry in q.analysis_slugs(db, AnalysisKind.LONGTERM):
        slug = entry["slug"]
        doc = q.analysis(db, AnalysisKind.LONGTERM, slug)
        if not doc or not doc.get("funds"):
            continue
        plan = doc.get("plan") or Plan.DIRECT
        base = slug.removesuffix("-regular")
        share = q.analysis(db, AnalysisKind.ACTIVE_SHARE, base)
        plans.setdefault(plan, []).append(_row(db, slug, doc, base, share))
    direct = _table(plans[Plan.DIRECT], Plan.DIRECT)
    if direct is None:
        raise errors.not_found("category comparison")
    tables = [t for t in (direct, _table(plans[Plan.REGULAR], Plan.REGULAR)) if t]

    rows = direct["rows"]
    groups = list(dict.fromkeys(r["group"] for r in rows))
    with_h = [r for r in rows if r["horizon"] == direct["horizon"] and r["excess"] is not None]
    seen: dict[str, dict] = {}
    for t in tables:
        for r in t["rows"]:
            for s in r["sources"]:
                if s.get("name"):
                    seen.setdefault(s["name"], s)
    # Benchmarks named per group.
    benches = {g: list(dict.fromkeys(r["bench"] for r in rows if r["group"] == g and r["bench"])) for g in groups}
    sel_years = next((r["selection_years"] for r in rows if r["selection_years"]), 10)
    return page(request, db, "summary.html", "summary",
                tables=tables, groups=groups, benches=benches, sel_years=sel_years,
                as_of=max(r["as_of"] for r in rows if r["as_of"]),
                funds=sum(r["funds"] for r in rows if r["horizon"] == direct["horizon"]),
                young=sum(r["horizon"] != direct["horizon"] for r in rows),
                beat=sum(r["excess"] > 0 for r in with_h), with_h=len(with_h),
                best=max(with_h, key=lambda r: r["excess"]) if with_h else None,
                worst=min(with_h, key=lambda r: r["excess"]) if with_h else None,
                # Only https links; the URLs come from upstream metadata.
                sources=[{**s, "url": s["url"] if re.match(r"^https://[^\s\"'<>]+$", str(s.get("url") or "")) else None}
                         for s in seen.values()])
