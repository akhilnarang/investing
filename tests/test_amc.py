"""The AMC portfolio archives. Parsing only: no network, no workbooks required."""
from datetime import date

from investing.db import connect, day
from investing.sources import amc

SHEET = [
    ["SCHEME NAME : Parag Parikh Flexi Cap Fund (An open ended scheme)"],
    ["Portfolio as on 31 July 2026"],
    ["Name of the Instrument", "ISIN", "Industry", "Quantity", "Market value", "% to Net Assets"],
    ["Equity & Equity related", "", "", "", "", ""],
    ["HDFC Bank Ltd", "INE040A01034", "Banks", "1000", "1500000", "7.52"],
    ["Bajaj Holdings", "INE118A01012", "Finance", "500", "900000", "4.10"],
    ["Debt Instruments", "", "", "", "", ""],
    ["Govt Bond 2030", "IN0020200070", "Sovereign", "100", "100000", "1.20"],
    ["Total", "", "", "", "", "100.00"],
]


def test_a_sebi_sheet_parses():
    title, as_on, holdings = amc.parse_sheet(SHEET)
    assert title == "Parag Parikh Flexi Cap Fund"
    assert as_on == "2026-07"
    assert len(holdings) == 3
    by_isin = {h["holding_isin"]: h for h in holdings}
    assert by_isin["INE040A01034"]["percentage_to_aum"] == 7.52
    # Only rows under an equity heading are Equity; the bond is left blank so overlap ignores it.
    assert by_isin["INE040A01034"]["security_asset_class"] == "Equity"
    assert by_isin["IN0020200070"]["security_asset_class"] == ""


def test_fractions_are_recognised_as_fractions():
    """Nippon writes 0.0752 where PPFAS writes 7.52; a book summing to about 1 is read as fractions."""
    sheet = [r[:] for r in SHEET]
    for row in sheet[4:8]:
        if row[-1] and row[0] != "Total":
            row[-1] = str(float(row[-1]) / 100)
    _, _, holdings = amc.parse_sheet(sheet)
    assert {h["holding_isin"]: h["percentage_to_aum"] for h in holdings}["INE040A01034"] == 7.52


def test_month_spellings():
    """AMCs write the month a dozen ways across file names and "as on" lines."""
    assert amc.month_from_text("NIMF-MONTHLY-PORTFOLIO-28-Feb-25.xls") == "2025-02"
    assert amc.month_from_text("Monthly-Portfolio_April-2016.xls") == "2016-04"
    assert amc.month_from_text("as on 31 July 2026") == "2026-07"
    assert amc.month_from_text("30062022") == "2022-06"
    assert amc.month_from_text("no date here at all") is None


def test_scheme_renames_still_match():
    """An old scheme name matches today's scheme code across an acquisition and a rename."""
    nippon = [{"scheme_code": 118668, "name": "Nippon India Growth Mid Cap Fund - Direct Plan - Growth"}]
    got = amc.match_scheme("RELIANCE GROWTH FUND", nippon)
    assert got is not None and got["scheme_code"] == 118668
    ppfas = [{"scheme_code": 122639, "name": "Parag Parikh Flexi Cap Fund - Direct Plan - Growth"}]
    got = amc.match_scheme("PPFAS Long Term Equity Fund", ppfas)
    assert got is not None and got["scheme_code"] == 122639
    assert amc.match_scheme("Some Completely Unrelated Fund", ppfas) is None


def test_month_end_is_the_disclosure_date():
    assert amc.month_end("2026-02") == date(2026, 2, 28)
    assert amc.month_end("2024-02") == date(2024, 2, 29)
    assert amc.month_end("2026-12") == date(2026, 12, 31)


def test_exited_positions_are_not_stored(tmp_path):
    db = connect(tmp_path / "t.sqlite3")
    n = amc.store(db, 1, "2026-07", [
        {"company_name": "Held", "holding_isin": "INE1", "percentage_to_aum": 5.0,
         "security_asset_class": "Equity", "sector_name": "Banks"},
        {"company_name": "Exited", "holding_isin": "INE2", "percentage_to_aum": 0.0,
         "security_asset_class": "Equity", "sector_name": None},
    ])
    db.commit()
    assert n == 1
    assert [r[0] for r in db.execute("SELECT name FROM holding")] == ["Held"]
    assert db.execute("SELECT d FROM holding").fetchone()[0] == day(date(2026, 7, 31))
