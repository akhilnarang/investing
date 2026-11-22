"""Every scheme is sold as a Direct and a Regular plan, and the lists show it once."""
import re

import pytest
from fastapi.testclient import TestClient

from investing.db import DEFAULT_PATH, connect
from investing.web import queries as q
from investing.web.app import app

FLEXI = "Equity Scheme - Flexi Cap Fund"
PPFAS_DIRECT = 122639        # Parag Parikh Flexi Cap Fund - Direct Plan - Growth
PPFAS_REGULAR = 122640       # the same scheme, regular plan


@pytest.fixture(scope="module")
def db():
    return connect(readonly=True)


@pytest.fixture(scope="module")
def c():
    return TestClient(app)


def rows(html: str) -> list[str]:
    body = "".join(re.findall(r"<tbody>(.*?)</tbody>", html, re.S))
    return [r for r in body.split("<tr>")[1:]]


# ---------------------------------------------------------------- the pairing

def test_scheme_key_pairs_the_two_plans(db):
    a = q.fund(db, PPFAS_DIRECT)
    b = q.fund(db, PPFAS_REGULAR)
    assert a is not None and b is not None
    assert a["plan"] == "direct" and b["plan"] == "regular"
    assert q.scheme_key(a["name"], a["fund_house"]) == q.scheme_key(b["name"], b["fund_house"])
    # The two plans have different ISINs.
    assert a["isin_growth"] != b["isin_growth"]


def test_sibling_is_the_other_plan_of_the_same_scheme(db):
    s = q.sibling_plan(db, PPFAS_DIRECT)
    f = q.fund(db, PPFAS_DIRECT)
    assert s is not None and f is not None
    assert s["scheme_code"] == PPFAS_REGULAR
    assert s["plan"] == "regular"
    assert s["category"] == f["category"]
    # and it is symmetric
    back = q.sibling_plan(db, PPFAS_REGULAR)
    assert back is not None and back["scheme_code"] == PPFAS_DIRECT


def test_plan_free_name_keeps_the_option_and_drops_the_plan():
    assert q.plan_free_name("Parag Parikh Flexi Cap Fund - Direct Plan - Growth") == \
        "Parag Parikh Flexi Cap Fund - Growth"
    assert q.plan_free_name("Aditya Birla Sun Life Flexi Cap Fund - Growth - Regular Plan") == \
        "Aditya Birla Sun Life Flexi Cap Fund - Growth"
    # Growth and IDCW stay separate rows.
    assert q.plan_free_name("Abakkus Flexi Cap Fund - Direct - IDCW") == "Abakkus Flexi Cap Fund - IDCW"


# ---------------------------------------------------------------- the list views

def test_category_list_names_each_category_once(c):
    html = c.get("/mf").text
    assert html.count(">Equity Scheme - Flexi Cap Fund</a>") == 1
    assert len(rows(html)) == len(q.scheme_categories(connect(readonly=True)))


def test_category_count_is_schemes_not_plan_rows(db):
    flexi = next(c for c in q.scheme_categories(db) if c["category"] == FLEXI)
    # Assert the halving, not a total that moves as AMFI adds funds.
    plan_rows = connect(DEFAULT_PATH, readonly=True).execute("SELECT COUNT(*) FROM fund WHERE category=?", (FLEXI,)).fetchone()[0]
    assert flexi["schemes"] < plan_rows
    assert flexi["schemes"] > plan_rows * 0.4
    assert flexi["plans"] == ["direct", "regular"]


def test_a_fund_appears_once_in_its_category(c):
    html = c.get("/mf", params={"category": FLEXI}).text
    plan_rows = connect(DEFAULT_PATH, readonly=True).execute("SELECT COUNT(*) FROM fund WHERE category=?", (FLEXI,)).fetchone()[0]
    assert len(rows(html)) < plan_rows
    assert html.count("Parag Parikh Flexi Cap Fund - Growth<") == 1
    assert "Parag Parikh Flexi Cap Fund - Direct Plan - Growth" not in html
    # but both plans are still reachable from that one row
    assert f'href="/mf/{PPFAS_DIRECT}"' in html and f'href="/mf/{PPFAS_REGULAR}"' in html


def test_plan_filter_still_gives_the_flat_listing(c):
    html = c.get("/mf", params={"category": FLEXI, "plan": "direct"}).text
    assert "Parag Parikh Flexi Cap Fund - Direct Plan - Growth" in html
    assert f'href="/mf/{PPFAS_REGULAR}"' not in html


def test_search_does_not_return_a_fund_twice(c):
    html = c.get("/mf", params={"q": "parag parikh flexi"}).text
    assert len(rows(html)) == 2            # the Growth plan and the IDCW plan, not four
    assert html.count("Parag Parikh Flexi Cap Fund - Growth<") == 1


# ---------------------------------------------------------------- the fund page

def test_fund_page_shows_its_plan_and_links_the_sibling(c):
    html = c.get(f"/mf/{PPFAS_DIRECT}").text
    assert '<span class="tag">direct plan</span>' in html
    assert f'<a href="/mf/{PPFAS_REGULAR}">regular plan</a>' in html
    # and the link resolves to a real fund of the same category and the other plan
    other = c.get(f"/mf/{PPFAS_REGULAR}")
    assert other.status_code == 200
    assert '<span class="tag">regular plan</span>' in other.text
    assert f'<a href="/mf/{PPFAS_DIRECT}">direct plan</a>' in other.text


# ---------------------------------------------------------------- the comparisons

@pytest.mark.real_db   # reads the stored longterm documents
def test_compare_lists_each_category_once(c):
    html = c.get("/compare").text
    assert html.count(">Flexi Cap</a>") == 1
    documents = q.analysis_slugs(connect(DEFAULT_PATH, readonly=True), "longterm")
    assert 0 < len(rows(html)) < len(documents)   # grouped, so fewer rows than documents
    # the plan is a choice on the row
    assert 'href="/compare/flexi-cap">direct</a>' in html
    assert 'href="/compare/flexi-cap-regular">regular</a>' in html
    assert "NIFTY 500 TRI" in html         # the benchmark name, not the dict it lives in


@pytest.mark.real_db   # reads the stored longterm documents
def test_category_page_offers_the_other_plan(c):
    html = c.get("/compare/flexi-cap").text
    assert '<a href="/compare/flexi-cap-regular">See the regular plans</a>' in html
    back = c.get("/compare/flexi-cap-regular").text
    assert '<a href="/compare/flexi-cap">See the direct plans</a>' in back
