"""The SIF and corporate-action fetchers, parsing real payloads into a scratch database.

The SIF cases use Kuvera rows for SBI's Magnum Hybrid (SIF-14); the Yahoo cases are the two error
payloads served with HTTP 200.
"""
from datetime import date

import pytest

from investing.db import DEFAULT_PATH, connect, day
from investing.sources import events, sif

MAGNUM = sif.code_of("SIF-14")       # 9000014, SBI's Magnum Hybrid Long Short
MAGNUM_DATE = "2026-08-31"

# SIF-14's book on 2026-08-31. "Option on BSE Ltd" appears twice with no ISIN: bought at +8.073 mn
# and written at -44.091 mn.
MAGNUM_ROWS = [
    {"company_name": "Option on BSE Ltd", "security_name": "Option on BSE Ltd",
     "security_asset_class": "", "sector_name": None, "percentage_to_aum": 0.1102,
     "value_in_mn": -44.091, "holding_isin": None},
    {"company_name": "Option on BSE Ltd", "security_name": "Option on BSE Ltd",
     "security_asset_class": "", "sector_name": None, "percentage_to_aum": 0.02018,
     "value_in_mn": 8.073, "holding_isin": None},
    {"company_name": "HDFC Bank Ltd", "security_name": "HDFC Bank Ltd Ordinary Shares",
     "security_asset_class": "equity", "sector_name": "Financial Services",
     "percentage_to_aum": 4.5, "value_in_mn": 1800.0, "holding_isin": "INE040A01034"},
    {"company_name": "Future on Marico Ltd", "security_name": "Future on Marico Ltd",
     "security_asset_class": "", "sector_name": None, "percentage_to_aum": 1.31137,
     "value_in_mn": -524.5, "holding_isin": None},
    {"company_name": "Treps", "security_name": "Treps", "security_asset_class": "",
     "sector_name": None, "percentage_to_aum": 16.3121, "value_in_mn": 6524.8,
     "holding_isin": None},
    {"company_name": "Net Current Assets", "security_name": "Net Current Assets",
     "security_asset_class": "", "sector_name": None, "percentage_to_aum": 0.41045,
     "value_in_mn": -164.2, "holding_isin": None},
    # Kuvera keeps listing an exited position at zero weight and value.
    {"company_name": "Exited Stock Ltd", "security_name": "Exited Stock Ltd",
     "security_asset_class": "equity", "sector_name": "Energy", "percentage_to_aum": 0.0,
     "value_in_mn": 0.0, "holding_isin": "INE999Z01011"},
]


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "t.sqlite3")


@pytest.fixture
def magnum(db):
    """SIF-14's book, classified and stored."""
    db.execute("INSERT INTO fund VALUES (?,?,?,?,?,?,?)",
               (MAGNUM, "Magnum Hybrid Long Short", "INF200K30049", "SBI Mutual Fund",
                sif.CATEGORY, "direct", "{}"))
    n = sif.store_holdings(db, MAGNUM, sif.positions(MAGNUM_ROWS), MAGNUM_DATE)
    db.commit()
    return n


# ---------------------------------------------------------------- SIF


def test_both_sides_of_the_bse_option_survive_storage(db, magnum):
    """Same name, null ISIN, opposite sides: `side_key` keeps both rows."""
    rows = db.execute("SELECT name, weight, value, side, isin FROM holding "
                      "WHERE scheme_code=? AND name='Option on BSE' ORDER BY side",
                      (MAGNUM,)).fetchall()
    assert len(rows) == 2, "the bought and written BSE options collapsed into one row"
    long_row, short_row = rows
    assert (long_row["side"], short_row["side"]) == ("long", "short")
    assert long_row["name"] == short_row["name"] == "Option on BSE"
    assert long_row["isin"] is None and short_row["isin"] is None, "side alone separates these"
    assert long_row["value"] == pytest.approx(8.073)
    assert short_row["value"] == pytest.approx(-44.091)
    assert long_row["weight"] == pytest.approx(0.02018)
    assert short_row["weight"] == pytest.approx(0.1102)


def test_clean_position_matches_what_the_site_shows(db, magnum):
    """The names are clean_position() output, not Kuvera's raw strings."""
    names = {r["name"] for r in db.execute("SELECT name FROM holding WHERE scheme_code=?", (MAGNUM,))}
    assert names == {"Option on BSE", "HDFC Bank", "Future on Marico", "Treps", "Net Current Assets"}
    assert "Ltd" not in " ".join(names)
    assert sif.clean_position("HDFC Bank Ltd Ordinary Shares") == "HDFC Bank"
    assert sif.clean_position("Indus Towers Ltd. (Put Option)") == "Indus Towers (Put Option)"
    assert sif.clean_position("Adani Ports - NCD & Bonds - NCD & Bonds") == "Adani Ports - NCD & Bonds"


