"""The HTML pages, checked on rendered content as well as status codes."""
import re

import pytest
from fastapi.testclient import TestClient

from investing.web.app import app

real_db = pytest.mark.real_db

RELIANCE = "INE002A01018"
ABSL = 100033


@pytest.fixture(scope="module")
def c():
    return TestClient(app)


@pytest.mark.parametrize("path", ["/", "/mf", "/stocks", "/sif", "/currency", "/ipo", "/static/site.css"])
def test_pages_render(c, path):
    assert c.get(path).status_code == 200


def test_unknown_things_are_404(c):
    assert c.get("/mf/999999").status_code == 404
    assert c.get("/stocks/NOTANISIN").status_code == 404
    assert c.get("/currency/nonesuch").status_code == 404


def test_fund_page_shows_its_name_and_a_real_return(c):
    t = c.get(f"/mf/{ABSL}").text
    assert "Aditya Birla Sun Life" in t
    # The returns table is populated.
    assert re.search(r"\d+\.\d\d%", t)


def test_stock_page_lists_the_funds_that_hold_it(c):
    t = c.get(f"/stocks/{RELIANCE}").text
    assert "RELIANCE" in t
    links = re.findall(r'href="/mf/(\d+)"', t)
    assert links, "no holder listed"
    assert c.get(f"/mf/{links[0]}").status_code == 200


def test_search_finds_a_known_fund(c):
    assert "Parag Parikh" in c.get("/mf", params={"q": "parag parikh"}).text


def test_sif_page_names_a_real_sif(c):
    t = c.get("/sif").text
    assert "Long Short" in t or "Long-Short" in t


def test_the_prospectus_analyzer_pages_work_under_lifespan():
    """The analyzer is built in the lifespan hook, which only a TestClient context manager runs."""
    with TestClient(app) as client:
        home = client.get("/ipo/rhp/")
        assert home.status_code == 200
        # No API tokens in tests, so there is no form.
        assert "New analyses are turned off" in home.text
        assert "<form" not in home.text
        assert client.get("/ipo/rhp/analysis").status_code == 200


def test_the_analyzer_pages_wear_the_site_chrome():
    with TestClient(app) as client:
        t = client.get("/ipo/allotment").text
        assert "oat.ink" not in t, "still loading the old CDN stylesheet"
        assert "/static/site.css" in t
        assert "currency" in t and "/compare" in t, "site nav missing"


def test_the_allotment_page_calls_its_api_under_the_prefix():
    """The page's JavaScript fetches the issue list from the /api/v1/ipo prefix."""
    with TestClient(app) as client:
        t = client.get("/ipo/allotment").text
        assert '"/v1/allotment/issues"' not in t, "absolute path would 404 under the prefix"
        assert "/api/v1/ipo/allotment/issues" in t
        assert client.get("/api/v1/ipo/allotment/issues").status_code == 200


def test_portfolio_rows_show_the_security_name(c):
    """queries.py aliases h.name AS security_name."""
    t = c.get("/mf/119700").text
    names = re.findall(r'<a href="/stocks/[A-Z0-9]+">([^<]*)</a>', t)
    assert names, "no holdings rendered at all"
    assert all(n.strip() for n in names), "a holding rendered with a blank name"


def test_the_stock_page_leads_with_money_not_a_sum_of_percentages(c):
    """Weights across funds do not add up; rupees do, so the headline is rupees."""
    t = c.get("/stocks/INE002A01018").text
    assert "crore held" in t
    assert "736.95" not in t, "still showing the summed-percentage figure"


def test_holders_can_be_sorted_by_value_or_conviction(c):
    by_value = re.findall(r'href="/mf/(\d+)"', c.get("/stocks/INE002A01018?by=value").text)
    by_weight = re.findall(r'href="/mf/(\d+)"', c.get("/stocks/INE002A01018?by=weight").text)
    assert by_value and by_weight
    assert by_value != by_weight, "the sort parameter changed nothing"


def test_a_series_page_draws_its_history(c):
    t = c.get("/currency/dxy").text
    assert "<polyline" in t, "no sparkline"
    assert "Recent" in t, "no recent values"


def test_no_em_dashes_anywhere(c):
    for path in ("/", "/mf", "/mf/119700", "/stocks/INE002A01018", "/currency", "/currency/dxy",
                 "/compare/arbitrage"):
        assert "—" not in c.get(path).text, f"em dash on {path}"


