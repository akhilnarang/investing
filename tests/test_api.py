"""The HTTP API, against the real database or the seeded one. No network: TestClient calls the app
in-process.

Assertions are on real values: /api/v1/funds/100033 is an Aditya Birla fund, INE002A01018 is
RELIANCE, nifty-50 opens at 1256.38 on 1999-06-30.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from investing.db import get_db
from investing.web import queries as q
from investing.web.app import app

RELIANCE = "INE002A01018"
ABSL = 100033


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["funds"] > 0


# ---------------------------------------------------------------- funds

def test_get_fund(client):
    body = client.get(f"/api/v1/funds/{ABSL}").json()
    assert body["name"] == "Aditya Birla Sun Life Large & Mid Cap Fund - Regular Growth"
    assert body["isin_growth"] == "INF209K01165"
    assert body["plan"] == "regular"
    assert isinstance(body["meta"], dict)  # parsed, not a JSON string


def test_unknown_fund_404(client):
    r = client.get("/api/v1/funds/99999999")
    assert r.status_code == 404
    assert r.json()["detail"] == "No such fund."


def test_fund_search(client):
    body = client.get("/api/v1/funds", params={"q": "Quantum Liquid"}).json()
    assert {103734, 141066} <= {f["scheme_code"] for f in body}


def test_fund_search_requires_a_filter(client):
    """No endpoint returns every fund; asking for one is a 422, not a full dump."""
    assert client.get("/api/v1/funds").status_code == 422


def test_fund_category_and_plan(client):
    body = client.get("/api/v1/funds", params={"category": "Debt Scheme - Liquid Fund",
                                            "plan": "direct", "limit": 500}).json()
    assert 103734 in {f["scheme_code"] for f in body}
    assert all(f["plan"] == "direct" for f in body)
    assert client.get("/api/v1/funds", params={"q": "Liquid", "plan": "sideways"}).status_code == 422


def test_funds_by_isin(client):
    assert [f["scheme_code"] for f in client.get("/api/v1/funds/by-isin/INF209K01165").json()] == [ABSL]


@pytest.mark.real_db
def test_categories(client):
    body = client.get("/api/v1/categories").json()
    index = {(c["category"], c["plan"]): c["funds"] for c in body}
    assert index[("Other Scheme - Index Funds", "direct")] > 200


# ---------------------------------------------------------------- NAV

def test_nav_emits_iso_dates_and_real_values(client):
    body = client.get(f"/api/v1/funds/{ABSL}/nav", params={"limit": 3}).json()
    assert body["screened"] is True
    assert body["count"] == 3
    assert body["points"][0] == {"date": "2006-04-03", "nav": 116.61}
    assert all(isinstance(p["date"], str) and p["date"][4] == "-" for p in body["points"])


def test_nav_date_range(client):
    body = client.get(f"/api/v1/funds/{ABSL}/nav",
                      params={"start": "2024-01-01", "end": "2024-01-05"}).json()
    assert [p["date"] for p in body["points"]] == ["2024-01-01", "2024-01-02", "2024-01-03",
                                                   "2024-01-04", "2024-01-05"]
    assert body["points"][0]["nav"] == 765.34


def test_nav_limit_is_enforced_not_clamped(client):
    """A limit over the cap is a 422, not a truncated result."""
    assert client.get(f"/api/v1/funds/{ABSL}/nav", params={"limit": 999999}).status_code == 422
    assert client.get(f"/api/v1/funds/{ABSL}/nav", params={"limit": 0}).status_code == 422
    r = client.get(f"/api/v1/funds/{ABSL}/nav", params={"limit": q.MAX_LIMIT})
    assert r.status_code == 200 and r.json()["count"] > 5000


def test_nav_offset_pages(client):
    a = client.get(f"/api/v1/funds/{ABSL}/nav", params={"limit": 10}).json()["points"]
    b = client.get(f"/api/v1/funds/{ABSL}/nav", params={"limit": 10, "offset": 5}).json()["points"]
    assert a[5:] == b[:5]


def test_nav_raw_path(client):
    body = client.get(f"/api/v1/funds/{ABSL}/nav", params={"limit": 3, "screen": "false"}).json()
    assert body["screened"] is False
    assert body["points"][0] == {"date": "2006-04-03", "nav": 116.61}


def test_nav_rejects_a_bad_date(client):
    assert client.get(f"/api/v1/funds/{ABSL}/nav", params={"start": "garbage"}).status_code == 422


def test_nav_of_unknown_fund_404(client):
    assert client.get("/api/v1/funds/99999999/nav").status_code == 404


def test_returns(client):
    body = client.get(f"/api/v1/funds/{ABSL}/returns", params={"as_of": "2024-03-28"}).json()
    assert body["inception"] == "2006-04-03"
    assert 10 < body["since_inception_cagr"] < 13
    assert body["cagr"]["20y"] is None  # the fund is younger than the window
    assert body["cagr"]["10y"] is not None
    assert body["latest_date"] >= "2026-01-01"


def test_returns_of_unknown_fund_404(client):
    assert client.get("/api/v1/funds/99999999/returns").status_code == 404


# ---------------------------------------------------------------- stocks

def test_get_stock(client):
    assert client.get(f"/api/v1/stocks/{RELIANCE}").json() == {
        "isin": RELIANCE, "symbol": "RELIANCE", "name": "RELIANCE INDUSTRIES LTD", "slug": None}


def test_unknown_stock_404(client):
    assert client.get("/api/v1/stocks/NOSUCHISIN").status_code == 404


def test_stock_prices(client):
    body = client.get(f"/api/v1/stocks/{RELIANCE}/prices", params={"limit": 2}).json()
    assert body["points"][0] == {"date": "2016-04-01", "close": 1034.45}
    # 2016-04-02 and 03 were a weekend, so the next close is the 4th, not the 2nd.
    assert body["points"][1]["date"] == "2016-04-04"


def test_stock_price_range_and_cap(client):
    body = client.get(f"/api/v1/stocks/{RELIANCE}/prices",
                      params={"start": "2026-09-01", "end": "2026-09-03"}).json()
    assert [p["date"] for p in body["points"]] == ["2026-09-01", "2026-09-02", "2026-09-03"]
    assert body["points"][0]["close"] == 1309.0
    assert client.get(f"/api/v1/stocks/{RELIANCE}/prices", params={"limit": 10**9}).status_code == 422


def test_stock_search_and_symbol(client):
    body = client.get("/api/v1/stocks", params={"q": "RELIANCE", "limit": 5}).json()
    assert body[0]["isin"] == RELIANCE
    assert [s["isin"] for s in client.get("/api/v1/stocks/by-symbol/RELIANCE").json()] == [RELIANCE]


def test_stock_search_requires_a_query(client):
    assert client.get("/api/v1/stocks").status_code == 422


def test_corporate_actions_empty_is_not_an_error(client):
    r = client.get(f"/api/v1/stocks/{RELIANCE}/actions")
    assert r.status_code == 200 and isinstance(r.json(), list)


# ---------------------------------------------------------------- series

def test_series(client):
    body = client.get("/api/v1/series/nifty-50", params={"limit": 1}).json()
    assert body["kind"] == "index"
    assert body["meta"]["index"] == "NIFTY 50"
    assert body["points"] == [{"date": "1999-06-30", "value": 1256.38}]


def test_series_list_and_kind_filter(client):
    assert {"nifty-50", "usd-inr", "gold-inr"} <= {s["name"] for s in client.get("/api/v1/series").json()}
    fx = client.get("/api/v1/series", params={"kind": "fx"}).json()
    assert {s["kind"] for s in fx} == {"fx"}
    assert "usd-inr" in {s["name"] for s in fx}
    assert client.get("/api/v1/series", params={"kind": "nonsense"}).status_code == 422


def test_unknown_series_404(client):
    assert client.get("/api/v1/series/nope").status_code == 404


def test_series_cap(client):
    assert client.get("/api/v1/series/nifty-50", params={"limit": 10**9}).status_code == 422


# ---------------------------------------------------------------- holdings: the join

def _holdings_loaded(client) -> bool:
    return bool(client.get(f"/api/v1/holdings/by-isin/{RELIANCE}", params={"limit": 1}).json()["holdings"])


def test_holdings_by_isin_never_crashes_when_empty(client):
    """An ISIN nothing holds is an empty list, not a 404 and not a stack trace."""
    body = client.get("/api/v1/holdings/by-isin/NOSUCHISIN").json()
    assert body == {"isin": "NOSUCHISIN", "company": None, "holdings": []}
    assert client.get("/api/v1/holdings/by-isin/NOSUCHISIN/dates").json() == []
    assert client.get("/api/v1/holdings/by-isin/NOSUCHISIN/ownership").json() == []
    assert client.get("/api/v1/funds/99999999/portfolio/dates").json() == []


def test_holdings_by_isin(client):
    if not _holdings_loaded(client):
        pytest.skip("holding table not loaded yet")
    body = client.get(f"/api/v1/holdings/by-isin/{RELIANCE}", params={"limit": 50}).json()
    assert body["company"]["symbol"] == "RELIANCE"
    rows = body["holdings"]
    # Newest disclosure first, ISO dates, and every holder is a fund the API knows.
    assert rows == sorted(rows, key=lambda r: r["date"], reverse=True)
    assert all(len(r["date"]) == 10 for r in rows)
    assert client.get(f"/api/v1/funds/{rows[0]['scheme_code']}").status_code == 200
    assert rows[0]["fund_name"]


def test_holdings_on_one_date(client):
    if not _holdings_loaded(client):
        pytest.skip("holding table not loaded yet")
    d = client.get(f"/api/v1/holdings/by-isin/{RELIANCE}/dates", params={"limit": 1}).json()[0]
    rows = client.get(f"/api/v1/holdings/by-isin/{RELIANCE}",
                      params={"on": d, "limit": q.MAX_ROW_LIMIT}).json()["holdings"]
    assert rows and {r["date"] for r in rows} == {d}
    counted = client.get(f"/api/v1/holdings/by-isin/{RELIANCE}/ownership", params={"limit": 1}).json()[0]
    assert counted["date"] == d
    assert counted["funds"] == len(rows)


def test_portfolio_round_trips_with_holdings_by_isin(client):
    """The join and its inverse must agree: same fund, same date, same weight."""
    if not _holdings_loaded(client):
        pytest.skip("holding table not loaded yet")
    top = client.get(f"/api/v1/holdings/by-isin/{RELIANCE}", params={"limit": 1}).json()["holdings"][0]
    p = client.get(f"/api/v1/funds/{top['scheme_code']}/portfolio",
                   params={"on": top["date"], "limit": q.MAX_ROW_LIMIT}).json()
    assert p["date"] == top["date"]
    held = [h for h in p["holdings"] if h["isin"] == RELIANCE]
    assert len(held) == 1 and held[0]["weight"] == top["weight"]
    assert held[0]["symbol"] == "RELIANCE"  # the company join resolved


def test_portfolio_defaults_to_newest_and_is_sorted(client):
    if not _holdings_loaded(client):
        pytest.skip("holding table not loaded yet")
    code = client.get(f"/api/v1/holdings/by-isin/{RELIANCE}",
                      params={"limit": 1}).json()["holdings"][0]["scheme_code"]
    dates = client.get(f"/api/v1/funds/{code}/portfolio/dates").json()
    assert dates == sorted(dates, reverse=True)
    p = client.get(f"/api/v1/funds/{code}/portfolio", params={"limit": q.MAX_ROW_LIMIT}).json()
    assert p["date"] == dates[0]
    weights = [h["weight"] for h in p["holdings"] if h["weight"] is not None]
    assert weights == sorted(weights, reverse=True)
    assert sum(weights) <= 101  # percent of net assets, not a fraction


def test_position_history(client):
    if not _holdings_loaded(client):
        pytest.skip("holding table not loaded yet")
    top = client.get(f"/api/v1/holdings/by-isin/{RELIANCE}", params={"limit": 1}).json()["holdings"][0]
    hist = client.get(f"/api/v1/funds/{top['scheme_code']}/holdings/{RELIANCE}").json()
    assert hist[0]["date"] == top["date"] and hist[0]["weight"] == top["weight"]


def test_overlap_with_self(client):
    if not _holdings_loaded(client):
        pytest.skip("holding table not loaded yet")
    code = client.get(f"/api/v1/holdings/by-isin/{RELIANCE}",
                      params={"limit": 1}).json()["holdings"][0]["scheme_code"]
    o = client.get(f"/api/v1/funds/{code}/overlap/{code}", params={"limit": q.MAX_ROW_LIMIT}).json()
    assert o["funds"] == [code, code]
    assert all(h["weight_a"] == h["weight_b"] for h in o["holdings"])
    assert o["common_weight"] > 0


def test_holdings_limit_is_enforced(client):
    assert client.get(f"/api/v1/holdings/by-isin/{RELIANCE}", params={"limit": 10**9}).status_code == 422
    assert client.get(f"/api/v1/funds/{ABSL}/portfolio", params={"limit": 10**9}).status_code == 422


# ---------------------------------------------------------------- read-only

def test_every_route_outside_the_prospectus_tools_is_a_get(client):
    """Only /api/v1/ipo/ routes take a request body."""
    for path, methods in app.openapi()["paths"].items():
        if not path.startswith("/api/v1/ipo/"):
            assert set(methods) <= {"get", "head"}, path


def test_the_connection_refuses_writes(client):
    """mode=ro plus query_only: a write is a SQLite error."""
    # Keep a reference to the generator; otherwise it is collected and closes the connection.
    gen = get_db()
    db = next(gen)
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM fund WHERE scheme_code=-1")
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO company VALUES ('X','X','X','x')")
    finally:
        gen.close()
