"""Disclosed portfolios, read back from the database the importer wrote.

A security name can repeat within one portfolio, so the ISIN is part of the key. Weights are
checked against what Kuvera published. Only the key-column test runs on the seeded database.
"""
from datetime import date

import pytest

from investing.db import DEFAULT_PATH, connect, day

AUG = day(date(2026, 8, 31))
JUL = day(date(2026, 7, 31))


@pytest.fixture(scope="module")
def db():
    con = connect(DEFAULT_PATH, readonly=True)
    if not con.execute("SELECT 1 FROM holding LIMIT 1").fetchone():
        pytest.skip("no holdings loaded; run scripts/refresh.py")
    return con


def one(db, code, name, d=AUG):
    """The one row for this name; fails if there are several."""
    rows = db.execute(
        "SELECT * FROM holding WHERE scheme_code=? AND d=? AND name=?", (code, d, name)
    ).fetchall()
    assert len(rows) == 1, f"{name}: expected one row, got {len(rows)}"
    return rows[0]


@pytest.mark.real_db
def test_parag_parikh_top_holding(db):
    """PPFAS flexi cap, August 2026: HDFC Bank at 7.63471% and 112,539mn, as Kuvera published it."""
    r = one(db, 122639, "HDFC Bank Ltd")
    assert r is not None
    assert r["isin"] == "INE040A01034"
    assert r["weight"] == pytest.approx(7.63471)
    assert r["value"] == pytest.approx(112539.0)
    assert r["sector"] == "Financial Services"
    assert r["asset_class"] == "Equity"
    assert r["company"] is None  # company_name equals security_name, so it is not stored twice
    assert r["side"] is None and r["quantity"] is None


@pytest.mark.real_db
def test_icici_large_cap_weight_and_isin(db):
    r = one(db, 120586, "ICICI Bank Ltd")
    assert r["isin"] == "INE090A01021"
    assert r["weight"] == pytest.approx(9.34095)
    assert r["value"] == pytest.approx(74920.2)


@pytest.mark.real_db
def test_duplicate_security_name_survives_as_two_instruments(db):
    """Canara Bank appears twice in 120484: as equity and as a CD with a different ISIN."""
    rows = db.execute(
        "SELECT * FROM holding WHERE scheme_code=? AND d=? AND name='Canara Bank' ORDER BY isin",
        (120484, JUL),
    ).fetchall()
    assert len(rows) == 2
    assert [r["isin"] for r in rows] == ["INE476A01022", "INE476A16G02"]
    assert rows[0]["weight"] == pytest.approx(0.94007)
    assert rows[0]["asset_class"] == "Equity"
    assert rows[1]["asset_class"] is None  # blank in the source
    assert all(r["name"] == "Canara Bank" for r in rows)
    assert [r["isin_key"] for r in rows] == ["INE476A01022", "INE476A16G02"]


def test_key_columns_track_their_sources(db):
    """isin_key and side_key mirror isin and side, with '' where those are null."""
    assert not db.execute(
        """SELECT COUNT(*) FROM holding
           WHERE isin_key IS NOT COALESCE(isin, '') OR side_key IS NOT COALESCE(side, '')"""
    ).fetchone()[0]
    # A missing ISIN is null, never ''.
    assert not db.execute("SELECT COUNT(*) FROM holding WHERE isin=''").fetchone()[0]
    # Only SIFs carry a side.
    assert not db.execute(
        "SELECT COUNT(*) FROM holding WHERE side IS NOT NULL AND scheme_code < 9000000"
    ).fetchone()[0]


@pytest.mark.real_db
def test_non_security_rows_have_no_isin(db):
    """Cash, TREPS and receivables load with no ISIN."""
    n = db.execute(
        "SELECT COUNT(*) FROM holding WHERE isin IS NULL AND d=?", (AUG,)
    ).fetchone()[0]
    assert n > 500


@pytest.mark.real_db
def test_company_recorded_only_when_it_differs(db):
    """Kuvera's company_name carries the instrument suffix the security_name strips."""
    r = one(db, 103734, "National Bank For Agriculture And Rural Development")
    assert r["company"] == "National Bank For Agriculture And Rural Development - Commercial Paper"
    assert r["asset_class"] == "Commercial Paper"


@pytest.mark.real_db
def test_swap_legs_are_loaded_verbatim(db):
    """A short swap leg keeps its negative market value; only the weight is reported positive."""
    t = db.execute("SELECT SUM(weight) FROM holding WHERE scheme_code=? AND d=?", (120425, AUG)).fetchone()[0]
    assert t == pytest.approx(195.40707)
    r = one(db, 120425, "Interest Rate Swaps- Md -12-Oct-2026 (Pay Fixed/Receive Float)")
    assert r["value"] == pytest.approx(-9250.0)
    assert r["isin"] is None


@pytest.mark.real_db
def test_amc_sourced_history_lands_on_month_end(db):
    """Nippon dates its disclosure "2026-03"; it is stored on the 31st, matching Kuvera."""
    d = db.execute(
        "SELECT DISTINCT d FROM holding WHERE scheme_code=? AND d BETWEEN ? AND ?",
        (118668, day(date(2026, 3, 1)), day(date(2026, 3, 31))),
    ).fetchall()
    assert [r["d"] for r in d] == [day(date(2026, 3, 31))]


@pytest.mark.real_db
def test_isin_index_finds_holders_across_funds(db):
    """Who held HDFC Bank in August."""
    n = db.execute(
        "SELECT COUNT(DISTINCT scheme_code) FROM holding WHERE isin=? AND d=?",
        ("INE040A01034", AUG),
    ).fetchone()[0]
    assert n > 100
