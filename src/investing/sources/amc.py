"""Monthly portfolio disclosures from AMC archives, for holdings history older than Kuvera's.

  ppfas   one xlsx per scheme since Nov 2022, one combined workbook from Jun 2013 to Oct 2022
  nippon  one combined workbook per month, ~100 sheets plus an INDEX sheet, from Jan 2013

Each sheet is the standard SEBI statement. Rows with an ISIN are kept; only rows in an equity
section are marked Equity, the rest get an empty asset class, as the Kuvera rows do.
"""
import html.parser
import io
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from ..core.enums import Plan
from ..db import day, writing
from . import UA, cache_dir

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
ISIN = re.compile(r"^IN[EF][0-9A-Z]{9}$|^[A-Z]{2}[0-9A-Z]{9}\d$")
NOISE = re.compile(r"\b(direct|regular|plan|growth|option|fund|scheme|the|of|an|a|open|ended|equity|"
                   r"oriented|formerly|known|as|erstwhile)\b")

# Scheme renames and house acquisitions, so an old sheet still matches today's scheme name.
ALIASES = [(re.compile(r"\breliance\b"), "nippon india"), (re.compile(r"\bppfas\b"), "parag parikh"),
           (re.compile(r"\bname of the scheme\b"), " "),
           (re.compile(r"\blong term (equity|value)\b"), "flexi cap"),
           (re.compile(r"\btax ?saver\b|\btax savings?\b|\btax shield\b|\bsaving scheme\b"), "elss"),
           (re.compile(r"\bmulti cap\b"), "multicap"), (re.compile(r"\bflexi cap\b"), "flexicap"),
           (re.compile(r"\bsmall cap\b"), "smallcap"), (re.compile(r"\bmid cap\b"), "midcap"),
           (re.compile(r"\blarge cap\b"), "largecap"), (re.compile(r"\bbluechip\b"), "largecap"),
           (re.compile(r"\bgrowth fund\b"), "midcap")]   # Nippon India Growth is the house's mid cap

AMCS = {
    "ppfas": {"house": "PPFAS Mutual Fund",
              "index": "https://amc.ppfas.com/downloads/portfolio-disclosure/",
              "base": "https://amc.ppfas.com"},
    "nippon": {"house": "Nippon India Mutual Fund",
               "index": "https://mf.nipponindiaim.com/investor-service/downloads/"
                        "factsheet-portfolio-and-other-disclosures",
               "base": "https://mf.nipponindiaim.com"},
}
CACHE = cache_dir("amc-portfolios")


def get(url: str, cache: Path | None = None, tries: int = 3, validate=None) -> bytes:
    """Download, optionally through a cache. The cache file is written only once `validate` accepts it."""
    if cache and cache.exists() and cache.stat().st_size > 0:
        return cache.read_bytes()
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                # Same suffix as the target: the readers pick a parser by extension.
                tmp = cache.with_name(f".{cache.stem}.tmp{cache.suffix}")
                try:
                    tmp.write_bytes(data)
                    if validate is not None:
                        validate(tmp)
                    os.replace(tmp, cache)
                finally:
                    tmp.unlink(missing_ok=True)
            return data
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url}: {last!r}")


def _require_sheets(path: Path) -> None:
    """Raise ValueError when the file has no readable sheet, for example an HTML error page."""
    if not read_workbook(path):
        raise ValueError(f"{path.name}: no readable sheets")


class _TableParser(html.parser.HTMLParser):
    """Rows of every <table> in an HTML file saved with an .xls extension."""

    def __init__(self) -> None:
        super().__init__()
        self.sheets: list[tuple[str, list[list[str]]]] = []
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.rows = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table":
            self.sheets.append((f"table{len(self.sheets) + 1}", self.rows))

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def read_workbook(path: Path) -> list[tuple[str, list[list[str]]]]:
    """[(sheet name, rows as strings)] for xls, xlsx, or HTML pretending to be xls."""
    head = path.read_bytes()[:8]
    if head.startswith(b"PK"):                   # an xlsx saved as .xls
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(path.read_bytes()), read_only=True, data_only=True)
        return [(ws.title, [["" if v is None else str(v) for v in r]
                            for r in ws.iter_rows(values_only=True)]) for ws in wb.worksheets]
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        import xlrd
        wb = xlrd.open_workbook(path, on_demand=True)
        out = []
        for name in wb.sheet_names():
            sh = wb.sheet_by_name(name)
            rows = []
            for r in range(sh.nrows):
                row = []
                for c in range(sh.ncols):
                    v = sh.cell_value(r, c)
                    row.append(str(v) if not isinstance(v, float)
                               else (str(int(v)) if v.is_integer() and abs(v) > 1e6 else repr(v)))
                rows.append(row)
            out.append((name, rows))
            wb.unload_sheet(name)
        return out
    parser = _TableParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    return parser.sheets


