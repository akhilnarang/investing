"""The query layer, against the real database or the seeded one.

Assertions are on real values: 100033's NAVs start on 2006-04-03, INE002A01018 is RELIANCE,
nifty-50 starts at 1256.38 on 1999-06-30. Holdings tests skip when the holding table is empty.
"""
from datetime import date

import pytest

from investing.db import connect
from investing.web import queries as q

RELIANCE = "INE002A01018"
ABSL = 100033  # Aditya Birla Sun Life Large & Mid Cap Fund - Regular Growth


@pytest.fixture(scope="module")
def db():
    c = connect(readonly=True)
    yield c
    c.close()


# ---------------------------------------------------------------- funds

def test_fund_by_scheme_code(db):
    f = q.fund(db, ABSL)
    assert f is not None
    assert f["name"] == "Aditya Birla Sun Life Large & Mid Cap Fund - Regular Growth"
    assert f["isin_growth"] == "INF209K01165"
    assert f["fund_house"] == "Aditya Birla Sun Life Mutual Fund"
    assert f["plan"] == "regular"
    # meta is returned parsed.
    assert f["meta"]["scheme_code"] in (ABSL, str(ABSL))


def test_unknown_fund_is_none(db):
    assert q.fund(db, 99_999_999) is None


def test_fund_by_isin(db):
    rows = q.funds_by_isin(db, "INF209K01165")
    assert [r["scheme_code"] for r in rows] == [ABSL]


def test_search_funds_finds_both_plans(db):
    rows = q.search_funds(db, "Quantum Liquid")
    codes = {r["scheme_code"] for r in rows}
    assert {103734, 141066} <= codes
    assert {r["plan"] for r in rows if r["scheme_code"] in (103734, 141066)} == {"direct", "regular"}
    assert all(r["plan"] == "direct" for r in q.search_funds(db, "Quantum Liquid", plan="direct"))


def test_search_escapes_wildcards(db):
    # An unescaped % would match every fund.
    assert len(q.search_funds(db, "%", limit=500)) < 2000


def test_category_listing(db):
    rows = q.funds_in_category(db, "Debt Scheme - Liquid Fund", plan="direct", limit=500)
    assert all(r["category"] == "Debt Scheme - Liquid Fund" and r["plan"] == "direct" for r in rows)
    assert 103734 in {r["scheme_code"] for r in rows}


def test_categories_counts(db):
    cats = q.categories(db)
    index = {(c["category"], c["plan"]): c["funds"] for c in cats}
    assert index[("Equity Scheme - Flexi Cap Fund", "direct")] >= 1
    assert sum(index.values()) == db.execute(
        "SELECT COUNT(*) FROM fund WHERE category IS NOT NULL").fetchone()[0]


# ---------------------------------------------------------------- NAV

def test_nav_series_real_values(db):
    points = q.nav_series(db, ABSL, limit=3)
    assert points[0] == {"date": "2006-04-03", "nav": 116.61}
    assert points[1]["date"] == "2006-04-04"
    assert all(isinstance(p["date"], str) and len(p["date"]) == 10 for p in points)


def test_nav_series_length(db):
    assert len(q.nav_series(db, ABSL, limit=q.MAX_LIMIT)) > 5000


def test_nav_window_is_inclusive(db):
    points = q.nav_series(db, ABSL, start="2024-01-01", end="2024-01-05")
    assert [p["date"] for p in points] == ["2024-01-01", "2024-01-02", "2024-01-03",
                                           "2024-01-04", "2024-01-05"]
    assert points[0]["nav"] == 765.34


def test_nav_limit_is_capped(db):
    assert len(q.nav_series(db, ABSL, limit=10**9)) <= q.MAX_LIMIT


def test_nav_offset_pages(db):
    first = q.nav_series(db, ABSL, limit=10)
    second = q.nav_series(db, ABSL, limit=10, offset=5)
    assert first[5:] == second[:5]


