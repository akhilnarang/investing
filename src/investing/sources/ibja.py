"""IBJA's daily gold and silver rates, accumulated from its rolling 30-day PDF.

IBJA's rates (Gold 999 per 10 g, Silver 999 per kg) are what Indian gold and silver ETFs are valued
at. The public site serves only the last 30 days, so the database is the only copy of older days
and new rows are merged into the stored ones. The PDF is cached by fetch day.

The PM rate is stored, as that is the one funds are struck at.
"""
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from datetime import date

from ..core.clock import today_ist
from ..core.enums import SeriesKind
from ..db import undate
from . import UA, cache_dir, store_series

CACHE = cache_dir("ibja")
HOME = "https://ibjarates.com/"
MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
ROW = re.compile(r"^\s*(\d{2})-([A-Za-z]{3})-(\d{2})\s+(.*)$")
MIN_ROWS = 10           # fewer is a layout change or a bad download

METALS = {"ibja-gold": ("gold", "10 g"), "ibja-silver": ("silver", "1 kg")}


def get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def pdf_url() -> str:
    html = get(HOME).decode("utf-8", "replace")
    m = re.search(r'href="([^"]*UploadedFiles/30DaysPdf/[^"]+\.pdf)"', html)
    if not m:
        raise RuntimeError("IBJA home page has no 30-day PDF link today")
    return urllib.parse.urljoin(HOME, m.group(1).replace(" ", "%20"))


def todays_pdf(today: date | None = None) -> bytes:
    today = today or today_ist()
    cached = CACHE / f"{today.isoformat()}.pdf"
    if cached.exists():
        return cached.read_bytes()
    payload = get(pdf_url())
    cached.write_bytes(payload)
    return payload


def header_ok(text: str) -> bool:
    """Whether the purity columns appear in the order the positional parse assumes.

    pdftotext splits the header over several lines; the check uses the one naming Gold 999 and 995."""
    head = text[:4000]
    if "AM" not in head or "PM" not in head or "Silver 999" not in head:
        return False
    for line in head.splitlines():
        if "Gold 999" in line and "Gold 995" in line:
            positions = [line.find(c) for c in
                         ("Gold 999", "Gold 995", "Gold 916", "Gold 750", "Gold 585", "Silver")]
            return all(p >= 0 for p in positions) and positions == sorted(positions)
    return False


def parse(text: str) -> dict[date, dict[str, int]]:
    """day -> {gold_am, gold_pm, silver_am, silver_pm}. The PDF's columns are Gold 999 AM/PM, 995
    AM/PM, 916 AM/PM, 750 AM/PM, 585 AM/PM, Silver 999 AM/PM, in that order, verified by header_ok."""
    if not header_ok(text):
        raise ValueError("IBJA PDF header is not in the expected column order; refusing to parse positionally")
    out: dict[date, dict[str, int]] = {}
    for line in text.splitlines():
        m = ROW.match(line)
        if not m:
            continue
        nums = [int(n) for n in re.findall(r"\b\d{4,7}\b", m.group(4))]
        if len(nums) != 12:
            continue    # weekend rows (SAT / SUN) and other non-rate rows
        month = MONTHS.get(m.group(2).title())
        if not month:
            continue
        try:
            when = date(2000 + int(m.group(3)), month, int(m.group(1)))
        except ValueError:
            continue    # invalid printed date
        if not (0 < nums[0] < 10_000_000 and 0 < nums[11] < 100_000_000):
            continue
        out[when] = {"gold_am": nums[0], "gold_pm": nums[1],
                     "silver_am": nums[10], "silver_pm": nums[11]}
    return out


def existing(db, name: str) -> dict[date, float]:
    return {undate(d): v for d, v in db.execute("SELECT d, v FROM series WHERE name=?", (name,))}


def fetch(db, only: list[str] | None = None, pdf: bytes | None = None) -> str:
    if not shutil.which("pdftotext"):
        raise RuntimeError("pdftotext (poppler) is needed to read IBJA's PDF")
    text = subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf or todays_pdf(),
                          capture_output=True, check=True).stdout.decode("utf-8", "replace")
    rows = parse(text)
    if len(rows) < MIN_ROWS:
        raise RuntimeError(f"only {len(rows)} dated rows from IBJA's PDF; nothing written")

    parts = []
    for name, (metal, unit) in METALS.items():
        if only is not None and name not in only:
            continue
        have = existing(db, name)
        fresh = {d: float(v[f"{metal}_pm"]) for d, v in rows.items()}
        added = sum(1 for d in fresh if d not in have)
        # A later PDF can revise an earlier day; the newer figure wins and is reported.
        revised = sorted(d for d, v in fresh.items() if d in have and have[d] != v)
        merged = {**have, **fresh}
        store_series(db, name, SeriesKind.METAL, merged, {
            "scheme_name": f"IBJA {metal} 999 rate, rupees per {unit}",
            "index": f"IBJA {metal.upper()}", "source": HOME,
            "description": f"IBJA {metal} 999 PM rate, rupees per {unit}",
            "note": "PM rate as the value; accumulated from IBJA's rolling 30-day PDF, so the "
                    "archive starts the day this first ran and cannot be backfilled from IBJA"})
        parts.append(f"{name} {len(merged)} days (+{added}"
                     + (f", {len(revised)} revised" if revised else "") + ")")
    newest = max(rows)
    return f"ibja: {', '.join(parts)}, latest {newest.isoformat()}"