def test_a_side_never_carries_a_negative_weight(db, magnum):
    """Weights are unsigned and `side` carries the direction, so SUM(weight) is gross."""
    assert not db.execute("SELECT 1 FROM holding WHERE side IS NOT NULL AND weight < 0").fetchone()
    short = db.execute("SELECT weight, value FROM holding WHERE scheme_code=? AND side='short' "
                       "AND name='Future on Marico'", (MAGNUM,)).fetchone()
    assert short["weight"] == pytest.approx(1.31137) and short["value"] < 0


def test_cash_rows_keep_their_sign_and_have_no_side(db, magnum):
    """Cash rows keep their sign so a payable cancels a receivable."""
    cash = {r["name"]: r["weight"] for r in db.execute(
        "SELECT name, weight FROM holding WHERE scheme_code=? AND side IS NULL", (MAGNUM,))}
    assert cash == {"Treps": pytest.approx(16.3121),
                    "Net Current Assets": pytest.approx(-0.41045)}
    assert round(sum(cash.values()), 4) == pytest.approx(15.9017)
    assert all(r["asset_class"] == "cash" for r in db.execute(
        "SELECT asset_class FROM holding WHERE scheme_code=? AND side IS NULL", (MAGNUM,)))


def test_exited_positions_are_not_stored(db, magnum):
    """Weight <= 0 is an exited position Kuvera still lists."""
    assert magnum == 6, "six real positions out of seven Kuvera rows"
    assert not db.execute("SELECT 1 FROM holding WHERE name='Exited Stock'").fetchone()
    assert not db.execute("SELECT 1 FROM holding WHERE weight = 0").fetchone()


def test_key_columns_track_their_sources(db, magnum):
    assert not db.execute("SELECT 1 FROM holding WHERE isin_key IS NOT COALESCE(isin,'') "
                          "OR side_key IS NOT COALESCE(side,'')").fetchone()
    keys = dict(db.execute("SELECT side_key, COUNT(*) FROM holding GROUP BY 1").fetchall())
    assert keys == {"": 2, "long": 2, "short": 2}


def test_two_rows_agreeing_on_name_isin_and_side_are_summed(db):
    twice = [{"name": "Same", "isin": None, "company": None, "weight": 1.0, "value": 1.0,
              "sector": None, "asset_class": "equity", "side": "long"}] * 2
    assert sif.store_holdings(db, MAGNUM, twice, MAGNUM_DATE) == 1
    assert tuple(db.execute("SELECT weight, value FROM holding WHERE name='Same'").fetchone()) == (2.0, 2.0)


def test_the_portfolio_date_is_the_disclosed_one(db, magnum):
    """holding.d is the portfolio's month-end, not the fetch day."""
    assert {r["d"] for r in db.execute("SELECT d FROM holding WHERE scheme_code=?", (MAGNUM,))} \
        == {day(date(2026, 8, 31))}


def test_sif_codes_live_in_their_reserved_band():
    """SIF-33 is 9000033, above every AMFI scheme code."""
    assert sif.code_of("SIF-33") == 9_000_033
    assert sif.code_of("SIF-1") == 9_000_001
    assert sif.SIF_BASE == 9_000_000


def test_only_direct_growth_plans_are_wanted():
    """Houses label plan and option inconsistently, so all three fields are read."""
    assert sif.direct_growth({"NavName": "qsif Equity Long Short Fund - Growth Option - Direct Plan"})
    assert sif.direct_growth({"NavName": "Magnum Hybrid", "Plan": "Direct", "Option": "Growth"})
    assert not sif.direct_growth({"NavName": "Magnum Hybrid - Direct Plan - IDCW"})
    assert not sif.direct_growth({"NavName": "Magnum Hybrid - Regular - Growth"})


# ---------------------------------------------------------------- stock events

# Yahoo returns both as HTTP 200 with a chart.error: no such symbol, and come back later.
NOT_FOUND = {"chart": {"result": None,
                       "error": {"code": "Not Found",
                                 "description": "No data found, symbol may be delisted"}}}
THROTTLED = {"chart": {"result": None,
                       "error": {"code": "Too Many Requests",
                                 "description": "Too Many Requests. Rate limited. Try after a while."}}}

INFY = {"chart": {"error": None, "result": [{"events": {
    "splits": {"948931200": {"date": 948931200, "numerator": 2, "denominator": 1,
                             "splitRatio": "2:1"},
               "1088640000": {"date": 1088640000, "numerator": 4, "denominator": 1,
                              "splitRatio": "4:1"}},
    "dividends": {"956880000": {"amount": 0.046875, "date": 956880000}},
}}]}}