def test_raw_and_screened_agree_on_a_clean_fund(db):
    """100033 has no known breaks, so raw and screened series are identical."""
    raw = q.nav_series(db, ABSL, start="2024-01-01", end="2024-03-31", screen=False)
    screened = q.nav_series(db, ABSL, start="2024-01-01", end="2024-03-31", screen=True)
    assert raw == screened


def test_screening_repairs_the_axis_zero(db):
    """Axis funds carry a literal 0.0 on 2013-04-07. Raw shows it, screened drops it."""
    code = db.execute("SELECT scheme_code FROM nav JOIN fund USING (scheme_code) "
                      "WHERE d=? AND nav=0 LIMIT 1", ((date(2013, 4, 7) - date(1970, 1, 1)).days,)).fetchone()
    if code is None:
        pytest.skip("no zero NAV on 2013-04-07 in this database")
    code = code[0]
    raw = q.nav_series(db, code, start="2013-04-07", end="2013-04-07", screen=False)
    assert raw and raw[0]["nav"] == 0.0
    assert q.nav_series(db, code, start="2013-04-07", end="2013-04-07", screen=True) == []


def test_nav_of_unknown_fund_is_empty(db):
    assert q.nav_series(db, 99_999_999) == []
    assert q.nav_series(db, 99_999_999, screen=False) == []


def test_returns(db):
    r = q.fund_returns(db, ABSL, as_of="2024-03-28")
    assert r is not None
    assert r["inception"] == "2006-04-03"
    assert r["points"] > 5000
    # 116.61 on 2006-04-03 to 786.21 on 2024-03-28 is about 11.1% a year.
    assert 10 < r["since_inception_cagr"] < 13
    assert set(r["cagr"]) == {"6m", "1y", "3y", "5y", "7y", "10y", "15y", "20y"}
    # 18 years of history covers 15y but not 20y.
    assert all(r["cagr"][k] is not None for k in ("6m", "1y", "3y", "5y", "7y", "10y", "15y"))
    assert r["cagr"]["20y"] is None
    assert 10 < r["cagr"]["15y"] < 20


def test_returns_null_for_windows_longer_than_the_fund(db):
    """A young fund reports null, not zero, for windows longer than its age."""
    row = db.execute("SELECT scheme_code, COUNT(*) c, MIN(d) m FROM nav GROUP BY scheme_code "
                     "HAVING c > 200 AND c < 600 ORDER BY m DESC LIMIT 1").fetchone()
    r = q.fund_returns(db, row[0])
    assert r is not None
    age = (date.fromisoformat(r["as_of"]) - date.fromisoformat(r["inception"])).days / 365.25
    for key, years in zip(("6m", "1y", "3y", "5y", "7y", "10y", "15y", "20y"), q.WINDOWS, strict=True):
        assert (r["cagr"][key] is not None) == (years <= age), key
    assert r["cagr"]["20y"] is None
    assert r["since_inception_cagr"] is None if age < 1 else r["since_inception_cagr"] is not None


def test_returns_none_for_unknown_fund(db):
    assert q.fund_returns(db, 99_999_999) is None


# ---------------------------------------------------------------- stocks

def test_stock_by_isin(db):
    assert q.stock(db, RELIANCE) == {"isin": RELIANCE, "symbol": "RELIANCE",
                                     "name": "RELIANCE INDUSTRIES LTD", "slug": None}


def test_stock_by_symbol(db):
    assert [s["isin"] for s in q.stock_by_symbol(db, "RELIANCE")] == [RELIANCE]
    assert [s["isin"] for s in q.stock_by_symbol(db, "reliance")] == [RELIANCE]  # case insensitive


def test_search_stocks_ranks_symbol_first(db):
    rows = q.search_stocks(db, "RELIANCE", limit=10)
    assert rows[0]["isin"] == RELIANCE  # exact symbol match sorts above name matches
    assert any(r["symbol"] == "RCOM" for r in rows)  # RELIANCE COMMUNICATIONS, a name-only match


