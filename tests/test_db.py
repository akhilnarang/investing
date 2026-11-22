import sqlite3
from datetime import date

import pytest

from investing.analysis.nav import MIN_POINTS, load_db
from investing.db import connect, day, undate, writing


def test_date_roundtrip():
    assert day(date(1970, 1, 1)) == 0
    for d in (date(1999, 12, 31), date(2026, 9, 23), date(2000, 2, 29)):
        assert undate(day(d)) == d


def test_nav_roundtrip_and_screening(tmp_path):
    db = connect(tmp_path / "t.sqlite3")
    start = date(2020, 1, 1)
    rows = [(1, day(start) + i, 100.0 + i) for i in range(MIN_POINTS + 10)]
    with writing(db):
        db.executemany("INSERT INTO nav VALUES (?,?,?)", rows)
    s = load_db(1, db)
    assert s is not None and len(s) == len(rows)
    assert s[start] == 100.0

    # Below MIN_POINTS there is no series.
    with writing(db):
        db.executemany("INSERT INTO nav VALUES (?,?,?)",
                       [(2, day(start) + i, 50.0) for i in range(MIN_POINTS - 1)])
    assert load_db(2, db) is None


def test_rollback_leaves_nothing(tmp_path):
    db = connect(tmp_path / "t.sqlite3")
    try:
        with writing(db):
            db.execute("INSERT INTO company VALUES ('X', 'X', 'X', 'x')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert db.execute("SELECT count(*) FROM company").fetchone()[0] == 0


def test_reopening_a_current_database_is_allowed(tmp_path):
    """The shape check accepts the file connect() itself just created."""
    p = tmp_path / "t.sqlite3"
    for _ in range(3):
        connect(p).close()


def test_a_stale_table_is_still_rejected(tmp_path):
    p = tmp_path / "stale.sqlite3"
    raw = sqlite3.connect(p)
    raw.executescript("CREATE TABLE holding (scheme_code INTEGER, d INTEGER, name TEXT,"
                      " PRIMARY KEY (scheme_code, d, name)) WITHOUT ROWID;")
    raw.close()
    with pytest.raises(RuntimeError, match="predates the current schema"):
        connect(p)