def test_a_throttle_is_not_a_missing_symbol():
    """A throttle raises, so the symbol is retried next run; only not-found means no symbol."""
    assert events.parse(NOT_FOUND) is None
    with pytest.raises(events.Throttled):
        events.parse(THROTTLED)
    # A 404 is also not-found.
    assert events.parse(None) is None


def test_splits_and_dividends_parse_to_the_real_values():
    """Infosys: 2:1 on 2000-01-27 and 4:1 on 2004-07-01."""
    got = events.parse(INFY)
    assert got is not None
    assert got["splits"] == [("2000-01-27", 2.0), ("2004-07-01", 4.0)]
    assert got["dividends"] == [("2000-04-28", 0.046875)]


def test_a_consolidation_is_a_ratio_below_one():
    """1:10 becomes 0.1."""
    payload = {"chart": {"result": [{"events": {"splits": {"1": {
        "date": 1600000000, "numerator": 1, "denominator": 10, "splitRatio": "1:10"}}}}]}}
    got = events.parse(payload)
    assert got is not None and got["splits"] == [("2020-09-13", 0.1)]


def test_a_malformed_ratio_is_skipped_not_fatal():
    """A ratio written as "2" rather than "2:1" drops that action only."""
    payload = {"chart": {"result": [{"events": {"splits": {
        "a": {"date": 1600000000, "splitRatio": "2"},
        "b": {"date": 1600000000, "numerator": 2, "denominator": 1}}}}]}}
    got = events.parse(payload)
    assert got is not None and got["splits"] == [("2020-09-13", 2.0)]


def test_events_land_in_corporate_action(db):
    """A split and a dividend on one ISIN are both stored; kind is in the key."""
    db.execute("INSERT INTO company (isin, symbol, name) VALUES ('INE009A01021','INFY','Infosys')")
    got = events.parse(INFY)
    assert got is not None
    n = events.store(db, "INE009A01021", got)
    db.commit()
    assert n == 3
    rows = db.execute("SELECT d, kind, ratio, amount FROM corporate_action "
                      "WHERE isin='INE009A01021' ORDER BY d, kind").fetchall()
    assert [(r["kind"], r["ratio"], r["amount"]) for r in rows] == [
        ("split", 2.0, None), ("dividend", None, 0.046875), ("split", 4.0, None)]
    assert rows[0]["d"] == day(date(2000, 1, 27))
    # Re-storing the same payload changes nothing.
    events.store(db, "INE009A01021", got)
    assert db.execute("SELECT COUNT(*) FROM corporate_action").fetchone()[0] == 3


def test_an_unknown_symbol_writes_no_actions(db):
    assert events.parse(NOT_FOUND) is None
    assert db.execute("SELECT COUNT(*) FROM corporate_action").fetchone()[0] == 0


# ---------------------------------------------------------------- against the live database


@pytest.fixture(scope="module")
def live():
    return connect(DEFAULT_PATH, readonly=True)


def test_exposure_is_derived_from_the_stored_rows(db, magnum):
    """Long/short/net figures match a SUM over the stored rows. Only equity and derivative legs count."""
    e = sif.exposures(sif.positions(MAGNUM_ROWS))
    assert e["long_pct"] == 4.5          # HDFC Bank, the one equity long
    assert e["long_derivatives_pct"] == 0.0   # the bought BSE option rounds away at 0.02%
    assert e["short_pct"] == 1.4         # the Marico future plus the written BSE option
    assert e["net_pct"] == 3.1 and e["gross_pct"] == 5.9
    assert e["equity_holdings"] == 1
    assert e["cash_pct"] == 15.9         # TREPS less the negative net current assets
    # The same numbers out of SQL.
    long_pct = db.execute("SELECT SUM(weight) FROM holding WHERE scheme_code=? AND side='long' "
                          "AND asset_class IN ('equity','derivative')", (MAGNUM,)).fetchone()[0]
    assert round(long_pct, 1) == e["long_pct"]


@pytest.mark.real_db
def test_the_live_database_agrees_with_these_rules(live):
    """The two invariants hold across every stored SIF row."""
    if not live.execute("SELECT 1 FROM holding WHERE scheme_code>=? LIMIT 1",
                        (sif.SIF_BASE,)).fetchone():
        pytest.skip("no SIF holdings loaded")
    assert not live.execute("SELECT 1 FROM holding WHERE side IS NOT NULL AND weight < 0").fetchone()
    assert not live.execute("SELECT 1 FROM holding WHERE scheme_code<? AND side IS NOT NULL",
                            (sif.SIF_BASE,)).fetchone(), "side is a SIF column"
    both = live.execute(
        "SELECT name, COUNT(DISTINCT side) n FROM holding WHERE scheme_code=? AND name='Option on BSE' "
        "GROUP BY name", (sif.code_of("SIF-14"),)).fetchone()
    assert both and both["n"] == 2, "SBI's BSE option is held both ways"