def test_stock_prices(db):
    points = q.stock_prices(db, RELIANCE, limit=2)
    assert points[0] == {"date": "2016-04-01", "close": 1034.45}
    assert points[1]["date"] == "2016-04-04"  # 2016-04-02/03 was a weekend, so the gap is 3 days
    assert len(q.stock_prices(db, RELIANCE, limit=q.MAX_LIMIT)) > 2500


def test_stock_price_window(db):
    points = q.stock_prices(db, RELIANCE, start="2026-09-01", end="2026-09-03")
    assert [p["date"] for p in points] == ["2026-09-01", "2026-09-02", "2026-09-03"]
    assert points[0]["close"] == 1309.0


def test_unknown_stock_is_empty(db):
    assert q.stock(db, "NOSUCHISIN") is None
    assert q.stock_prices(db, "NOSUCHISIN") == []
    assert q.corporate_actions(db, "NOSUCHISIN") == []


# ---------------------------------------------------------------- series

def test_series_meta_and_points(db):
    m = q.series_meta(db, "nifty-50")
    assert m is not None
    assert m["kind"] == "index"
    assert m["meta"]["index"] == "NIFTY 50"
    points = q.series_points(db, "nifty-50", limit=1)
    assert points == [{"date": "1999-06-30", "value": 1256.38}]


def test_series_list_by_kind(db):
    names = {s["name"] for s in q.series_list(db)}
    assert {"nifty-50", "usd-inr", "gold-inr", "cpi-india"} <= names
    assert {s["kind"] for s in q.series_list(db, "fx")} == {"fx"}
    assert "usd-inr" in {s["name"] for s in q.series_list(db, "fx")}


def test_series_window_and_cap(db):
    points = q.series_points(db, "nifty-50", start="2020-03-23", end="2020-03-24")
    assert [p["date"] for p in points] == ["2020-03-23", "2020-03-24"]
    assert len(q.series_points(db, "nifty-50", limit=10**9)) <= q.MAX_LIMIT


def test_unknown_series_is_empty(db):
    assert q.series_meta(db, "nope") is None
    assert q.series_points(db, "nope") == []


# ---------------------------------------------------------------- holdings: the join

def _has_holdings(db) -> bool:
    return db.execute("SELECT 1 FROM holding LIMIT 1").fetchone() is not None


def test_empty_holdings_never_crash(db):
    """None of these raise on missing holdings."""
    assert q.holders_of(db, "NOSUCHISIN") == []
    assert q.holding_dates_for_isin(db, "NOSUCHISIN") == []
    assert q.isin_ownership(db, "NOSUCHISIN") == []
    assert q.position_history(db, 99_999_999, "NOSUCHISIN") == []
    assert q.portfolio(db, 99_999_999) == {"scheme_code": 99_999_999, "date": None, "holdings": []}
    assert q.portfolio_dates(db, 99_999_999) == []
    assert q.overlap(db, 99_999_999, 99_999_998)["holdings"] == []


def test_holders_of_reliance(db):
    if not _has_holdings(db):
        pytest.skip("holding table not loaded yet")
    rows = q.holders_of(db, RELIANCE, limit=q.MAX_ROW_LIMIT)
    if not rows:
        pytest.skip("no disclosed holdings of RELIANCE yet")
    # Newest first, each a holding of this ISIN by a known fund.
    assert rows == sorted(rows, key=lambda r: r["date"], reverse=True)
    assert all(RELIANCE in r["security_name"] or "Reliance" in r["security_name"] for r in rows)
    for r in rows[:20]:
        assert q.fund(db, r["scheme_code"]) is not None
        # A weight may be absent; when present it is a percent.
        assert r["weight"] is None or 0 < r["weight"] <= 100
    assert all(len(r["date"]) == 10 and r["date"][4] == "-" for r in rows)


