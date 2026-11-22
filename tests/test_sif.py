"""SIFs read back from the database the importer wrote.

The published exposure figures in each SIF's meta are recomputed here as SQL over the holding rows.
"""
import json
from datetime import date

import pytest

from investing.db import DEFAULT_PATH, connect, day

pytestmark = pytest.mark.real_db

SIF_BASE = 9_000_000
ISIF = SIF_BASE + 33       # iSIF Equity Ex-Top 100 Long-Short, ICICI Prudential
MAGNUM_HYBRID = SIF_BASE + 14  # Magnum Hybrid Long Short, SBI
ALTIVA_HYBRID = SIF_BASE + 9   # Altiva Hybrid Long Short
QSIF = SIF_BASE + 1            # qsif Equity Long Short

# The month-end portfolios these assertions are taken from.
ISIF_D = day(date(2026, 7, 31))
MAGNUM_D = day(date(2026, 8, 31))
ALTIVA_D = day(date(2026, 7, 31))


@pytest.fixture(scope="module")
def db():
    con = connect(DEFAULT_PATH, readonly=True)
    if not con.execute("SELECT 1 FROM fund WHERE category='SIF' LIMIT 1").fetchone():
        pytest.skip("no SIFs loaded; run scripts/refresh.py")
    return con


def exposure(db, code, d, side):
    """The long or short exposure the site reports: only equity and derivative legs are counted."""
    v = db.execute("SELECT SUM(weight) FROM holding WHERE scheme_code=? AND d=? AND side=? "
                   "AND asset_class IN ('equity','derivative')", (code, d, side)).fetchone()[0]
    return round(v or 0.0, 1)


def test_sifs_are_their_own_category(db):
    codes = [r["scheme_code"] for r in db.execute("SELECT scheme_code FROM fund WHERE category='SIF'")]
    assert len(codes) == 33, f"33 SIFs have a NAV history on AMFI, found {len(codes)}"
    assert all(c >= SIF_BASE for c in codes)
    # The reserved band keeps SIF codes apart from AMFI scheme codes.
    assert not db.execute("SELECT 1 FROM fund WHERE category!='SIF' AND scheme_code>=?", (SIF_BASE,)).fetchone()


def test_isif_navs(db):
    """iSIF launched 2026-02-05 at 9.99 and was 10.43 on 2026-09-22 (AMFI's SIF NAV history)."""
    rows = db.execute("SELECT d, nav FROM nav WHERE scheme_code=? ORDER BY d", (ISIF,)).fetchall()
    assert len(rows) >= 156
    assert rows[0]["d"] == day(date(2026, 2, 5))
    assert rows[-1]["d"] >= day(date(2026, 9, 22))   # moves forward as AMFI publishes
    on_22 = next(r for r in rows if r["d"] == day(date(2026, 9, 22)))
    assert on_22["nav"] == pytest.approx(10.43)
    assert rows[0]["nav"] == pytest.approx(9.99)


def test_shorts_are_stored_and_retrievable(db):
    """Marico and Voltas are shorted through futures."""
    rows = db.execute("SELECT name, weight, sector, asset_class, isin FROM holding "
                      "WHERE scheme_code=? AND d=? AND side='short' ORDER BY weight DESC",
                      (ISIF, ISIF_D)).fetchall()
    assert len(rows) == 16
    assert rows[0]["name"] == "Future on Marico"
    assert rows[0]["weight"] == pytest.approx(1.31137)
    assert rows[0]["sector"] == "Consumer Defensive"  # from the underlying's own equity row
    assert rows[0]["asset_class"] == "derivative"
    assert rows[0]["isin"] is None  # a future has no ISIN of its own
    assert [r["name"] for r in rows][:2] == ["Future on Marico", "Future on Voltas"]


def test_ordinary_funds_do_not_use_side(db):
    assert not db.execute("SELECT 1 FROM holding WHERE scheme_code<? AND side IS NOT NULL",
                          (SIF_BASE,)).fetchone()


def test_exposure_totals_match_the_source(db):
    """iSIF on 2026-07-31: 80.6% long, 9.7% short, net 70.9%, summed from the stored rows."""
    long_pct, short_pct = exposure(db, ISIF, ISIF_D, "long"), exposure(db, ISIF, ISIF_D, "short")
    assert (long_pct, short_pct) == (80.6, 9.7)
    assert round(long_pct - short_pct, 1) == 70.9

    meta = json.loads(db.execute("SELECT meta FROM fund WHERE scheme_code=?", (ISIF,)).fetchone()[0])
    assert meta["exposure"]["long_pct"] == long_pct
    assert meta["exposure"]["short_pct"] == short_pct
    assert meta["exposure"]["net_pct"] == 70.9


def test_hybrid_sif_is_genuinely_short_heavy(db):
    """Altiva Hybrid runs 86 short positions against 155 longs."""
    n = dict(db.execute("SELECT COALESCE(side,'cash') s, COUNT(*) FROM holding WHERE scheme_code=? AND d=? "
                        "GROUP BY 1", (ALTIVA_HYBRID, ALTIVA_D)).fetchall())
    assert n == {"long": 155, "short": 86, "cash": 3}


