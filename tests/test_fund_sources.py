"""The four fund-side fetchers. No network: cached payloads and the real database, read only."""
import json
from datetime import date, timedelta

import pytest

from investing.db import DEFAULT_PATH, connect, day, undate
from investing.sources import constituents, ter, universe, us_indexes

LIVE = pytest.mark.real_db


@pytest.fixture(scope="module")
def db():
    return connect(DEFAULT_PATH, readonly=True)


# --------------------------------------------------------------------------- universe

def test_merging_into_a_fund_meta_blob_keeps_every_amfi_key(tmp_path):
    """universe merges into the blob AMFI owns; isin_growth, which holdings joins use, survives."""
    scratch = connect(tmp_path / "t.sqlite3")
    amfi_meta = {"scheme_code": 120503, "scheme_name": "Parag Parikh Flexi Cap Fund - Direct Plan",
                 "scheme_type": "Open Ended Schemes",
                 "scheme_category": "Equity Scheme - Flexi Cap Fund",
                 "isin_growth": "INF879O01027", "isin_div_reinvestment": None,
                 "fund_house": "PPFAS Mutual Fund", "source": "amfi",
                 "source_url": "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"}
    scratch.execute("INSERT INTO fund (scheme_code, name, isin_growth, fund_house, category, plan, "
                    "meta) VALUES (?,?,?,?,?,?,?)",
                    (120503, amfi_meta["scheme_name"], "INF879O01027", "PPFAS Mutual Fund",
                     "Equity Scheme - Flexi Cap Fund", "direct",
                     json.dumps(amfi_meta)))

    merged, unknown = universe.merge(scratch, [
        {"scheme_code": "120503", "aum": 125000.5, "sub_category": "Flexi Cap",
         "exit_load": "Exit load of 2%, if redeemed within 365 days",
         "min_investment_amount": 1000.0, "expense_ratio": "0.63"},
        {"scheme_code": "999999", "aum": 1.0},          # Groww lists it, AMFI has no fund row
    ])
    assert (merged, unknown) == (1, 1)

    after = json.loads(scratch.execute(
        "SELECT meta FROM fund WHERE scheme_code=120503").fetchone()[0])
    for key, value in amfi_meta.items():
        assert after[key] == value, f"universe overwrote the AMFI key {key}"
    assert after["aum"] == 125000.5
    assert after["sub_category"] == "Flexi Cap"
    assert after["min_investment"] == 1000.0
    assert after["exit_load"] == "Exit load of 2%, if redeemed within 365 days"
    # Groww's TER is kept only as a diagnostic; the TER used is AMFI's, from ter.py.
    assert after["groww_expense_ratio"] == "0.63"
    assert "expense_ratio" not in after
    # No row is created for a code AMFI does not have.
    assert scratch.execute("SELECT COUNT(*) FROM fund").fetchone()[0] == 1


def test_deduplicate_collapses_repeats_and_aliases_onto_one_amfi_code():
    """Pagination repeats a search_id; a renamed scheme appears under two ids sharing one code."""
    schemes, search_records = universe.deduplicate([
        {"search_id": "a", "scheme_code": "120503", "aum": 100.0},
        {"search_id": "a", "scheme_code": "120503", "aum": 100.0},
        {"search_id": "b", "scheme_code": "120503", "available_for_investment": 1, "aum": 125000.0},
        {"search_id": "c", "scheme_code": "118834", "aum": 7.0},
    ])
    assert search_records == 3                      # the repeat of "a" is gone
    by_code = {str(r["scheme_code"]): r for r in schemes}
    assert set(by_code) == {"120503", "118834"}
    assert by_code["120503"]["search_id"] == "b"    # open for investment first, then the larger AUM


@LIVE
def test_the_universe_landed_in_real_fund_rows(db):
    # Check that the merge survives an AMFI run, not a ratio of counts, which drifts.
    enriched = db.execute(
        "SELECT COUNT(*) FROM fund WHERE json_extract(meta,'$.aum') IS NOT NULL").fetchone()[0]
    assert enriched > 1000, f"only {enriched} funds carry Groww's AUM"
    ppfas = json.loads(db.execute(
        "SELECT meta FROM fund WHERE scheme_code=122639").fetchone()[0])
    assert ppfas["isin_growth"] == "INF879O01027"    # AMFI's key survived the merge
    assert ppfas["sub_category"] == "Flexi Cap"
    assert ppfas["aum"] > 50000                      # PPFAS Flexi Cap is a very large fund
    # Groww's launch_date is bogus and is not taken for an inception date.
    assert "launch_date" not in ppfas and "inception" not in ppfas