def test_holders_on_one_date_matches_the_count(db):
    if not _has_holdings(db):
        pytest.skip("holding table not loaded yet")
    own = q.isin_ownership(db, RELIANCE, limit=1)
    if not own:
        pytest.skip("no disclosed holdings of RELIANCE yet")
    d, n = own[0]["date"], own[0]["funds"]
    rows = q.holders_of(db, RELIANCE, on=d, limit=q.MAX_ROW_LIMIT)
    assert len(rows) == min(n, q.MAX_ROW_LIMIT)
    assert {r["date"] for r in rows} == {d}
    # The total is the sum of the rows, skipping null weights as SUM does.
    if n <= q.MAX_ROW_LIMIT:
        total = sum(r["weight"] for r in rows if r["weight"] is not None)
        assert abs(total - own[0]["total_weight"]) < 1e-6


def test_portfolio_is_the_inverse_of_holders_of(db):
    """A fund that holds an ISIN must appear in that fund's portfolio on the same date."""
    if not _has_holdings(db):
        pytest.skip("holding table not loaded yet")
    rows = q.holders_of(db, RELIANCE, limit=1)
    if not rows:
        pytest.skip("no disclosed holdings of RELIANCE yet")
    code, d, weight = rows[0]["scheme_code"], rows[0]["date"], rows[0]["weight"]
    p = q.portfolio(db, code, on=d, limit=q.MAX_ROW_LIMIT)
    assert p["date"] == d
    match = [h for h in p["holdings"] if h["isin"] == RELIANCE]
    assert len(match) == 1 and match[0]["weight"] == weight
    # Heaviest first, and the company join resolved the symbol.
    weights = [h["weight"] for h in p["holdings"] if h["weight"] is not None]
    assert weights == sorted(weights, reverse=True)
    assert match[0]["symbol"] == "RELIANCE"


def test_portfolio_defaults_to_the_newest_date(db):
    if not _has_holdings(db):
        pytest.skip("holding table not loaded yet")
    code = db.execute("SELECT scheme_code FROM holding LIMIT 1").fetchone()[0]
    dates = q.portfolio_dates(db, code)
    assert dates == sorted(dates, reverse=True)
    assert q.portfolio(db, code)["date"] == dates[0]


def test_position_history(db):
    if not _has_holdings(db):
        pytest.skip("holding table not loaded yet")
    rows = q.holders_of(db, RELIANCE, limit=1)
    if not rows:
        pytest.skip("no disclosed holdings of RELIANCE yet")
    code = rows[0]["scheme_code"]
    hist = q.position_history(db, code, RELIANCE)
    assert hist and hist[0]["date"] == rows[0]["date"]
    assert hist[0]["weight"] == rows[0]["weight"]
    assert [h["date"] for h in hist] == sorted((h["date"] for h in hist), reverse=True)


def test_overlap_of_a_fund_with_itself_is_its_whole_portfolio(db):
    """Overlap with itself: the common weight is the fund's total."""
    if not _has_holdings(db):
        pytest.skip("holding table not loaded yet")
    code = db.execute("SELECT scheme_code FROM holding WHERE isin IS NOT NULL LIMIT 1").fetchone()[0]
    o = q.overlap(db, code, code, limit=q.MAX_ROW_LIMIT)
    p = q.portfolio(db, code, on=o["date"], limit=q.MAX_ROW_LIMIT)
    own = sum(h["weight"] for h in p["holdings"] if h["isin"] and h["weight"])
    assert len(o["holdings"]) == len([h for h in p["holdings"] if h["isin"]])
    assert abs(o["common_weight"] - own) < 1e-6
    assert all(h["weight_a"] == h["weight_b"] for h in o["holdings"])


def test_holdings_limits_are_capped(db):
    assert len(q.holders_of(db, RELIANCE, limit=10**9)) <= q.MAX_ROW_LIMIT
    assert len(q.portfolio(db, 119700, limit=10**9)["holdings"]) <= q.MAX_ROW_LIMIT
