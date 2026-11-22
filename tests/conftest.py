"""Test setup: a small seeded database stands in for the real one when that is absent.

The seed is chosen before `investing.db` is imported, by pointing `config.DB_PATH` at a scratch file,
so the whole app opens it. `INVESTING_TEST_SEED=1` forces the seed even when the real database
exists. Tests that need the real database carry `real_db` and are skipped on the seed.

Every value is steady geometric growth through one anchor date, so a test can compute what it
expects. The last day is fixed, not today.
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from investing import config

SEEDED = os.environ.get("INVESTING_TEST_SEED") == "1" or not config.DB_PATH.exists()
LAST = date(2026, 9, 22)
_SEED_DIR: Path | None = None

if SEEDED:
    assert "investing.db" not in sys.modules, "conftest must run before investing.db is imported"
    _SEED_DIR = Path(tempfile.mkdtemp(prefix="investing-seed-"))
    config.DB_PATH = _SEED_DIR / "investing.sqlite3"



def weekdays(start: date, end: date = LAST) -> list[date]:
    return [start + timedelta(n) for n in range((end - start).days + 1) if (start + timedelta(n)).weekday() < 5]


def curve(days: list[date], first: float, anchor: date, value: float):
    """A function of the date: `first` on the first day, `value` on `anchor`, steady growth between."""
    position = {d: i for i, d in enumerate(days)}
    rate = (value / first) ** (1 / position[anchor])
    return lambda d: round(first * rate ** position[d], 2)


def _companies(db) -> None:
    db.executemany("INSERT INTO company (isin, symbol, name) VALUES (?,?,?)", [
        ("INE002A01018", "RELIANCE", "RELIANCE INDUSTRIES LTD"),
        ("INE330H01018", "RCOM", "RELIANCE COMMUNICATIONS LTD"),
        ("INE040A01034", "HDFCBANK", "HDFC BANK LTD"),
        ("INE009A01021", "INFY", "INFOSYS LTD"),
    ])
    days = weekdays(date(2016, 4, 1))
    close = curve(days, 1034.45, date(2026, 9, 1), 1309.0)
    db.executemany("INSERT INTO stock_price VALUES ('INE002A01018', ?, ?)",
                   [((d - date(1970, 1, 1)).days, close(d)) for d in days])
    db.execute("INSERT INTO corporate_action VALUES ('INE002A01018', ?, 'bonus', 1.0, NULL)",
               ((date(2024, 10, 28) - date(1970, 1, 1)).days,))


def _fund(db, code, name, isin, house, category, plan, **meta) -> None:
    db.execute("INSERT INTO fund VALUES (?,?,?,?,?,?,?)",
               (code, name, isin, house, category, plan, json.dumps({"scheme_code": code, **meta})))


def _funds(db) -> None:
    absl, quantum, ppfas = ("Aditya Birla Sun Life Mutual Fund", "Quantum Mutual Fund", "PPFAS Mutual Fund")
    mid = "Equity Scheme - Large & Mid Cap Fund"
    flexi = "Equity Scheme - Flexi Cap Fund"
    liquid = "Debt Scheme - Liquid Fund"
    _fund(db, 100033, "Aditya Birla Sun Life Large & Mid Cap Fund - Regular Growth", "INF209K01165", absl, mid, "regular")
    _fund(db, 119700, "Aditya Birla Sun Life Large & Mid Cap Fund - Direct Plan - Growth", "INF209K01VJ6", absl, mid, "direct")
    _fund(db, 103734, "Quantum Liquid Fund - Direct Plan - Growth", "INF082J01036", quantum, liquid, "direct")
    _fund(db, 141066, "Quantum Liquid Fund - Regular Plan - Growth", "INF082J01028", quantum, liquid, "regular")
    _fund(db, 122639, "Parag Parikh Flexi Cap Fund - Direct Plan - Growth", "INF879O01027", ppfas, flexi, "direct")
    _fund(db, 122640, "Parag Parikh Flexi Cap Fund - Regular Plan - Growth", "INF879O01019", ppfas, flexi, "regular")
    _fund(db, 199001, "Parag Parikh Flexi Cap Fund - Direct Plan - IDCW", None, ppfas, flexi, "direct")
    _fund(db, 199002, "Parag Parikh Flexi Cap Fund - Regular Plan - IDCW", None, ppfas, flexi, "regular")
    _fund(db, 150001, "Seed Young Equity Fund - Direct Plan - Growth", "INF000S00001", "Seed Mutual Fund",
          "Equity Scheme - Thematic Fund", "direct")
    _fund(db, 9000033, "iSIF Equity Ex-Top 100 Long-Short Fund - Direct Plan - Growth", "INF109K30042",
          "ICICI Prudential Mutual Fund", "SIF", "direct", sif_code="SIF-33",
          strategy="Equity Ex-Top 100 Long-Short", fund_house="ICICI Prudential Mutual Fund",
          portfolio_date="2026-07-31", exposure={"long_pct": 80.6, "long_equity_pct": 80.6, "long_derivatives_pct": 0.0, "short_pct": 9.7,
                    "net_pct": 70.9, "gross_pct": 90.3, "equity_holdings": 40, "cash_pct": 23.7},
          ter=0.9, manager="A Manager", detail={"book": {"top_pct": 3.02, "top5_pct": 11.73, "top10_pct": 20.68}})


def _navs(db) -> None:
    epoch = date(1970, 1, 1)

    def put(code, days, value):
        db.executemany("INSERT INTO nav VALUES (?,?,?)", [(code, (d - epoch).days, value(d)) for d in days])

    days = weekdays(date(2006, 4, 3))
    absl = curve(days, 116.61, date(2024, 1, 1), 765.34)
    put(100033, days, absl)
    put(119700, days, lambda d: round(absl(d) * 1.1, 2))
    days = weekdays(date(2013, 5, 28))
    for code, first in ((122639, 10.0), (122640, 10.0), (199001, 10.0), (199002, 10.0)):
        put(code, days, curve(days, first, date(2026, 9, 22), 90.0))
    days = weekdays(date(2012, 1, 2))
    put(103734, days, curve(days, 20.0, date(2026, 9, 22), 40.0))
    put(141066, days, curve(days, 20.0, date(2026, 9, 22), 38.0))
    days = weekdays(LAST - timedelta(days=420))[:300]
    put(150001, days, curve(days, 10.0, days[-1], 11.0))
    days = weekdays(date(2026, 2, 5))
    put(9000033, days, curve(days, 9.99, date(2026, 9, 22), 10.43))


def _series(db) -> None:
    epoch = date(1970, 1, 1)
    month_ends = [date(y, m, 1) - timedelta(days=1) for y in range(2000, 2027) for m in range(1, 13)
                  if date(y, m, 1) <= LAST] + [LAST]
    kinds = {"usd-inr": ("fx", {"scheme_name": "US dollar in rupees", "kind": "price_index"}, 43.0, 88.0),
             "aed-inr": ("fx", {"scheme_name": "UAE dirham in rupees", "peg": 3.6725, "note":
                                "pegged to the US dollar since 1997"}, 11.7, 24.0),
             "gold-inr": ("metal", {"kind": "price_index"}, 4400.0, 150000.0),
             "cpi-india": ("cpi", {"kind": "price_index", "annual_inflation_pct": {"2010": 12.0}}, 100.0, 450.0),
             "cpi-usa": ("cpi", {"kind": "price_index"}, 100.0, 190.0),
             "nifty-500": ("index", {"index": "NIFTY 500 TRI"}, 1000.0, 40000.0),
             "nifty-1d-rate-index": ("index", {"index": "NIFTY 1D Rate Index"}, 1000.0, 1900.0)}
    for name, (kind, meta, first, last) in kinds.items():
        db.execute("INSERT INTO series_meta VALUES (?,?,?)", (name, kind, json.dumps(meta)))
        value = curve(month_ends, first, month_ends[-1], last)
        db.executemany("INSERT INTO series VALUES (?,?,?)", [(name, (d - epoch).days, value(d)) for d in month_ends])
    daily = {"nifty-50": ("index", {"index": "NIFTY 50"}, date(1999, 6, 30), 1256.38, 25000.0),
             "dxy": ("fx", {"note": "A six-currency basket."}, date(2020, 1, 1), 96.0, 98.0)}
    for name, (kind, meta, start, first, last) in daily.items():
        days = weekdays(start)
        value = curve(days, first, days[-1], last)
        db.execute("INSERT INTO series_meta VALUES (?,?,?)", (name, kind, json.dumps(meta)))
        db.executemany("INSERT INTO series VALUES (?,?,?)", [(name, (d - epoch).days, value(d)) for d in days])


def _holdings(db) -> None:
    epoch = date(1970, 1, 1)
    # (fund, date, name, isin, weight, value in millions, sector, asset class, side). The two funds
    # rank RELIANCE differently by weight and by rupees, so the holder sort has something to do.
    rows = []
    for code, weight, value in ((100033, 5.0, 9000.0), (119700, 3.0, 15000.0)):
        for d, drift in ((date(2026, 7, 31), 0.9), (date(2026, 8, 31), 1.0)):
            rows += [
                (code, d, "Reliance Industries Ltd", "INE002A01018", weight * drift, value * drift, "Energy",
                 "Equity", None),
                (code, d, "HDFC Bank Ltd", "INE040A01034", 7.0 * drift, 7000.0, "Financial Services", "Equity", None),
                (code, d, "Treps", None, 2.0, 2000.0, None, None, None),
            ]
    sif = date(2026, 7, 31)
    rows += [
        (9000033, sif, "Infosys", "INE009A01021", 3.0, 300.0, "Technology", "equity", "long"),
        (9000033, sif, "Future on Infosys", None, 1.3, -130.0, "Technology", "derivative", "short"),
        (9000033, sif, "Treps", None, 16.3, 1630.0, None, "cash", None),
    ]
    db.executemany("INSERT INTO holding VALUES (?,?,?,?,NULL,?,?,NULL,?,?,?,COALESCE(?, ''),COALESCE(?, ''))",
                   [(c, (d - epoch).days, n, i, w, v, s, a, side, i, side) for c, d, n, i, w, v, s, a, side in rows])


def _seed() -> None:
    from investing.db import connect, writing

    db = connect(config.DB_PATH)
    with writing(db):
        for table in (_companies, _funds, _navs, _series, _holdings):
            table(db)
    db.close()


if SEEDED:
    _seed()


def pytest_collection_modifyitems(config, items):
    if SEEDED:
        skip = pytest.mark.skip(reason="needs the full database")
        for item in items:
            if "real_db" in item.keywords:
                item.add_marker(skip)


def pytest_sessionfinish(session, exitstatus):
    if _SEED_DIR is not None:
        shutil.rmtree(_SEED_DIR, ignore_errors=True)
