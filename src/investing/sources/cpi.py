"""Consumer-price inflation as a NAV-shaped index, so a page can plot a fund's return against it.

Source: the World Bank's FP.CPI.TOTL.ZG series (annual CPI inflation, %). Each year's rate is
compounded evenly across its months from 100 in January FROM_YEAR, so month-level values are coarse.

A year not yet published reuses the latest rate and is flagged `assumed`; the month in progress is
prorated by days elapsed and flagged `partial`. Both flags are in meta.

Each run rewrites the whole series, since a newly published year moves every later point.
"""
import json
import urllib.request
from datetime import date

from ..core.clock import month_ends, today_ist
from ..core.enums import SeriesKind
from . import UA, store_series

URL = "https://api.worldbank.org/v2/country/{iso3}/indicator/FP.CPI.TOTL.ZG?format=json&per_page=200"
PAGE = "https://data.worldbank.org/indicator/FP.CPI.TOTL.ZG?locations={iso2}"
FROM_YEAR = 2000        # the base: January of this year reads 100
MIN_RATES = 20          # fewer years than this is a truncated or errored answer

COUNTRIES = {           # series name -> (ISO3, label)
    "cpi-india": ("IND", "India CPI inflation (World Bank, MOSPI CPI)"),
    "cpi-usa": ("USA", "US CPI inflation (World Bank, BLS CPI)"),
}


def levels(rates: dict[int, float], from_year: int, today: date) -> tuple[dict[date, float], dict]:
    """Month-end index levels and the flags for the tail, from a base of 100 at January `from_year`.

    The month in progress is dated today and prorated by days elapsed."""
    latest = max(rates)
    series: dict[date, float] = {}
    level, assumed_from, partial_day = 100.0, None, None
    for last_of_month in month_ends(today, (today.year - from_year) * 12 + today.month):
        y, m = last_of_month.year, last_of_month.month
        rate = rates.get(y, rates[latest])
        monthly = (1 + rate / 100) ** (1 / 12)
        if y not in rates and assumed_from is None:
            assumed_from = date(y, m, 1).isoformat()
        if last_of_month <= today:
            level *= monthly
            series[last_of_month] = level
        else:
            series[today] = level * monthly ** (today.day / last_of_month.day)
            partial_day = today.isoformat()
    return series, {"assumed_from": assumed_from, "partial_day": partial_day}


def rates_of(iso3: str) -> dict[int, float]:
    request = urllib.request.Request(URL.format(iso3=iso3), headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not (isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], list)):
        raise RuntimeError(f"{iso3}: unexpected World Bank reply; nothing written")
    rates = {int(row["date"]): float(row["value"])
             for row in payload[1] if row.get("value") is not None}
    if len(rates) < MIN_RATES:
        raise RuntimeError(f"{iso3}: only {len(rates)} annual rates from the World Bank; nothing written")
    return rates


def fetch_one(db, name: str, iso3: str, label: str, from_year: int = FROM_YEAR,
              today: date | None = None) -> int:
    today = today or today_ist()
    rates = rates_of(iso3)
    latest = max(rates)
    series, flags = levels(rates, from_year, today)

    # The partial point is dated today; replace everything after the last completed month end.
    last_complete = max(d for d in series if d != today) if len(series) > 1 else today

    return store_series(db, name, SeriesKind.CPI, series, {
        "scheme_name": label, "index": f"CPI {name.removeprefix('cpi-').upper()}",
        "source": URL.format(iso3=iso3), "source_page": PAGE.format(iso2=iso3[:2]),
        "description": f"annual CPI inflation compounded monthly, 100 at January {from_year}",
        "latest_published_year": latest,
        "annual_inflation_pct": {str(y): rates[y] for y in sorted(rates) if y >= from_year},
        "note": f"years after {latest} carry {latest}'s rate ({rates[latest]:.2f}%) until the World "
                f"Bank publishes them; the last point is prorated by days elapsed in the month",
        **flags}, replace_after=last_complete)


def fetch(db, only: list[str] | None = None, from_year: int = FROM_YEAR) -> str:
    done, rows = [], 0
    for name, (iso3, label) in COUNTRIES.items():
        if only is not None and name not in only:
            continue
        n = fetch_one(db, name, iso3, label, from_year=from_year)
        done.append(name)
        rows += n
    return f"cpi: {len(done)} series ({', '.join(done)}), {rows:,} month-ends"
