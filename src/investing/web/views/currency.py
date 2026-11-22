"""The currency calculator and cards on /currency.

A holding is valued nominally, after Indian inflation, and after the home country's inflation where
that series exists, next to Indian cash, the Nifty 500 and gold. Uses month-end points.
"""
import json
import sqlite3
from bisect import bisect_right
from datetime import date

from ...db import undate

# Currencies with a home CPI series loaded.
HOME_CPI = {"usd-inr": "cpi-usa"}
ALTERNATIVES = [("nifty-500", "Nifty 500 TRI"), ("nifty-1d-rate-index", "Overnight rate (cash in India)"),
                ("gold-inr", "Gold in rupees")]


class Monthly:
    """A series reduced to its last value per month. `at` returns the value on or before a month."""

    def __init__(self, rows: list[tuple[int, float]]):
        by_month: dict[str, float] = {}
        for d, v in rows:   # oldest first, so the last write in a month wins
            by_month[undate(d).strftime("%Y-%m")] = v
        self.months = list(by_month)
        self.values = list(by_month.values())

    def at(self, month: str) -> float | None:
        i = bisect_right(self.months, month) - 1
        return self.values[i] if i >= 0 else None

    @property
    def first(self) -> str:
        return self.months[0]


# The refresh writes without restarting the process, so the cache is keyed on each series' newest
# day and row count.
_CACHE: dict[tuple, dict[str, Monthly]] = {}


def _load(db: sqlite3.Connection, names: list[str]) -> dict[str, Monthly]:
    marks = ",".join("?" * len(names))
    key = tuple(tuple(r) for r in db.execute(
        f"SELECT name, MAX(d), COUNT(*) FROM series WHERE name IN ({marks}) GROUP BY name", names))
    if (hit := _CACHE.get(key)) is not None:
        return hit
    rows: dict[str, list] = {n: [] for n in names}
    for name, d, v in db.execute(f"SELECT name, d, v FROM series WHERE name IN ({marks}) ORDER BY name, d",
                                 names):
        rows[name].append((d, v))
    loaded = {n: Monthly(r) for n, r in rows.items() if r}
    _CACHE.clear()
    _CACHE[key] = loaded
    return loaded


def _years(a: str, b: str) -> float:
    return (date.fromisoformat(b + "-01") - date.fromisoformat(a + "-01")).days / 365.25


def _cagr(start: float, end: float, years: float) -> float | None:
    return ((end / start) ** (1 / years) - 1) * 100 if years > 0.5 and start > 0 else None


def label(month: str) -> str:
    return date.fromisoformat(month + "-01").strftime("%b %Y")


def view(db: sqlite3.Connection, amount: float, month: str | None, code: str | None) -> dict:
    metas = {r["name"]: r for r in db.execute(
        "SELECT name, meta FROM series_meta WHERE kind='fx' AND name LIKE '%-inr' ORDER BY name")}
    extra = ["cpi-india", *HOME_CPI.values(), *(n for n, _ in ALTERNATIVES)]
    s = _load(db, [*metas, *extra])
    ccys = []
    for name, row in metas.items():
        if name not in s:
            continue
        m = json.loads(row["meta"])
        ccys.append({"series": name, "code": name[:3].upper(),
                     "name": (m.get("scheme_name") or name).removesuffix(" in rupees"),
                     "peg": m.get("peg")})
    # USD first, the rest alphabetical.
    ccys.sort(key=lambda c: (c["code"] != "USD", c["code"]))
    months = sorted({mo for c in ccys for mo in s[c["series"]].months})
    if len(months) < 2:
        return {"currencies": [], "cards": [], "answer": None}
    first, last = months[0], months[-1]

    span = _years(first, last)
    cards = []
    for c in ccys:
        ser = s[c["series"]]
        then, now = ser.at(first), ser.values[-1]
        cards.append({**c, "now": now, "cagr": _cagr(then, now, span) if then else None})

    pick = next((c for c in ccys if c["code"] == (code or "").upper()), ccys[0])
    month = month or first
    out = {"currencies": ccys, "cards": cards, "first": first, "last": last, "latest_start": months[-2],
           "first_label": label(first), "amount": amount, "month": month, "code": pick["code"]}
    if not first <= month < last:
        out["error"] = f"Pick a month between {label(first)} and {label(months[-2])}."
        return out

    ser = s[pick["series"]]
    rate0, rate1 = ser.at(month), ser.at(last)
    if not rate0 or not rate1:
        out["error"] = f"No {pick['code']} rate for {label(month)}."
        return out
    units = amount / rate0
    now = units * rate1
    years = _years(month, last)

    def row(text: str, value: float | None, note: str | None = None) -> dict:
        return {"label": text, "value": value, "note": note,
                "pct": (value / amount - 1) * 100 if value is not None else None}

    rows = [row("Nominal, in rupees today", now)]
    # Deflate `now` into the starting month's prices.
    for cpi, text in (("cpi-india", "After Indian inflation (what it buys here)"),
                      (HOME_CPI.get(pick["series"]), f"After {pick['name']} inflation (what it buys there)")):
        if cpi and cpi in s and (c0 := s[cpi].at(month)) and (c1 := s[cpi].at(last)):
            rows.append(row(text, now * c0 / c1))
    for name, text in ALTERNATIVES:
        alt = s.get(name)
        a0, a1 = (alt.at(month), alt.at(last)) if alt else (None, None)
        if a0 and a1:
            rows.append(row(f"Instead: {text}", amount * a1 / a0))
        else:
            # The series starts after the chosen month.
            rows.append(row(f"Instead: {text}", None,
                            f"starts {label(alt.first)}" if alt else "no data"))
    out["answer"] = {"units": units, "rate0": rate0, "rate1": rate1, "now": now,
                     "from_label": label(month), "years": years,
                     "cagr": _cagr(amount, now, years) if years >= 1 else None,
                     "rows": rows, "peg": pick["peg"], "name": pick["name"]}
    return out


def inr(v: float) -> str:
    """Rupees with Indian digit grouping: 2,19,941."""
    s = f"{round(v):d}"
    head, tail = s[:-3], s[-3:]
    while len(head) > 2:
        head, tail = head[:-2], head[-2:] + "," + tail
    return "₹" + (head + "," + tail if head else tail)