# --------------------------------------------------------------------------- ter

def test_acceptance_compares_against_houses_that_answered_not_amfis_whole_list():
    """The 90% floor is measured against houses that had TER rows last time, not AMFI's full list."""
    answered_last_time = {"amcs": 57, "amcs_with_rows": 53, "schemes": 2109}
    assert ter.acceptable(2100, 51, answered_last_time) is None

    whole_list = {"amcs": 57, "amcs_with_rows": 57, "schemes": 2109}
    why = ter.acceptable(2100, 51, whole_list)
    assert why is not None and "57" in why


def test_acceptance_still_refuses_a_genuinely_thin_month():
    previous = {"amcs_with_rows": 53, "schemes": 2109}
    why = ter.acceptable(2100, 40, previous)
    assert why is not None and "fund houses" in why
    why = ter.acceptable(1200, 53, previous)
    assert why is not None and "schemes" in why
    why = ter.acceptable(100, 53, None)               # no previous document: the absolute floor
    assert why is not None and "floor" in why
    assert ter.acceptable(2100, 53, None) is None


def test_an_etf_falls_back_to_its_regular_plan():
    """An ETF has one plan, which AMFI files under regular. Without the fallback its cost reads 0."""
    etf = ter.scheme_row({"Scheme_Name": "Nippon India ETF Nifty 50 BeES",
                          "SchemeCat_Desc": "Other Schemes - Index Funds/ETFs",
                          "D_TER": 0, "D_BER": 0, "R_TER": 0.04, "R_BER": 0.03})
    assert etf["effective_ter"] == 0.04 and etf["effective_ber"] == 0.03
    assert etf["etf_single_plan_fallback"] is True

    fund = ter.scheme_row({"Scheme_Name": "Parag Parikh Flexi Cap Fund",
                           "SchemeCat_Desc": "Equity Scheme - Flexi Cap Fund",
                           "D_TER": 0.63, "D_BER": 0.55, "R_TER": 1.33, "R_BER": 1.2})
    assert fund["effective_ter"] == 0.63             # a fund uses direct, not regular
    assert fund["etf_single_plan_fallback"] is False


def test_latest_row_in_the_month_wins():
    rows = [{"Scheme_Name": "X Fund", "D_TER": 1.0, "TER_Date": "2026-09-01T00:00:00.000Z"},
            {"Scheme_Name": "X Fund", "D_TER": 1.1, "TER_Date": "2026-09-21T00:00:00.000Z"},
            {"Scheme_Name": "X Fund", "D_TER": 1.05, "TER_Date": "2026-09-10T00:00:00.000Z"},
            {"Scheme_Name": "Y Fund"}]                # no TER at all: dropped
    latest = ter.latest_rows(rows)
    assert set(latest) == {"x"}
    assert latest["x"]["D_TER"] == 1.1


@LIVE
def test_the_stored_ter_document_holds_real_expense_ratios(db):
    body = db.execute("SELECT body FROM analysis WHERE kind='ter' AND slug=''").fetchone()
    assert body is not None, "the TER document is one row, kind 'ter', slug ''"
    document = json.loads(body[0])
    assert document["meta"]["source_kind"] == "official AMFI"
    assert document["meta"]["amcs_with_rows"] >= 50
    schemes = document["schemes"]
    assert len(schemes) > 2000

    ppfas = schemes[ter.normalise("Parag Parikh Flexi Cap Fund")]
    assert 0.4 < ppfas["direct_ter"] < 1.0, ppfas["direct_ter"]
    assert ppfas["direct_ter"] < ppfas["regular_ter"]      # direct is always the cheaper plan
    # SEBI caps equity TER at 2.25%, but a fund with almost no assets can report far more. Those
    # rows are real and kept.
    absurd = [s for s in schemes.values() if s["direct_ter"] > 3.0]
    assert len(absurd) < 25, f"{len(absurd)} schemes over 3%: the source, or the parse, has moved"
    assert sum(1 for s in schemes.values() if 0 < s["direct_ter"] <= 2.25) > 1500


def test_normalise_collapses_a_scheme_to_one_key():
    assert (ter.normalise("Parag Parikh Flexi Cap Fund - Direct Plan - Growth")
            == ter.normalise("Parag Parikh Flexi Cap Fund - Regular Plan - IDCW")
            == "paragparikhflexicap")


# --------------------------------------------------------------------------- us_indexes

