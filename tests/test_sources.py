"""The fetchers. These do not touch the network: they parse cached responses into a scratch DB."""
from datetime import date, timedelta

import pytest

from investing.db import DEFAULT_PATH, connect, day
from investing.sources import amfi, bhavcopy, fx, kuvera

CACHED = sorted(bhavcopy.CACHE.glob("*.csv.zip"))
needs_bhavcopy = pytest.mark.skipif(not CACHED, reason="no cached bhavcopy zips to parse")


@needs_bhavcopy
def test_a_bhavcopy_parses_into_real_rows():
    rows, file_day = bhavcopy.rows_of(CACHED[-1])
    assert len(rows) > bhavcopy.MIN_ROWS
    assert file_day is not None
    isins = {r[0] for r in rows}
    assert all(i.startswith("IN") for i in isins), "an ISIN that is not an ISIN"
    assert all(c > 0 for *_, c in rows), "a close at or below zero"


@needs_bhavcopy
def test_rights_entitlements_are_dropped():
    """Symbols ending -RE trade for a few days and are not the company's stock."""
    rows, _ = bhavcopy.rows_of(CACHED[-1])
    assert not [r for r in rows if r[1].endswith("-RE")]


@needs_bhavcopy
def test_loading_a_day_twice_changes_nothing(tmp_path):
    """Re-running the importer on the same day is idempotent."""
    db = connect(tmp_path / "t.sqlite3")
    d = date.fromisoformat(CACHED[-1].stem.replace(".csv", ""))
    bhavcopy.fetch(db, since=d, until=d)
    first = db.execute("SELECT COUNT(*) FROM stock_price").fetchone()[0]
    bhavcopy.fetch(db, since=d, until=d)
    assert db.execute("SELECT COUNT(*) FROM stock_price").fetchone()[0] == first
    assert first > bhavcopy.MIN_ROWS
    assert db.execute("SELECT COUNT(*) FROM stock_price WHERE d=?", (day(d),)).fetchone()[0] == first


# ---------------------------------------------------------------- AMFI

REPORT = amfi.HEADER + """
Open Ended Schemes ( Equity Scheme - Flexi Cap Fund )
Parag Parikh Mutual Fund
122639;PPFAS Flexi Cap Direct;Direct;Growth;INF879O01027;;85.1234;22-Sep-2026
122639;PPFAS Flexi Cap Direct;Direct;Growth;INF879O01027;;#N/A;21-Sep-2026
100033;Other Fund;Regular;Growth;INF209K01165;;947.9500;22-Sep-2026
"""


def test_report_parses_and_skips_unusable_rows():
    """AMFI emits #N/A for some old rows; they are counted and skipped."""
    s = amfi.parse(REPORT)
    assert set(s) == {"122639", "100033"}
    assert s["122639"]["navs"] == {date(2026, 9, 22): 85.1234}
    assert s["122639"]["skipped"] == 1
    assert s["122639"]["scheme_category"] == "Equity Scheme - Flexi Cap Fund"


def test_the_no_data_page_is_not_an_error():
    """A window with no trading days is empty, not an error."""
    assert amfi.parse(f"<html>{amfi.NO_DATA}</html>") == {}


def test_anything_else_raises():
    with pytest.raises(ValueError):
        amfi.parse("<html>502 Bad Gateway</html>")


def test_a_matured_scheme_does_not_drag_its_house_back():
    """A fund that stopped publishing is left out so it does not pull the house's window back."""
    live, dead = date(2026, 9, 22), date(2025, 1, 1)
    newest = {"a": live, "b": live, "matured": dead}
    assert amfi.stale_codes(["a", "b", "matured"], newest) == ["matured"]
    start = amfi.window_start(["a", "b", "matured"], newest, date(2006, 4, 1))
    assert start == live - timedelta(days=amfi.OVERLAP_DAYS)


def test_a_code_with_no_history_forces_a_full_window():
    since = date(2006, 4, 1)
    assert amfi.window_start(["a", "new"], {"a": date(2026, 9, 22), "new": None}, since) == since


def test_house_aliases_survive_a_rebrand():
    """A scheme recorded under a house's old name still finds the house."""
    by_key = {amfi._key("Bandhan Mutual Fund"): {"mfName": "Bandhan Mutual Fund", "mfId": 1},
              amfi._key("360 ONE Mutual Fund"): {"mfName": "360 ONE Mutual Fund", "mfId": 2}}
    for name, mf_id in (("IDFC Mutual Fund", 1), ("360 ONE Asset", 2), ("Bandhan Mutual Fund", 1)):
        house = amfi.resolve(name, by_key)
        assert house is not None and house["mfId"] == mf_id
    assert amfi.resolve("Not A Real House", by_key) is None


# ---------------------------------------------------------------- Kuvera

def test_one_issuer_can_appear_several_times_in_a_portfolio(tmp_path):
    """Quantum Liquid held two Canara Bank CDs and three Power Finance bonds; the ISIN keeps them apart."""
    db = connect(tmp_path / "t.sqlite3")
    rows = [
        {"security_name": "Canara Bank", "company_name": "Canara Bank",
         "holding_isin": "INE476A16K48", "percentage_to_aum": 4.46822, "portfolio_date": "2026-08-31"},
        {"security_name": "Canara Bank", "company_name": "Canara Bank",
         "holding_isin": "INE476A16I18", "percentage_to_aum": 3.58703, "portfolio_date": "2026-08-31"},
    ]
    n, pd = kuvera.store(db, 103734, "QMFLFGPG-GR", rows)
    db.commit()
    assert (n, pd) == (2, "2026-08-31")
    stored = {r[0]: r[1] for r in db.execute("SELECT isin, weight FROM holding")}
    assert stored == {"INE476A16K48": 4.46822, "INE476A16I18": 3.58703}


