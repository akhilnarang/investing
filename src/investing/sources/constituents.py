"""Which NSE size index each stock belongs to, from NSE's published constituent lists.

Each list is a CSV with ISIN, symbol and NSE's industry label. Current membership only. One
document per index. Around a rebalance a stock can appear in two lists; a reader flattening to one
index per ISIN should keep the first, which is the broadest.
"""
import csv
import io
import json
import time
import urllib.request

from ..core.clock import today_ist
from ..db import writing
from . import UA, cache_dir

ARCHIVE = "https://nsearchives.nseindia.com/content/indices/{name}.csv"
MIN_ROWS = 40           # a truncated or refused CSV; the smallest of these lists holds 50

# slug -> (label, NSE's file name), broadest first.
LISTS = [
    ("nifty-50", "NIFTY 50", "ind_nifty50list"),
    ("nifty-next-50", "NIFTY NEXT 50", "ind_niftynext50list"),
    ("nifty-midcap-150", "NIFTY MIDCAP 150", "ind_niftymidcap150list"),
    ("nifty-smallcap-250", "NIFTY SMALLCAP 250", "ind_niftysmallcap250list"),
    ("nifty-microcap-250", "NIFTY MICROCAP 250", "ind_niftymicrocap250_list"),
]


def members(text: str, slug: str, label: str) -> dict[str, dict]:
    """One CSV into {ISIN: member}. Headers may have stray spaces; the column is "ISIN Code" or "ISIN"."""
    out: dict[str, dict] = {}
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        isin = row.get("ISIN Code") or row.get("ISIN")
        if not isin or isin in out:
            continue
        out[isin] = {"index": slug, "index_name": label, "symbol": row.get("Symbol", ""),
                     "industry": row.get("Industry", "")}
    return out


def get(name: str, timeout: int = 60) -> str:
    request = urllib.request.Request(ARCHIVE.format(name=name), headers={
        "User-Agent": UA, "Referer": "https://www.nseindia.com/"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def fetch(db, pause: float = 1.0) -> str:
    today = today_ist().isoformat()
    documents: list[tuple[str, str]] = []
    counts: dict[str, int] = {}
    for slug, label, name in LISTS:
        text = get(name)
        (cache_dir("constituents") / f"{slug}.csv").write_text(text, encoding="utf-8")
        found = members(text, slug, label)
        if len(found) < MIN_ROWS:
            raise RuntimeError(f"{name}: only {len(found)} rows parsed; nothing written")
        counts[slug] = len(found)
        documents.append((slug, json.dumps(
            {"index": slug, "index_name": label, "retrieved": today, "source": ARCHIVE.format(name=name),
             "count": len(found), "members": found}, ensure_ascii=False)))
        time.sleep(pause)

    with writing(db):
        for slug, body in documents:
            db.execute("INSERT OR REPLACE INTO analysis (kind, slug, as_of, body) "
                       "VALUES ('index_constituents',?,?,?)", (slug, today, body))
    return (f"constituents: {sum(counts.values())} members across {len(counts)} indices ("
            + ", ".join(f"{k} {v}" for k, v in counts.items()) + ")")

