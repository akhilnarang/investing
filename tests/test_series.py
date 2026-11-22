"""The classified series, read back from the database the importer wrote.

The DXY bounds are the ones `fx.check` uses; an inverted basket leg moves them by ten points or more.
"""
import json
from datetime import date
from itertools import pairwise

import pytest

from investing.db import DEFAULT_PATH, connect, day

pytestmark = pytest.mark.real_db


@pytest.fixture(scope="module")
def db():
    con = connect(DEFAULT_PATH, readonly=True)
    if not con.execute("SELECT 1 FROM series_meta LIMIT 1").fetchone():
        pytest.skip("no series loaded; run scripts/refresh.py")
    return con


def month(db, name, y, m):
    lo, hi = day(date(y, m, 1)), day(date(y + (m == 12), m % 12 + 1, 1)) - 1
    return [r["v"] for r in db.execute(
        "SELECT v FROM series WHERE name=? AND d BETWEEN ? AND ? ORDER BY d", (name, lo, hi))]


@pytest.mark.parametrize("y,m,low,high", [(2008, 4, 71.0, 74.0), (2002, 1, 115.0, 121.0), (2022, 9, 107.0, 115.0)])
def test_dxy_known_levels(db, y, m, low, high):
    # The April 2008 trough, the January 2002 and September 2022 peaks.
    seen = month(db, "dxy", y, m)
    assert seen, f"no dxy rows for {y}-{m:02d}"
    assert low <= max(seen) <= high, f"dxy {y}-{m:02d}: {min(seen):.1f}-{max(seen):.1f}"


def test_dxy_meta_survives(db):
    row = db.execute("SELECT kind, meta FROM series_meta WHERE name='dxy'").fetchone()
    assert row["kind"] == "fx"
    meta = json.loads(row["meta"])
    assert meta["basket"] == {"EUR": 0.576, "JPY": 0.136, "GBP": 0.119,
                              "CAD": 0.091, "SEK": 0.042, "CHF": 0.036}
    assert meta["scale"] == 50.14348112


def test_pegs_survive(db):
    for name, peg, since in (("aed-inr", 3.6725, "1997"), ("sar-inr", 3.75, "1986")):
        row = db.execute("SELECT kind, meta FROM series_meta WHERE name=?", (name,)).fetchone()
        assert row["kind"] == "fx"
        meta = json.loads(row["meta"])
        assert meta["peg"] == peg
        assert f"pegged to the US dollar since {since}" in meta["note"]
    # Both pegs are exact, so AED/SAR is constant.
    off = db.execute("""SELECT MAX(ABS(a.v / s.v - 3.75 / 3.6725)) AS off
                        FROM series a JOIN series s ON s.name='sar-inr' AND s.d=a.d
                        WHERE a.name='aed-inr'""").fetchone()["off"]
    assert off < 1e-4, off


def test_usd_inr_plausible_recent_level(db):
    d, v = db.execute("SELECT d, v FROM series WHERE name='usd-inr' ORDER BY d DESC LIMIT 1").fetchone()
    assert d >= day(date(2026, 1, 1)), "usd-inr is stale"
    assert 70.0 < v < 130.0, v
    # The rupee was in the 40s in 2008; an inverted division would give 0.02.
    assert 38.0 < max(month(db, "usd-inr", 2008, 1)) < 45.0


def test_kinds_are_the_four(db):
    counts = {r["kind"]: r["n"] for r in db.execute(
        "SELECT kind, COUNT(*) AS n FROM series_meta GROUP BY kind")}
    assert counts == {"fx": 13, "metal": 4, "cpi": 2, "index": 26}
    # meta.kind is "price_index" for fx, metal and cpi alike, so the column kind is what counts.
    assert json.loads(db.execute(
        "SELECT meta FROM series_meta WHERE name='usd-inr'").fetchone()["meta"])["kind"] == "price_index"


def test_cpi_is_monthly_from_annual_rates(db):
    row = db.execute("SELECT kind, meta FROM series_meta WHERE name='cpi-india'").fetchone()
    assert row["kind"] == "cpi"
    meta = json.loads(row["meta"])
    assert meta["annual_inflation_pct"]["2010"] > 10.0  # India's 2010 CPI print was near 12%
    # Monthly points.
    days = [r["d"] for r in db.execute(
        "SELECT d FROM series WHERE name='cpi-india' AND d>=? AND d<? ORDER BY d",
        (day(date(2015, 1, 1)), day(date(2016, 1, 1))))]
    assert len(days) == 12
    assert min(b - a for a, b in pairwise(days)) >= 28
    # Starts at 100 in January 2000 and compounds to roughly 4x.
    first, last = db.execute("SELECT MIN(d), MAX(d) FROM series WHERE name='cpi-india'").fetchone()
    lo = db.execute("SELECT v FROM series WHERE name='cpi-india' AND d=?", (first,)).fetchone()["v"]
    hi = db.execute("SELECT v FROM series WHERE name='cpi-india' AND d=?", (last,)).fetchone()["v"]
    assert 100.0 <= lo < 101.0
    assert 3.5 < hi / lo < 6.0


def test_metals_carry_their_duty_and_scale(db):
    meta = json.loads(db.execute("SELECT meta FROM series_meta WHERE name='gold-inr'").fetchone()["meta"])
    assert meta["duty_schedule"][0]["pct"] == 0.0
    assert any(s["pct"] == 15.0 for s in meta["duty_schedule"])
    # Gold is per 10 g and silver per kg.
    gold = db.execute("SELECT v FROM series WHERE name='gold-inr' ORDER BY d DESC LIMIT 1").fetchone()["v"]
    silver = db.execute("SELECT v FROM series WHERE name='silver-inr' ORDER BY d DESC LIMIT 1").fetchone()["v"]
    assert 50_000 < gold < 500_000, gold
    assert 50_000 < silver < 500_000, silver
    # IBJA uses the same units and sits within a few percent.
    for name, ref in (("ibja-gold", gold), ("ibja-silver", silver)):
        v = db.execute("SELECT v FROM series WHERE name=? ORDER BY d DESC LIMIT 1", (name,)).fetchone()["v"]
        assert 0.85 < v / ref < 1.15, (name, v, ref)


def test_nifty_500_tri_known_levels(db):
    assert db.execute("SELECT kind FROM series_meta WHERE name='nifty-500'").fetchone()["kind"] == "index"
    v = db.execute("SELECT v FROM series WHERE name='nifty-500' AND d=?",
                   (day(date(1995, 1, 2)),)).fetchone()
    assert v is None or v["v"] < 2000  # the TRI is based near 1000 at the start of 1995
    # The March 2020 crash and the level since.
    assert min(month(db, "nifty-500", 2020, 3)) < min(month(db, "nifty-500", 2020, 1))
    last = db.execute("SELECT v FROM series WHERE name='nifty-500' ORDER BY d DESC LIMIT 1").fetchone()["v"]
    assert last > 30_000