def month_from_text(text: str) -> str | None:
    """YYYY-MM out of the many date spellings AMCs use in file names and "as on" lines."""
    t = text.lower().replace("%20", " ").replace("_", " ").replace("-", " ").replace(".", " ")
    m = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*([a-z]{3,9})\s*,?\s*(\d{2,4})\b", t)
    if m and m.group(2)[:3] in MONTHS:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        return f"{y:04d}-{MONTHS[m.group(2)[:3]]:02d}"
    m = re.search(r"\b([a-z]{3,9})\s*(\d{1,2})?\s*,?\s*(\d{2,4})\b", t)
    if m and m.group(1)[:3] in MONTHS and (m.group(2) or len(m.group(3)) in (2, 4)):
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        if 2005 <= y <= 2100:
            return f"{y:04d}-{MONTHS[m.group(1)[:3]]:02d}"
    m = re.search(r"\b(\d{2})\s?(\d{2})\s?(\d{4})\b", t)      # 30062022, 30-04-2021, 31.12.2017
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(3)}-{int(m.group(2)):02d}"
    return None


def discover(amc: str) -> dict[str, list[tuple[str, str]]]:
    """month -> [(label, url)]; several files per month in PPFAS' per-scheme era."""
    spec = AMCS[amc]
    page = get(spec["index"]).decode("utf-8", "replace")
    out: dict[str, list[tuple[str, str]]] = {}
    for href in re.findall(r'href="([^"]+\.(?:xlsx?|XLSX?))(?:\?[^"]*)?"', page):
        # The label names a cache file, so it must stay a plain file name.
        name = Path(urllib.parse.unquote(href.rsplit("/", 1)[-1])).name
        if name in ("", ".", "..") or "/" in name or "\\" in name or "\0" in name:
            continue
        low = name.lower()
        if amc == "ppfas" and "portfolio" not in low and "monthly" not in low:
            continue
        if amc == "nippon" and ("monthly" not in low
                                or re.search(r"debt|fortnight|riskometer|etf|liquid|overnight", low)):
            continue
        month = month_from_text(name)
        if not month:
            continue
        out.setdefault(month, []).append(
            (name, href if href.startswith("http") else spec["base"] + href))
    return out


def parse_sheet(rows: list[list[str]]) -> tuple[str | None, str | None, list[dict]]:
    """(scheme title, as-on month, holdings) from one SEBI-format sheet."""
    title = as_on = header = None
    for i, row in enumerate(rows[:40]):
        cells = [c.strip() for c in row]
        joined = " ".join(cells).lower()
        if title is None:
            for k, c in enumerate(cells):
                low = c.lower()
                if low.startswith("scheme name"):
                    rest = ((c.split(":", 1)[1].strip() if ":" in c else "")
                            or next((x for x in cells[k + 1:] if x), ""))
                    if rest:
                        title = re.sub(r"\s*\(.*$", "", rest).strip()
                        break
                if len(c) > 12 and re.search(r"\bfund\b|\bscheme\b", c, re.I) and not re.search(
                        r"statement|name of the instrument|mutual fund$|asset management|trustee", low):
                    title = re.sub(r"\s*\(.*$", "", c).strip()
                    break
        if as_on is None and "as on" in joined:
            as_on = month_from_text(joined)
        if any("isin" in c.lower() for c in cells) and any(
                "%" in c and re.search(r"net|nav|aum", c.lower()) for c in cells):
            header = i
            break
    if header is None:
        return title, as_on, []

    cols = [c.strip().lower() for c in rows[header]]
    cn = next((k for k, c in enumerate(cols) if "instrument" in c or c == "name" or "security" in c), None)
    cp = next((k for k, c in enumerate(cols) if "%" in c and re.search(r"net|nav|aum", c)), None)
    cind = next((k for k, c in enumerate(cols) if "industry" in c or "rating" in c), None)
    if cn is None or cp is None:
        return title, as_on, []

    section = ""
    holdings: list[dict] = []
    for row in rows[header + 1:]:
        cells = [c.strip() for c in row] + [""] * (len(cols) - len(row))
        # The ISIN has its own column in most files, but some older PPFAS sheets fold it under the
        # name header, so take whichever cell looks like one.
        isin = next((c.upper() for c in cells if ISIN.match(c.upper())), "")
        name, pct = cells[cn], cells[cp]
        label = (name or isin or " ".join(cells)).lower()
        if not ISIN.match(isin.upper()):
            # A heading or a total line: track which section the following rows are in.
            if name:
                if re.search(r"equity", label) and not re.search(
                        r"derivative|future|option|linked|total|preference", label):
                    section = "Equity"
                elif re.search(r"debt|money market|derivative|future|option|reit|invit|units|mutual fund|"
                               r"cash|net receivable|treps|repo|margin|others|preference|warrant|rights|"
                               r"bond|exchange traded|etf|commercial paper|certificate", label):
                    section = ""
            continue
        try:
            p = float(pct.replace(",", "").replace("%", "") or 0)
        except ValueError:
            continue
        holdings.append({"company_name": name, "security_asset_class": section,
                         "sector_name": cells[cind] if cind is not None else None,
                         "percentage_to_aum": p, "holding_isin": isin.upper()})
    # Nippon writes fractions rather than percentages.
    total = sum(abs(h["percentage_to_aum"]) for h in holdings)
    if holdings and total <= 1.6:
        for h in holdings:
            h["percentage_to_aum"] = round(h["percentage_to_aum"] * 100, 5)
    return title, as_on, holdings