def test_tables_can_scroll_on_a_phone(c):
    """The returns table is wider than a phone, so it sits in a scroll container."""
    assert 'class="scroll-x"' in c.get("/mf/119700").text


def test_fund_count_change_compares_like_with_like(c):
    """A few funds disclose fortnightly, so the change is measured against a month-end date."""
    t = c.get("/stocks/INE002A01018").text
    m = re.search(r'([+-]\d+)</span><span class="sub">funds since ([\d-]+)', t)
    if m:
        _, since = m.groups()
        assert since.endswith(("-31", "-30", "-28", "-29")), f"compared against a partial: {since}"


@pytest.mark.parametrize("path", [
    "/compare", pytest.param("/compare/flexi-cap", marks=real_db),
    pytest.param("/compare/arbitrage", marks=real_db), pytest.param("/index-funds", marks=real_db)])
def test_analysis_pages_render(c, path):
    assert c.get(path).status_code == 200


@real_db
def test_a_category_page_shows_real_returns(c):
    """Horizons are integers but the cagr dicts are keyed by strings."""
    t = c.get("/compare/flexi-cap").text
    assert len(re.findall(r"\d+\.\d\d%", t)) > 50, "no returns rendered"
    assert re.findall(r">(\d+y)</th>", t), "no horizon columns"


@real_db
def test_arbitrage_is_measured_against_cash(c):
    """An arbitrage fund earns the cash-futures basis, so its benchmark is the overnight rate."""
    t = c.get("/compare/arbitrage").text
    assert "1D RATE" in t.upper(), "arbitrage is not measured against the overnight rate"
    assert re.findall(r">(\d+y)</th>", t), "no horizon columns"
    # Returns near the short rate; a figure in the teens means an equity series.
    rates = [float(x) for x in re.findall(r">(\d+\.\d\d)%<", t)]
    assert rates, "no returns rendered"
    assert max(rates) < 12, f"an arbitrage fund does not compound at {max(rates)}%"


@real_db
def test_no_category_prints_a_return_longer_than_the_fund(c):
    """A window longer than the fund's History column renders blank.

    nav.cagr accepts a NAV up to 12 days inside the window, so a fund just short of a horizon could
    otherwise print it."""
    row = re.compile(r'<tr><td><a href="/mf/\d+">.*?</a></td>(.*?)</tr>', re.S)
    cell = re.compile(r'<td class="n[^"]*"[^>]*data-v="[^"]*">(.*?)</td>', re.S)
    checked = 0
    for slug in ("flexi-cap", "flexi-cap-regular", "target-maturity-regular", "liquid", "arbitrage"):
        t = c.get(f"/compare/{slug}").text
        horizons = [int(h[:-1]) for h in re.findall(r">(\d+y)</th>", t)]
        assert horizons, f"{slug}: no horizon columns"
        # Find History by its header. Fund and House are the two non-numeric cells.
        thead = re.search(r"<thead>(.*?)</thead>", t, re.S)
        assert thead is not None
        headers = [re.sub(r"<[^>]+>", "", h).strip()
                   for h in re.findall(r"<th[^>]*>.*?</th>", thead.group(1), re.S)]
        numeric = headers[2:]
        at = numeric.index("History")
        for rest in row.findall(t):
            cells = [re.sub(r"<[^>]+>", "", x) for x in cell.findall(rest)]
            assert len(cells) == len(numeric), f"{slug}: {len(cells)} numeric cells under {len(numeric)} headers"
            history = float(cells[at].strip().rstrip("y"))
            for years in horizons:
                text = cells[numeric.index(f"{years}y")]
                if text.strip():
                    checked += 1
                    assert history >= years, (
                        f"{slug}: a fund with {history}y of history prints a {years}y return")
    assert checked > 500, f"only {checked} return cells seen; the tables did not render"


@real_db
def test_index_funds_shows_tracking_differences(c):
    t = c.get("/index-funds").text
    assert len(re.findall(r"\d+\.\d\d%", t)) > 50
    assert "Tracking difference" in t or "diff" in t


def test_unknown_category_is_404(c):
    assert c.get("/compare/not-a-category").status_code == 404
