"""Currency series from the ECB's daily reference rates, plus the dollar index.

DXY is computed from ICE's public formula: a geometric mean of six currencies against the dollar,
scaled by 50.14348112. It differs from ICE's print by a few hundredths, since the ECB fixes once a
day. EUR and GBP enter as dollars per unit (negative exponent), the rest as units per dollar.
"""
import csv
import io
import time
import urllib.error
import urllib.request
from datetime import date

from ..core.enums import SeriesKind
from ..db import undate
from . import UA, store_series

ECB = ("https://data-api.ecb.europa.eu/service/data/EXR/D.{ccy}.EUR.SP00.A"
       "?format=csvdata&startPeriod=1999-01-01")

# ICE's published basket: currency -> (weight, sign).
DXY_BASKET = {"EUR": (0.576, -1), "JPY": (0.136, 1), "GBP": (0.119, -1),
              "CAD": (0.091, 1), "SEK": (0.042, 1), "CHF": (0.036, 1)}
DXY_SCALE = 50.14348112

PAIRS = {   # file stem -> (currency, label); rupees per unit of that currency
    "usd-inr": ("USD", "US dollar in rupees"),
    "eur-inr": ("EUR", "Euro in rupees"),
    "gbp-inr": ("GBP", "Pound sterling in rupees"),
    "jpy-inr": ("JPY", "Japanese yen in rupees"),
    "sgd-inr": ("SGD", "Singapore dollar in rupees"),
    "aud-inr": ("AUD", "Australian dollar in rupees"),
    "cad-inr": ("CAD", "Canadian dollar in rupees"),
    "chf-inr": ("CHF", "Swiss franc in rupees"),
    "cny-inr": ("CNY", "Chinese yuan in rupees"),
    "hkd-inr": ("HKD", "Hong Kong dollar in rupees"),
}

# The ECB does not publish these; they are dollar pegs, so rupees per unit is USD/INR over the peg.
PEGGED = {
    "aed-inr": ("AED", "UAE dirham in rupees", 3.6725, "pegged to the US dollar since 1997"),
    "sar-inr": ("SAR", "Saudi riyal in rupees", 3.75, "pegged to the US dollar since 1986"),
}

MIN_ROWS = 5000        # refuse to write a series shorter than this


def ecb(ccy: str) -> dict[date, float]:
    """ECB reference rate, units of `ccy` per euro, by day.

    A 5xx, connection error or timeout is retried; a 4xx is not."""
    request = urllib.request.Request(ECB.format(ccy=ccy), headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=120) as r:
                text = r.read().decode("utf-8")
            break
        except (urllib.error.URLError, TimeoutError) as e:
            permanent = isinstance(e, urllib.error.HTTPError) and e.code < 500
            if attempt == 2 or permanent:
                raise RuntimeError(f"ECB {ccy} rates unavailable: {e}") from e
            time.sleep(30 * (attempt + 1))
    out: dict[date, float] = {}
    for row in csv.DictReader(io.StringIO(text)):
        try:
            out[date.fromisoformat(row["TIME_PERIOD"])] = float(row["OBS_VALUE"])
        except (ValueError, KeyError):
            continue
    return out


def dxy(quotes: dict[str, dict[date, float]]) -> dict[date, float]:
    usd = quotes["USD"]
    out: dict[date, float] = {}
    for d, usd_per_eur in usd.items():
        if usd_per_eur <= 0:
            continue
        product = DXY_SCALE
        for ccy, (weight, sign) in DXY_BASKET.items():
            if ccy == "EUR":
                rate = usd_per_eur                       # EURUSD, already dollars per euro
            else:
                q = quotes[ccy].get(d)
                if not q or q <= 0:
                    product = None
                    break
                per_usd = q / usd_per_eur                 # ECB quotes per euro; make it per dollar
                rate = per_usd if sign > 0 else 1 / per_usd   # GBP enters as GBPUSD, not USDGBP
            product *= rate ** (sign * weight)
        if product is not None:
            out[d] = product
    return out


def fetch(db, min_rows: int = MIN_ROWS) -> str:
    need = ({"INR", "USD"} | {c for c, _ in PAIRS.values()} | set(DXY_BASKET)) - {"EUR"}
    quotes = {c: ecb(c) for c in sorted(need)}          # units of c per euro, by day
    inr = quotes["INR"]

    written = 0
    for stem, (ccy, label) in PAIRS.items():
        other = quotes.get(ccy)                          # EUR has no per-euro series: it is 1
        series = ({d: inr[d] for d in inr} if other is None else
                  {d: inr[d] / other[d] for d in inr if d in other and other[d] > 0})
        if len(series) < min_rows:
            raise RuntimeError(f"{stem}: only {len(series)} days from the ECB; nothing written")
        store_series(db, stem, SeriesKind.FX, series, {
            "scheme_name": label, "index": f"{ccy} INR", "kind": "price_index",
            "description": f"rupees per {ccy}, ECB reference rates", "source": ECB.format(ccy=ccy)})
        written += 1

    for stem, (ccy, label, peg, note) in PEGGED.items():
        series = {d: inr[d] / usd_per_eur / peg for d, usd_per_eur in quotes["USD"].items()
                  if d in inr and usd_per_eur > 0}
        store_series(db, stem, SeriesKind.FX, series, {
            "scheme_name": label, "index": f"{ccy} INR", "kind": "price_index",
            "description": f"rupees per {ccy}, USD/INR divided by the {peg} peg",
            "source": ECB.format(ccy="USD"), "peg": peg,
            "note": f"{ccy} is {note}, so this is exact rather than a cross rate"})
        written += 1

    index = dxy(quotes)
    if len(index) < min_rows:
        raise RuntimeError(f"dxy: only {len(index)} days; nothing written")
    store_series(db, "dxy", SeriesKind.FX, index, {
        "scheme_name": "US Dollar Index (DXY)", "index": "DXY", "kind": "price_index",
        "description": "ICE's published basket and weights, computed from ECB reference rates",
        "source": ECB.format(ccy="USD"),
        "basket": {c: w for c, (w, _) in DXY_BASKET.items()}, "scale": DXY_SCALE,
        "note": "ICE's own level is proprietary and continuous; this is its published formula on "
                "ECB's 14:15 CET fixes, so it runs a few hundredths of a point from the ICE print"})
    written += 1
    return f"fx: {written} series, {len(index):,} DXY days, latest {max(index).isoformat()}"


# Known DXY levels: the April 2008 trough, the January 2002 and September 2022 peaks. A sign or
# inversion error in the basket moves these by ten points or more.
CHECKS = (("2008-04", 71.0, 74.0), ("2002-01", 115.0, 121.0), ("2022-09", 107.0, 115.0))


def check(db) -> str:
    by_month: dict[str, list[float]] = {}
    for d, v in db.execute("SELECT d, v FROM series WHERE name='dxy'"):
        by_month.setdefault(undate(d).strftime("%Y-%m"), []).append(v)
    for month, low, high in CHECKS:
        seen = by_month.get(month) or []
        if not seen:
            raise AssertionError(f"no DXY rows for {month}")
        if not low <= max(seen) <= high:
            raise AssertionError(f"DXY {month}: got {min(seen):.1f}-{max(seen):.1f}, expected {low}-{high}")
    return "dxy self-check passed"
