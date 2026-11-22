"""/portfolio: the tradebook is read in the browser; the server serves only reference data and
accepts no body."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from investing.web.views.portfolio import router

PPFAS_REGULAR_ISIN = "INF879O01019"   # Parag Parikh Flexi Cap, regular plan, growth: scheme 122640


@pytest.fixture(scope="module")
def c():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.real_db   # pins real scheme codes and the benchmark pools
def test_map_resolves_a_regular_isin_to_its_scheme_category_and_direct_twin(c):
    m = c.get("/api/v1/portfolio/map").json()
    code = m["isin"][PPFAS_REGULAR_ISIN]
    assert code == "122640"
    f = m["funds"][code]
    assert (f["p"], f["o"], f["c"], f["d"]) == ("regular", "growth", "flexi-cap", "122639")
    assert m["categories"]["flexi-cap"]["bench"] == "nifty-500"
    assert 122640 in m["categories"]["flexi-cap"]["pool"]["regular"]
    # An IDCW plan takes its growth sibling's category.
    assert m["funds"]["153964"]["o"] == "idcw" and m["funds"]["153964"]["c"] == "flexi-cap"


@pytest.mark.real_db   # pins the stored NAV and index history
def test_nav_and_index_series_are_the_real_values(c):
    nav = c.get("/api/v1/portfolio/nav/122639").json()
    assert nav["name"].startswith("Parag Parikh Flexi Cap") and nav["d0"] == "2013-05-28"
    assert len(nav["v"]) == len(nav["dd"]) + 1 > 3000
    n50 = c.get("/api/v1/portfolio/index/nifty-50").json()
    assert n50["d0"] == "1999-06-30" and n50["v"][0] == pytest.approx(1256.38)
    # CPI is monthly upstream but served daily.
    cpi = c.get("/api/v1/portfolio/index/cpi-india").json()
    assert set(cpi["dd"]) == {1}
    assert c.get("/api/v1/portfolio/index/usd-inr").status_code == 404
    assert c.get("/api/v1/portfolio/nav/999999").status_code == 404


def test_no_route_accepts_a_body_and_the_page_says_the_file_stays_local(c):
    routes = router.routes
    assert {r.path for r in routes} >= {"/portfolio", "/api/v1/portfolio/map"}
    for route in routes:
        assert route.methods <= {"GET", "HEAD"}, route.path
        assert route.dependant.body_params == [], route.path
    assert c.post("/api/v1/portfolio/map", content=b"isin,quantity\n").status_code == 405
    html = c.get("/portfolio").text
    assert "Your file stays in this browser." in html
    assert '<form' not in html