def test_exited_positions_are_not_stored(tmp_path):
    """Kuvera keeps listing a position after a fund has exited it, at zero or null weight."""
    db = connect(tmp_path / "t.sqlite3")
    rows = [
        {"security_name": "Held", "holding_isin": "INE1", "percentage_to_aum": 5.0,
         "portfolio_date": "2026-08-31"},
        {"security_name": "Exited", "holding_isin": "INE2", "percentage_to_aum": None,
         "portfolio_date": "2026-08-31"},
        {"security_name": "AlsoExited", "holding_isin": "INE3", "percentage_to_aum": 0.0,
         "portfolio_date": "2026-08-31"},
    ]
    n, _ = kuvera.store(db, 1, "X-GR", rows)
    db.commit()
    assert n == 1
    assert [r[0] for r in db.execute("SELECT name FROM holding")] == ["Held"]


def test_only_the_newest_disclosure_in_a_payload_is_stored(tmp_path):
    """Kuvera can return more than one portfolio date; only one is kept."""
    db = connect(tmp_path / "t.sqlite3")
    rows = [
        {"security_name": "New", "holding_isin": "INE1", "percentage_to_aum": 5.0, "portfolio_date": "2026-08-31"},
        {"security_name": "Old", "holding_isin": "INE2", "percentage_to_aum": 5.0, "portfolio_date": "2026-07-31"},
    ]
    n, pd = kuvera.store(db, 1, "X-GR", rows)
    db.commit()
    assert (n, pd) == (1, "2026-08-31")
    assert [r[0] for r in db.execute("SELECT name FROM holding")] == ["New"]


def test_scheme_name_tokens_ignore_plan_words():
    """"Direct", "Growth" and "Plan" are dropped."""
    assert kuvera.tokens("PPFAS Flexi Cap Fund - Direct Plan - Growth") == {"ppfas", "flexicap"}
    # Kuvera writes Flexicap, Groww writes Flexi Cap.
    assert kuvera.tokens("Flexicap") == kuvera.tokens("Flexi Cap")


# ---------------------------------------------------------------- FX

def test_dxy_basket_is_ices_published_one():
    assert sum(w for w, _ in fx.DXY_BASKET.values()) == pytest.approx(1.0, abs=1e-9)
    # EUR and GBP are quoted as dollars per unit, so their exponent is negative.
    assert {c for c, (_, s) in fx.DXY_BASKET.items() if s < 0} == {"EUR", "GBP"}


def test_dxy_is_computed_the_right_way_up():
    """One April 2008 day from known ECB per-euro fixes; a wrong GBP sign gives 84, not 71.5."""
    d = date(2008, 4, 22)
    quotes = {"USD": {d: 1.5931}, "JPY": {d: 164.80}, "GBP": {d: 0.80205},
              "CAD": {d: 1.5942}, "SEK": {d: 9.3565}, "CHF": {d: 1.5892}}
    level = fx.dxy(quotes)[d]
    assert 71.0 <= level <= 74.0, f"April 2008 DXY came out at {level:.2f}, not the ~71.5 it was"


@pytest.mark.real_db
def test_the_gulf_pegs_hold_exactly():
    """Both are pegged to the dollar, so their cross rate is the ratio of the pegs on every day."""
    db = connect(DEFAULT_PATH, readonly=True)
    worst = db.execute(
        "SELECT MAX(ABS(a.v / b.v - ?)) FROM series a JOIN series b ON a.d = b.d "
        "WHERE a.name='aed-inr' AND b.name='sar-inr'", (3.75 / 3.6725,)).fetchone()[0]
    if worst is None:
        pytest.skip("pegged series not loaded")
    assert worst < 1e-4, f"peg ratio drifted by {worst}"


@pytest.mark.real_db
def test_dxy_matches_known_history():
    db = connect(DEFAULT_PATH, readonly=True)
    if not db.execute("SELECT 1 FROM series WHERE name='dxy' LIMIT 1").fetchone():
        pytest.skip("dxy not loaded")
    assert fx.check(db) == "dxy self-check passed"


def test_the_kuvera_listing_is_read_in_either_shape():
    """The listing is nested category -> sub-category -> AMC -> [{"c", "n"}]; a flat list of
    {code, name} is accepted too."""
    flat = [{"code": "AAA-GR", "name": "A Fund"}, {"code": "BBB-GR", "name": "B Fund"}]
    nested = {"Equity": {"Flexi Cap": {"PPFAS_MF": [{"c": "AAA-GR", "n": "A Fund"}],
                                       "OTHER_MF": [{"c": "BBB-GR", "n": "B Fund"}]}}}
    for shape in (flat, nested):
        out: list[dict] = []
        kuvera._walk(shape, out)
        assert {r["code"] for r in out} == {"AAA-GR", "BBB-GR"}, f"failed on {type(shape).__name__}"
        assert {r["name"] for r in out} == {"A Fund", "B Fund"}
