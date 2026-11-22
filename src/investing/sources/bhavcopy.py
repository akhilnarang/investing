"""Daily closes for every NSE-listed stock, from NSE's own bhavcopy archives, keyed by ISIN.

Two archive formats: UDiFF from NEW_FORMAT_FROM and the older cmDDMONYYYY file. Cash-market equity
series are kept (EQ, BE, BZ, SZ, and SME series SM, ST), which includes ETFs. Bonds, REITs, InvITs
and rights entitlements (-RE) are not.

Closes are as traded; pages adjust for splits and bonuses with the actions from `events.py`.
"""
import csv
import io
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta

from ..core.clock import today_ist
from ..db import day, undate, writing
from . import UA, cache_dir

CACHE = cache_dir("bhavcopy")
NEW_FORMAT_FROM = date(2024, 7, 8)   # NSE's UDiFF format
KEEP_SERIES = {"EQ", "BE", "BZ", "SZ", "SM", "ST"}
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
MIN_ROWS = 500          # fewer rows means a WAF page or a truncated download


def urls_for(d: date) -> list[str]:
    new = (f"https://nsearchives.nseindia.com/content/cm/"
           f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
    old = (f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{d.year}/"
           f"{MONTHS[d.month - 1]}/cm{d.day:02d}{MONTHS[d.month - 1]}{d.year}bhav.csv.zip")
    return [new, old] if d >= NEW_FORMAT_FROM else [old, new]


def fetch_day(d: date, pause: float = 0.4):
    """The day's zip, from the cache or NSE; None on a holiday (404 from both archives).

    A day is recorded as a holiday only once it is three days old and both archives answered 404,
    since NSE publishes late and its WAF sometimes refuses."""
    cached = CACHE / f"{d.isoformat()}.csv.zip"
    if cached.exists():
        return cached
    miss = CACHE / f"{d.isoformat()}.none"
    if miss.exists():
        return None
    confirmed = True
    for url in urls_for(d):
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": UA, "Referer": "https://www.nseindia.com/"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    payload = r.read()
                if len(payload) < 2000:
                    confirmed = False
                    break
                # Cache only a real zip.
                tmp = cached.with_suffix(".part")
                tmp.write_bytes(payload)
                if not zipfile.is_zipfile(tmp):
                    tmp.unlink(missing_ok=True)
                    confirmed = False
                    break
                tmp.replace(cached)
                time.sleep(pause)
                return cached
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    break
                confirmed = False
                if e.code == 403:
                    break
                time.sleep(3 * (attempt + 1))
            except Exception:  # any transport failure leaves the day for the next run
                confirmed = False
                time.sleep(3 * (attempt + 1))
        time.sleep(pause)
    if confirmed and d <= today_ist() - timedelta(days=3):
        miss.touch()
    return None


def rows_of(zpath) -> tuple[list[tuple[str, str, str, str, float]], date | None]:
    """(isin, symbol, series, name, close) for one day's equity rows, and the date the file names."""
    with zipfile.ZipFile(zpath) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    out: list[tuple[str, str, str, str, float]] = []
    file_day: date | None = None
    for raw in csv.DictReader(io.StringIO(text)):
        r = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if "TckrSymb" in r:                                  # UDiFF
            if file_day is None and r.get("TradDt"):
                try:
                    file_day = date.fromisoformat(r["TradDt"])
                except ValueError:
                    pass
            if r.get("Sgmt") != "CM" or r.get("SctySrs") not in KEEP_SERIES:
                continue
            isin, sym, ser, nm, close = (r.get("ISIN", ""), r.get("TckrSymb", ""),
                                         r.get("SctySrs", ""), r.get("FinInstrmNm", ""),
                                         r.get("ClsPric", ""))
        else:                                                # old cm*bhav
            if file_day is None and r.get("TIMESTAMP"):
                try:
                    file_day = datetime.strptime(r["TIMESTAMP"], "%d-%b-%Y").date()
                except ValueError:
                    pass
            if r.get("SERIES") not in KEEP_SERIES:
                continue
            isin, sym, ser, nm, close = (r.get("ISIN", ""), r.get("SYMBOL", ""),
                                         r.get("SERIES", ""), "", r.get("CLOSE", ""))
        if sym.endswith("-RE"):
            continue
        try:
            c = float(close)
        except ValueError:
            continue
        if isin and sym and c > 0:
            out.append((isin, sym, ser, nm, c))
    return out, file_day


def fetch(db, since: date | None = None, until: date | None = None, pause: float = 0.4) -> str:
    """Every trading day from the last one in the database up to today."""
    if since is None:
        last = db.execute("SELECT MAX(d) FROM stock_price").fetchone()[0]
        since = undate(last) + timedelta(days=1) if last is not None else date(2016, 4, 1)
    until = until or today_ist()

    days = written = skipped = 0
    d = since
    while d <= until:
        if d.weekday() == 6:                 # no Sunday sessions
            d += timedelta(days=1)
            continue
        z = fetch_day(d, pause)
        if z is None:
            d += timedelta(days=1)
            continue
        try:
            rows, file_day = rows_of(z)
        except zipfile.BadZipFile:
            z.unlink(missing_ok=True)        # refetched on the next run
            skipped += 1
            d += timedelta(days=1)
            continue
        # A short file or one naming another date is left for the next run.
        if len(rows) < MIN_ROWS or (file_day is not None and file_day != d):
            z.unlink(missing_ok=True)
            skipped += 1
            d += timedelta(days=1)
            continue
        n = day(d)
        with writing(db):
            db.executemany(
                "INSERT OR IGNORE INTO company (isin, symbol, name) VALUES (?,?,?)",
                [(isin, sym, nm or sym) for isin, sym, _, nm, _ in rows])
            # A symbol or name can change while the ISIN stays; the newest file wins.
            db.executemany("UPDATE company SET symbol=?, name=COALESCE(NULLIF(?,''), name) WHERE isin=?",
                           [(sym, nm, isin) for isin, sym, _, nm, _ in rows])
            db.executemany("INSERT OR REPLACE INTO stock_price (isin, d, close) VALUES (?,?,?)",
                           [(isin, n, c) for isin, _, _, _, c in rows])
        days += 1
        written += len(rows)
        d += timedelta(days=1)
    return (f"bhavcopy: {days} trading days, {written:,} closes"
            + (f", {skipped} day(s) left for the next run" if skipped else ""))