def test_weights_are_unsigned_with_side_carrying_the_sign(db):
    """As in Kuvera, weight is unsigned and value carries the sign, so SUM(weight) is gross."""
    row = db.execute("SELECT weight, value FROM holding WHERE scheme_code=? AND d=? AND side='short' "
                     "ORDER BY weight DESC LIMIT 1", (ISIF, ISIF_D)).fetchone()
    assert row["weight"] > 0 and row["value"] < 0
    assert not db.execute("SELECT 1 FROM holding WHERE side IS NOT NULL AND weight < 0").fetchone()


def test_cash_and_treps_have_no_isin_and_no_side(db):
    """Net current assets can be negative."""
    rows = {r["name"]: r["weight"] for r in db.execute(
        "SELECT name, weight FROM holding WHERE scheme_code=? AND d=? AND side IS NULL", (ISIF, ISIF_D))}
    assert rows["Treps"] == pytest.approx(16.3121)
    assert rows["Net Current Assets"] == pytest.approx(-0.41045)
    assert not db.execute("SELECT 1 FROM holding WHERE side IS NULL AND isin IS NOT NULL "
                          "AND scheme_code>=? AND asset_class='cash'", (SIF_BASE,)).fetchone()
    # 23.7% cash, net of the negative current assets.
    total = db.execute("SELECT SUM(weight) FROM holding WHERE scheme_code=? AND d=? AND side IS NULL",
                       (ISIF, ISIF_D)).fetchone()[0]
    assert round(total, 1) == 23.7


def test_same_security_on_both_sides_survives(db):
    """Same name, null ISIN, opposite sides: side is part of the key.

    Magnum Hybrid held BSE options both bought (+8.073 mn) and written (-44.091 mn) on 2026-08-31."""
    rows = db.execute("SELECT name, weight, value, side, isin FROM holding WHERE scheme_code=? AND d=? "
                      "AND name='Option on BSE' ORDER BY side", (MAGNUM_HYBRID, MAGNUM_D)).fetchall()
    assert len(rows) == 2, "the written and bought BSE options collapsed into one row"
    long_row, short_row = rows
    assert (long_row["side"], short_row["side"]) == ("long", "short")
    assert long_row["isin"] is None and short_row["isin"] is None, "side alone separates these"
    assert long_row["value"] == pytest.approx(8.073)
    assert short_row["value"] == pytest.approx(-44.091)
    assert short_row["weight"] == pytest.approx(0.1102)

    # iSIF's Indus Towers hedge: stock long, future short, differing in ISIN as well as side.
    pair = db.execute("SELECT name, side, isin FROM holding WHERE scheme_code=? AND d=? "
                      "AND name='Indus Towers' ORDER BY side", (ISIF, ISIF_D)).fetchall()
    assert [r["side"] for r in pair] == ["long", "short"]
    assert pair[0]["isin"] == "INE121J01017" and pair[1]["isin"] is None


def test_key_columns_track_their_sources(db):
    """isin_key and side_key match their columns and all three side keys are in use."""
    bad = db.execute("SELECT COUNT(*) FROM holding WHERE scheme_code>=? "
                     "AND (isin_key IS NOT COALESCE(isin,'') OR side_key IS NOT COALESCE(side,''))",
                     (SIF_BASE,)).fetchone()[0]
    assert bad == 0
    keys = dict(db.execute("SELECT side_key, COUNT(*) FROM holding WHERE scheme_code>=? GROUP BY 1",
                           (SIF_BASE,)).fetchall())
    assert set(keys) == {"", "long", "short"}, f"expected all three side keys, got {sorted(keys)}"
    assert keys["long"] > 0 and keys["short"] > 0, "a SIF book has both sides"
    assert keys["long"] > keys["short"], "the long book is the larger one in every SIF seen so far"


def test_many_bonds_share_one_issuer_name(db):
    """qsif lists 14 'India (Republic of)' rows: 10 bonds with distinct ISINs, and 4 exited at zero
    weight, which are not stored."""
    rows = db.execute("SELECT name, isin, weight FROM holding WHERE scheme_code=? "
                      "AND name='India (Republic of)'", (QSIF,)).fetchall()
    assert len(rows) == 10
    assert len({r["isin"] for r in rows}) == 10, "distinct ISINs, one issuer name"
    assert all(r["weight"] > 0 for r in rows)


def test_meta_carries_the_sif_specific_fields(db):
    """Strategy, terms and concentration live in meta."""
    meta = json.loads(db.execute("SELECT meta FROM fund WHERE scheme_code=?", (ISIF,)).fetchone()[0])
    assert meta["sif_code"] == "SIF-33"
    assert meta["strategy"] == "Equity Ex-Top 100 Long-Short"
    assert meta["fund_house"] == "ICICI Prudential Mutual Fund"
    assert meta["manager"] == "Sankaran Naren"
    assert meta["portfolio_date"] == "2026-07-31"
    assert meta["amfi"]["ISINPO"] == "INF109K30042"
    book = meta["detail"]["book"]
    assert (book["top_pct"], book["top5_pct"], book["top10_pct"]) == (3.02, 11.73, 20.68)


def test_a_sif_holding_joins_to_its_stock_price(db):
    """A SIF's long leg joins to the stock's price series by ISIN."""
    row = db.execute(
        "SELECT h.name, h.weight, c.symbol FROM holding h JOIN company c ON c.isin = h.isin "
        "WHERE h.scheme_code=? AND h.d=? AND h.side='long' ORDER BY h.weight DESC LIMIT 1",
        (ISIF, ISIF_D)).fetchone()
    if row is None:
        pytest.skip("stock universe not loaded; run scripts/refresh.py")
    assert row["weight"] > 0
