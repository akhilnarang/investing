"""/summary against the real database: one table per plan, a row for every category with funds."""
import json
import re
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from investing.db import DEFAULT_PATH
from investing.web.views.summary import router

pytestmark = pytest.mark.real_db


def test_summary_has_a_row_per_category_and_plan():
    """Every longterm document with funds is a linked row in its plan's table, with active share
    and selection filled in."""
    app = FastAPI()
    app.include_router(router)
    t = TestClient(app).get("/summary").text

    db = sqlite3.connect(f"file:{DEFAULT_PATH}?mode=ro", uri=True)
    slugs = [s for s, body in db.execute("SELECT slug, body FROM analysis WHERE kind='longterm'")
             if json.loads(body).get("funds")]
    tables = re.findall(r"<table data-sortable>(.*?)</table>", t, re.DOTALL)
    assert len(tables) == 2
    linked = [re.findall(r'<tr><td><a href="/compare/([^"]+)"', tb) for tb in tables]
    assert sorted(linked[0] + linked[1]) == sorted(slugs)
    assert all(not s.endswith("-regular") for s in linked[0])

    m = re.search(r'href="/compare/large-cap">.*?</tr>', tables[0], re.DOTALL)
    assert m is not None
    row = m.group(0)
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td.*?</td>", row, re.DOTALL)]
    assert cells[0] == "Nifty 100"
    assert re.fullmatch(r"\d+\.\d", cells[2])            # median ten-year CAGR
    assert re.fullmatch(r"\d+%", cells[7])               # active share
    assert re.fullmatch(r"[+-]\d+\.\d", cells[8])        # selection
