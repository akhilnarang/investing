"""The category longterm generator, against the real database.

A rebuilt document must give the same numbers for the same funds as the stored one that
`/compare/{slug}` renders.
"""
from datetime import date

import pytest

from investing.analysis import longterm as lt
from investing.db import connect

pytestmark = pytest.mark.real_db

# The date the stored documents were measured on.
AS_OF = date(2026, 9, 22)
QUANT_FLEXI = "120843"       # quant Flexi Cap, direct


@pytest.fixture(scope="module")
def db():
    c = connect(readonly=True)
    if not c.execute("SELECT 1 FROM analysis WHERE kind='longterm' LIMIT 1").fetchone():
        pytest.skip("no longterm documents; run scripts/refresh.py")
    yield c
    c.close()


@pytest.fixture(scope="module")
def tables(db):
    rows, month = lt.ter_table(db)
    return lt.universe(db), rows, month, lt.regular_rows(db)


@pytest.fixture(scope="module")
def flexi(db, tables):
    return lt.document(db, "Flexi Cap", "direct", AS_OF, *tables)


# ---------------------------------------------------------------- the document a page reads

def test_flexi_cap_document_reproduces_the_stored_returns(db, flexi):
    """Figures match the stored 2026-09-22 document for quant Flexi Cap."""
    fund = next(f for f in flexi["funds"] if f["scheme_code"] == QUANT_FLEXI)
    assert fund["cagr"] == {"1": 8.14, "3": 16.03, "5": 14.59, "7": 25.03, "10": 19.05}
    assert fund["cagr_since_inception"] == 18.42
    assert fund["inception"] == "2013-01-07"
    assert fund["history_years"] == 13.71
    assert fund["sip_xirr"] == 20.3
    assert fund["max_drawdown_life"] == -41.3
    assert fund["rolling"]["5"]["windows"] == 105
    assert fund["rolling"]["5"]["median"] == 21.4
    assert fund["vs_benchmark_5y"] == {"observations": 97, "median_excess_pp": 6.786, "hit_rate": 0.9381}
    # The benchmark is the Nifty 500 TRI from the series table.
    assert fund["benchmark_cagr"]["10"] == 12.86
    assert flexi["benchmark"]["scheme_code"] == "nifty-500"


def test_document_header_matches_what_the_page_expects(flexi):
    """`web.category` reads horizons, sort, funds and plan off the document."""
    assert flexi["plan"] == "direct"
    assert flexi["horizons"] == [1, 3, 5, 7, 10]
    assert flexi["min_years"] == 0.0           # no history gate; a too-short window renders blank
    assert flexi["sort"] == "10"
    assert flexi["sip_years"] == 10.0 and flexi["floor"] == 12.0
    assert lt.UNGATED_NOTE in flexi["category_notes"]


def test_a_fund_too_young_to_measure_is_listed_and_blank(flexi):
    """A young fund keeps its row and age, with no returns."""
    young = [f for f in flexi["funds"] if "too_short" in f]
    assert young, "no unmeasurable fund in flexi cap; the fixture has drifted"
    for f in young:
        assert "cagr" not in f and f["too_short"] == "no usable NAV history"
        # The age comes from raw NAV days even when there is no screened series.
        assert f["history_years"] is not None and f["history_years"] >= 0
    # They sort below every fund ranked on the sort column, though not necessarily last.
    ranked = [i for i, f in enumerate(flexi["funds"])
              if (f.get("cagr") or {}).get(flexi["sort"]) is not None
              and (f.get("history_years") or 0) >= float(flexi["sort"])]
    assert ranked == list(range(len(ranked)))
    assert all(flexi["funds"].index(f) >= len(ranked) for f in young)


# ---------------------------------------------------------------- metals

@pytest.mark.parametrize("sub,series", [("Gold", "gold-inr"), ("Silver", "silver-inr")])
def test_metal_notes_describe_the_comex_close(db, tables, sub, series):
    """The metals notes name the real source of the dollar leg and say it is a futures close."""
    doc = lt.document(db, sub, "direct", AS_OF, *tables)
    notes = " ".join(doc["category_notes"])
    assert "COMEX" in notes and "futures close" in notes
    assert "not an auction fix" in notes
    names = [s["name"] for s in doc["sources"]]
    assert any("COMEX" in n for n in names), names
    assert doc["benchmark"]["scheme_code"] == series
    assert doc["bench_stats"]["cagr"]["10"] is not None


def test_metal_decomposition_splits_the_rupee_return(db):
    """The note splits the rupee return into metal, currency and duty, rebuilt from `usd-inr` and
    the duty schedule in `series_meta`."""
    notes = lt.metal_decomposition(db, "gold-inr", AS_OF)
    assert notes and "Where the rupee return came from" in notes[0]
    assert "metal in dollars" in notes[0] and "rupee weakening" in notes[0] and "duty" in notes[0]
    # Gold's 10-year rupee CAGR to 2026-09-22 is about 17%.
    assert "10y: +17." in notes[0]


# ---------------------------------------------------------------- lookups