def canon(name: str) -> set[str]:
    n = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    for pat, rep in ALIASES:
        n = pat.sub(rep, n)
    return {t for t in NOISE.sub(" ", n).split() if t}


def match_scheme(title: str, candidates: list[dict]) -> dict | None:
    want = canon(title)
    if not want:
        return None
    best, score = None, 0.0
    for row in candidates:
        have = canon(row["name"])
        s = len(want & have) / max(len(want | have), 1)
        if s > score:
            best, score = row, s
    return best if score >= 0.6 else None


def month_end(month: str) -> date:
    y, m = (int(x) for x in month.split("-"))
    return date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)


def store(db, scheme_code: int, month: str, holdings: list[dict]) -> int:
    d = day(month_end(month))
    rows = []
    for h in holdings:
        name = h["company_name"]
        pct = h["percentage_to_aum"]
        if not name or pct is None or pct <= 0:
            continue                    # a position the fund has exited, listed at zero
        isin = h["holding_isin"] or None
        rows.append((scheme_code, d, name, isin, None, pct, None, None,
                     h.get("sector_name"), h.get("security_asset_class") or None, None,
                     isin or "", ""))
    if rows:
        db.executemany(
            "INSERT OR REPLACE INTO holding (scheme_code, d, name, isin, company, weight, value, "
            "quantity, sector, asset_class, side, isin_key, side_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
    return len(rows)


def fetch(db, months: int = 24, only: list[str] | None = None) -> str:
    """The most recent `months` disclosures from each AMC archive. Months Kuvera already has are skipped."""
    done = rows = skipped = 0
    failed: list[str] = []
    for amc, spec in AMCS.items():
        if only and amc not in only:
            continue
        candidates = [dict(r) for r in db.execute(
            "SELECT scheme_code, name FROM fund WHERE fund_house=? AND plan=?", (spec["house"], Plan.DIRECT))]
        if not candidates:
            failed.append(f"{amc} (no {spec['house']} funds in the database)")
            continue
        try:
            found = discover(amc)
        except Exception as exc:  # one archive being down does not stop the other
            failed.append(f"{amc} ({exc})")
            continue
        for month in sorted(found, reverse=True)[:months]:
            for label, url in found[month]:
                cache = CACHE / amc / label
                try:
                    get(url, cache=cache, validate=_require_sheets)
                    sheets = read_workbook(cache)
                except Exception:  # one bad file does not stop the run
                    skipped += 1
                    continue
                for _, sheet_rows in sheets:
                    title, as_on, holdings = parse_sheet(sheet_rows)
                    if not title or not holdings:
                        continue
                    scheme = match_scheme(title, candidates)
                    if scheme is None:
                        continue
                    with writing(db):
                        n = store(db, int(scheme["scheme_code"]), as_on or month, holdings)
                    if n:
                        done += 1
                        rows += n
    return (f"amc: {done} portfolios, {rows:,} holdings"
            + (f", {skipped} unreadable file(s)" if skipped else "")
            + (f"; failed: {'; '.join(failed)}" if failed else ""))
