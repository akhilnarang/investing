"""fund_facts: what a Groww scheme page becomes in the document, and when the stored one is kept."""
import contextlib
import json

import pytest

from investing.db import connect
from investing.sources import fund_facts

PAGE = {
    "scheme_code": "151796", "search_id": "360-one-flexicap-fund-direct-growth",
    "benchmark_name": "NIFTY 500 Total Return Index", "portfolio_turnover": 31.0,
    "launch_date": "2023-06-30",
    "fund_manager_details": [
        # One person listed twice for two roles: the earlier date is when they took over.
        {"person_name": "Mayur Patel", "date_from": "2023-06-11T18:30:00.000Z"},
        {"person_name": "Mayur Patel ", "date_from": "2024-01-01T00:00:00"},
        {"person_name": "Rahul Khetawat", "date_from": "2024-02-01"},
    ],
    "historic_fund_expense": [
        {"expense_ratio": 0.9, "as_on_date": "2023-06-30T00:00:00"},
        {"expense_ratio": 0.95, "as_on_date": "2023-07-15T00:00:00"},
        {"expense_ratio": 0.92, "as_on_date": "2023-07-31T00:00:00"},
    ],
}


def test_a_page_becomes_tenure_and_monthly_ter_and_truncated_months_carry_forward():
    """Months the page no longer lists are kept from the stored document."""
    previous = {"funds": {"151796": {"ter_history": [["2019-01", 1.5], ["2023-06", 9.9]]}}}
    doc = fund_facts.document({"151796": PAGE}, {}, "2026-10-01", previous)
    f = doc["funds"]["151796"]
    assert f["managers"] == [{"name": "Mayur Patel", "since": "2023-06-11"},
                             {"name": "Rahul Khetawat", "since": "2024-02-01"}]
    # The month's last record stands for it; the page wins where both have a month.
    assert f["ter_history"] == [["2019-01", 1.5], ["2023-06", 0.9], ["2023-07", 0.92]]
    assert (f["benchmark"], f["turnover"], f["launch"]) == ("NIFTY 500 Total Return Index", 31.0, "2023-06-30")
    assert doc["meta"]["funds"] == 1


def test_a_pull_that_mostly_fails_keeps_the_stored_document(tmp_path, monkeypatch):
    db = connect(tmp_path / "t.sqlite3")
    for code in range(100, 120):
        db.execute("INSERT INTO fund (scheme_code, name, plan, meta) VALUES (?,?,?,?)",
                   (code, f"Fund {code}", "direct", json.dumps({"groww_id": f"fund-{code}"})))
    db.execute("INSERT INTO analysis (kind, slug, as_of, body) VALUES ('fund_facts','','x','{\"kept\":1}')")
    db.commit()

    class Down:
        requests = 0

        def open(self, url, **kw):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(fund_facts, "cache_dir", lambda name: tmp_path)
    monkeypatch.setattr(fund_facts.egress, "rotation", lambda **kw: contextlib.nullcontext(Down()))
    with pytest.raises(RuntimeError, match="stored document is kept"):
        fund_facts.fetch(db)
    assert db.execute("SELECT body FROM analysis WHERE kind='fund_facts'").fetchone()[0] == '{"kept":1}'


def test_a_retired_slug_is_marked_gone_not_failed(tmp_path, monkeypatch):
    """egress spells a redirect "<HTTPError 308: ...>", not urllib's "HTTP Error 308"."""
    db = connect(tmp_path / "t.sqlite3")
    db.execute("INSERT INTO fund (scheme_code, name, plan, meta) VALUES (?,?,?,?)",
               (119779, "Kotak FoF", "direct", json.dumps({"groww_id": "kotak-fof"})))
    db.commit()

    class Retired:
        requests = 1

        def open(self, url, **kw):
            raise RuntimeError(f"{url}: <HTTPError 308: 'Permanent Redirect'>")

    monkeypatch.setattr(fund_facts, "cache_dir", lambda name: tmp_path)
    monkeypatch.setattr(fund_facts.egress, "rotation", lambda **kw: contextlib.nullcontext(Retired()))
    out = fund_facts.fetch(db)
    assert "1 gone from Groww" in out and "failed" not in out
    assert (tmp_path / "119779.gone").exists()


def test_a_redirect_loop_is_a_failure_not_gone(tmp_path, monkeypatch):
    db = connect(tmp_path / "t.sqlite3")
    db.execute("INSERT INTO fund (scheme_code, name, plan, meta) VALUES (?,?,?,?)",
               (1, "Loop", "direct", json.dumps({"groww_id": "loop"})))
    db.commit()

    class Loop:
        requests = 1

        def open(self, url, **kw):
            raise RuntimeError(f"{url}: <HTTPError 308: 'The HTTP server returned a redirect error "
                               "that would lead to an infinite loop.'>")

    monkeypatch.setattr(fund_facts, "cache_dir", lambda name: tmp_path)
    monkeypatch.setattr(fund_facts.egress, "rotation", lambda **kw: contextlib.nullcontext(Loop()))
    assert "1 failed" in fund_facts.fetch(db)
    assert not (tmp_path / "1.gone").exists()