def test_conversion_uses_the_last_fix_on_or_before_the_day_and_gives_up_after_ten():
    fx = {date(2026, 9, 18): 90.0, date(2026, 9, 21): 100.0}
    usd = {date(2026, 9, 21): 10.0,      # a fix of its own
           date(2026, 9, 22): 10.0,      # the Monday fix carried one day forward
           date(2026, 9, 17): 10.0,      # before any fix
           date(2026, 10, 5): 10.0}      # 14 days past the last fix: too stale to price
    inr, skipped = us_indexes.convert(usd, fx)
    assert inr[date(2026, 9, 21)] == 1000.0
    assert inr[date(2026, 9, 22)] == 1000.0          # carried forward, not re-fixed
    assert date(2026, 9, 17) not in inr and date(2026, 10, 5) not in inr
    assert skipped == 2


def test_a_yahoo_error_object_is_not_read_as_missing_data(monkeypatch):
    """Yahoo answers HTTP 200 with a chart error when throttling; that must raise, not read as empty."""
    monkeypatch.setattr(us_indexes, "get_json", lambda url, tries=5: {
        "chart": {"result": None, "error": {"code": "Too Many Requests"}}})
    with pytest.raises(RuntimeError, match="Too Many Requests"):
        us_indexes.window("QQQ", 2020, 2026, True)


@LIVE
def test_a_us_index_in_rupees_has_a_plausible_level_and_tracks_usd_inr(db):
    fx = {undate(d): v for d, v in db.execute("SELECT d, v FROM series WHERE name='usd-inr'")}
    rows = {undate(d): v for d, v in
            db.execute("SELECT d, v FROM series WHERE name='sp500-tr-inr'")}
    assert len(rows) > 6000

    assert db.execute("SELECT kind FROM series_meta WHERE name='sp500-tr-inr'").fetchone()[0] == "index"

    # ^SP500TR was about 1,870 USD in January 2010 with the rupee near 46.3, so about 86,000 INR.
    start_2010 = [v for d, v in rows.items() if d.year == 2010 and d.month == 1]
    assert 80_000 < max(start_2010) < 95_000, max(start_2010)
    assert 1_200_000 < rows[max(rows)] < 2_500_000, rows[max(rows)]

    # Dividing the rupee back out must leave a plausible USD level; a misaligned FX join would not.
    d = max(rows)
    assert (d - max(fx)).days <= us_indexes.MAX_FX_GAP_DAYS
    usd = rows[d] / fx[max(f for f in fx if f <= d)]
    assert 12_000 < usd < 25_000, f"S&P 500 TR in USD implied at {usd:,.0f}"


@LIVE
def test_every_us_series_is_dense(db):
    for stem in us_indexes.SERIES:
        n, _ = us_indexes.have(db, stem)
        assert n > 6000, f"{stem} holds only {n} days"
        meta = json.loads(db.execute(
            "SELECT meta FROM series_meta WHERE name=?", (stem,)).fetchone()[0])
        assert meta["symbol"] == us_indexes.SERIES[stem]["symbol"]
        assert "refetched" in meta["note"]


# --------------------------------------------------------------------------- constituents

def test_constituents_parse_from_nses_csv_shape():
    csv = ("Company Name,Industry,Symbol,Series,ISIN Code\n"
           "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n"
           "HDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,INE040A01034\n"
           "HDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,INE040A01034\n")   # a repeat
    got = constituents.members(csv, "nifty-50", "NIFTY 50")
    assert set(got) == {"INE002A01018", "INE040A01034"}
    assert got["INE002A01018"] == {"index": "nifty-50", "index_name": "NIFTY 50",
                                   "symbol": "RELIANCE",
                                   "industry": "Oil Gas & Consumable Fuels"}


@LIVE
def test_the_constituent_documents_hold_the_real_index_sizes(db):
    sizes = {}
    for slug, label, _name in constituents.LISTS:
        row = db.execute("SELECT as_of, body FROM analysis WHERE kind='index_constituents' "
                         "AND slug=?", (slug,)).fetchone()
        assert row is not None, f"{slug} has no document"
        document = json.loads(row[1])
        assert document["index_name"] == label
        sizes[slug] = len(document["members"])
    # NSE's lists are the size their names say, give or take a rebalance in progress.
    assert sizes["nifty-50"] == 50 and sizes["nifty-next-50"] == 50
    assert sizes["nifty-midcap-150"] == 150
    assert 245 <= sizes["nifty-smallcap-250"] <= 255
    assert 245 <= sizes["nifty-microcap-250"] <= 260


def test_dates_round_trip_through_the_day_helpers():
    d = date(2026, 9, 22)
    assert undate(day(d)) == d
    assert day(d + timedelta(days=1)) - day(d) == 1