def test_ter_lookup_finds_the_scheme_and_refuses_a_near_miss(db):
    """The fuzzy TER match resolves AMFI's typos but not a name that differs in a number."""
    ters, _ = lt.ter_table(db)
    # AMFI misspells these; the TER feed has them right. "Cumulative" is ICICI's word for growth.
    assert lt.scheme_ter(ters, "SBI AGGRESIVE HYBRID FUND - DIRECT PLAN - Growth")["direct_ter"]
    assert lt.scheme_ter(ters, "WhiteOak Capital Pharma and Heathcare Fund Direct Plan Growth")["direct_ter"]
    assert lt.scheme_ter(ters, "ICICI Prudential Healthcare Fund - Direct Plan - Cumulative")["direct_ter"]
    # These read close to other houses' or other schemes' names and must not match.
    assert lt.scheme_ter(ters, "Sundaram Medium Term Fund Direct Plan - Growth") == {}
    assert lt.scheme_ter(ters, "Axis Dynamic Bond Fund - Direct Plan - Growth Option") == {}
    # A target-maturity fund must not match one with a different year.
    sdl = lt.scheme_ter(ters, "Aditya Birla Sun Life Nifty SDL Sep 2025 Index Fund-Direct Plan-Growth")
    assert "2027" not in sdl.get("scheme_name", "")


def test_regular_plan_pairing_never_lands_on_a_payout_series(db, tables):
    """Every pair is a growth row, and known pairs resolve to the stored regular codes.

    AMFI types one Baroda BNP Paribas regular growth row "REGULAT PLAN", so its closest sibling is IDCW.
    """
    regulars = lt.regular_rows(db)
    rows = {r["scheme_code"]: r for r in tables[0]}
    # (direct code, the regular code the stored -regular document evaluates it on)
    for direct, regular in ((120843, 109830), (122639, 122640), (118955, 101762),
                            (150260, 150258), (139527, 139529), (125503, 125498)):
        pair = lt.plan_pair(rows[direct], regulars)
        assert pair is not None and pair["scheme_code"] == regular, \
            f"{direct} paired to {pair and pair['scheme_code']}, expected {regular}"
    for pool in regulars.values():
        assert not [r for r in pool if lt.is_payout(r["name"])]


def test_dividend_yield_is_a_mandate_not_a_payout_option(db):
    """"Dividend Yield" is a SEBI strategy name, not a payout word."""
    assert not lt.is_payout("ICICI Prudential Dividend Yield Equity Fund - Direct Plan - Growth")
    assert lt.is_payout("ICICI Prudential Dividend Yield Equity Fund - Direct Plan - IDCW")
    assert lt.active_segment({"scheme_name": "Templeton India Equity Income Fund - Direct - Growth",
                              "sub_category": "Thematic"}) is None
    assert lt.active_segment({"scheme_name": "ICICI Prudential Dividend Yield Equity Fund - Direct Growth",
                              "sub_category": "Thematic"}) == "dividend_yield"


# ---------------------------------------------------------------- pools

def test_a_polluted_bucket_is_filtered_by_mandate(db, flexi):
    """Groww files focused, retirement and children's schemes under Flexi Cap; 40 are excluded."""
    mandate = [e for e in flexi["excluded"] if "not a flexi cap mandate" in e["excluded"]]
    assert len(mandate) == 40
    assert not any("Focused" in f["name"] or "Children" in f["name"] for f in flexi["funds"])
    # Every fund left has the mandate or is an index fund.
    for f in flexi["funds"]:
        row = {"scheme_name": f["name"], "sub_category": f["sub_category"]}
        assert lt.active_segment(row) == "flexi_cap" or f.get("passive"), f["name"]


def test_overseas_pool_comes_from_amfis_own_category(db, tables):
    """The pool comes from AMFI's `category`; Groww lists only the international funds open to new money."""
    doc = lt.document(db, "International", "direct", AS_OF, *tables)
    assert len(doc["funds"]) > 40
    # Bond, REIT and bullion feeders stay out unless Groww files them under International.
    for f in doc["funds"]:
        assert not lt.NOT_EQUITY_ABROAD.search(f["name"]) or f["sub_category"] == "International"
    assert lt.plans_for("International") == ("direct",)       # no regular-plan mapping exists


def test_every_category_names_a_series_that_exists(db):
    for sub, (slug, series) in lt.CATEGORIES.items():
        assert db.execute("SELECT 1 FROM series_meta WHERE name=?", (series,)).fetchone(), \
            f"{sub} benchmarks {series}, which is not in series_meta"
        assert db.execute("SELECT 1 FROM series WHERE name=? LIMIT 1", (series,)).fetchone(), \
            f"{series} has no points"
        assert slug and slug == slug.lower()


# ---------------------------------------------------------------- sort

def test_sort_is_the_longest_well_covered_horizon_and_blanks_go_last(flexi):
    """The opening order matches the page's own sort, which puts blanks last."""
    ranked = [f for f in flexi["funds"] if (f.get("cagr") or {}).get("10") is not None
              and (f.get("history_years") or 0) >= 10]
    assert len(ranked) >= 5
    tens = [f["cagr"]["10"] for f in ranked]
    assert tens == sorted(tens, reverse=True)
    # A fund younger than the window is not ranked on it.
    first_blank = next((i for i, f in enumerate(flexi["funds"])
                        if (f.get("history_years") or 0) < 10), len(flexi["funds"]))
    assert all((f.get("history_years") or 0) < 10 for f in flexi["funds"][first_blank:])


def test_sort_falls_back_to_cost_when_no_horizon_is_covered(db, tables):
    """Silver's funds are too young for a 10y ranking, so it opens on a covered horizon or BER."""
    doc = lt.document(db, "Silver", "direct", AS_OF, *tables)
    assert doc["sort"] in ("ber", "3")
    if doc["sort"] == "ber":
        bers = [f["ber"] for f in doc["funds"] if f["ber"] is not None]
        assert bers == sorted(bers)
