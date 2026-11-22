"""Active share and tilt/selection attribution, against the real database and the real page.

The numbers asserted are known-good figures for quant Flexi Cap.
"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from investing.analysis import attribution as at
from investing.db import connect
from investing.web.app import app

pytestmark = pytest.mark.real_db   # reads the stored longterm, holding and series documents

QUANT_FLEXI = "120843"


@pytest.fixture(scope="module")
def db():
    c = connect(readonly=True)
    if not c.execute("SELECT 1 FROM analysis WHERE kind='longterm' AND slug='flexi-cap'").fetchone():
        pytest.skip("no longterm documents; run scripts/refresh.py")
    yield c
    c.close()


def test_active_share_on_pinned_portfolios(db):
    # The August disclosures, matched by ISIN against the Nifty 500 proxy scaled to 100.
    month = date(2026, 8, 31)
    doc = at.active_share(db, "flexi-cap", month, {QUANT_FLEXI: month})
    q = doc["funds"][QUANT_FLEXI]
    assert doc["index_stocks"] == 500
    assert (q["active_share_pct"], q["equity_pct"], q["holdings"]) == (91.23, 70.85, 27)
    assert q["top_holdings"][0]["stock"] == "INE775A01035"     # Samvardhana Motherson


def test_attribution_on_a_pinned_date(db):
    # Figures hold for the longterm document dated 2026-09-22.
    doc = at.longterm_doc(db, "flexi-cap-regular")
    assert doc is not None
    if doc["as_of"] != "2026-09-22":
        pytest.skip("longterm documents regenerated on a later date")
    q = at.attribution(db, "flexi-cap", "regular")["funds"][QUANT_FLEXI]
    assert (q["months"], q["fund_cagr"], q["tilt_pp"], q["selection_pp"]) == (180, 15.57, 0.87, 1.01)
    assert (q["beta_large"], q["beta_mid"], q["beta_small"]) == (0.326, 0.132, 0.49)


def test_compare_page_shows_active_share_tilt_and_selection():
    text = TestClient(app).get("/compare/flexi-cap").text
    assert "Selection 10y" in text and "Active share" in text
    assert "91%" in text                          # quant Flexi Cap's active share, rendered
