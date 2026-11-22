"""The /currency calculator and the /sif tracker pages."""
import pytest
from fastapi.testclient import TestClient

from investing.web.app import app

pytestmark = pytest.mark.real_db   # pins real ECB rates, the SIF register and Kuvera's books


@pytest.fixture(scope="module")
def c():
    return TestClient(app)


def test_a_lakh_in_dollars_in_2000_is_answered_three_ways(c):
    """Nominal, after Indian and US inflation, and against alternatives. 43.6299 is the ECB-derived
    USD/INR at end January 2000."""
    t = c.get("/currency").text
    assert "₹1,00,000 →" in t
    assert "43.6299 in" in t and "Jan 2000" in t
    for row in ("After Indian inflation", "After US dollar inflation", "Instead: Nifty 500 TRI"):
        assert row in t, row
    # The overnight rate index starts in 2011; the row says so.
    assert "starts Jan 2011" in t


def test_a_pegged_currency_says_so_and_a_bad_month_is_explained(c):
    t = c.get("/currency", params={"ccy": "AED", "month": "2015-06"}).text
    assert "pegged to the US dollar at 3.6725" in t
    r = c.get("/currency", params={"month": "1990-01"})
    assert r.status_code == 200 and "Pick a month between Jan 2000" in r.text


def test_sif_tracker_names_the_fund_house_not_the_brand(c):
    """AMFI's register calls Aditya Birla's SIF "Apex SIF"; the page names the house."""
    t = c.get("/sif").text
    assert "Aditya Birla Sun Life" in t and "Apex SIF" not in t
    assert "Every SIF, newest first" in t


def test_sif_panel_shows_the_long_short_book_kuvera_reported(c):
    """ITI's SIF has a side-less August book from the AMC importer; the panel shows the July
    long/short book its exposure figures came from."""
    t = c.get("/sif", params={"fund": 9000019}).text
    assert "Shorts (1)" in t and "deepest fall from a peak" in t
    assert c.get("/sif", params={"fund": 119700}).status_code == 404
