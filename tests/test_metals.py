"""Metals: the rupee gold and silver series, and what happens when Yahoo will not answer."""
import json
import unittest.mock as mock
from datetime import UTC, date, datetime, timedelta

import pytest

from investing.db import connect, day, undate
from investing.sources import metals

# Fixed so the recency guard does not overtake the hardcoded dates.
TODAY = date(2026, 9, 29)
FX = {TODAY - timedelta(days=n): 88.0 for n in range(6)}
NY_OFFSET = -14400


def chart(points: list[tuple[date, float]], gmtoffset: int = NY_OFFSET) -> dict:
    """A Yahoo chart payload with bars at exchange-local midnight."""
    return {"chart": {"error": None, "result": [{
        "meta": {"gmtoffset": gmtoffset, "symbol": "GC=F"},
        "timestamp": [int(datetime(d.year, d.month, d.day, 4, tzinfo=UTC).timestamp())
                      for d, _ in points],
        "indicators": {"quote": [{"close": [usd for _, usd in points]}]}}]}}


def test_unreachable_upstream_keeps_the_series_instead_of_failing_the_refresh():
    """A blocked feed keeps the stored series, and the result line says so."""
    blocked = RuntimeError("https://query1.finance.yahoo.com/v8/finance/chart/GC=F: HTTPError(429)")
    with mock.patch.object(metals, "usd_inr", return_value={}), \
         mock.patch.object(metals, "fetch_one", side_effect=blocked), \
         mock.patch.object(metals, "last_day", return_value=None):
        line = metals.fetch(object())
    assert "unreachable" in line, "the failure has to be visible in the line the refresh prints"
    assert "kept stored series" in line


def test_a_feed_that_answers_with_nonsense_still_fails():
    """Only a refusal is tolerated; unparseable or stale data still raises."""
    for message in ("gold-inr: only 3 rows; nothing written",
                    "gold-inr: newest futures close is 2026-01-01, more than 7 days old",
                    # egress's wording when decoding fails on every attempt.
                    "https://query1.finance.yahoo.com/v8/x: no usable JSON after 3 attempts"):
        with mock.patch.object(metals, "usd_inr", return_value={}), \
             mock.patch.object(metals, "fetch_one", side_effect=RuntimeError(message)):
            with pytest.raises(RuntimeError):
                metals.fetch(object())


@pytest.mark.parametrize(("code", "tolerated"), [
    ("Too Many Requests", True),
    ("Service Unavailable", True),
    ("Not Found", False),
    ("Bad Request", False),
    ("Internal Server Error", False),
    ("Weird New Code", False),
])
def test_only_a_recognised_transient_refusal_keeps_the_stored_series(code, tolerated):
    """Yahoo answers HTTP 200 with an error object for every failure; only listed transient codes
    are tolerated."""
    payload = {"chart": {"result": None, "error": {"code": code, "description": code}}}
    with mock.patch.object(metals, "usd_inr", return_value=FX), \
         mock.patch.object(metals, "yahoo_json", return_value=payload), \
         mock.patch.object(metals, "last_day", return_value=None):
        if tolerated:
            assert "kept stored series" in metals.fetch(object())
        else:
            with pytest.raises(RuntimeError, match="not a transient refusal"):
                metals.fetch(object())


def test_a_refusal_is_not_cached(tmp_path):
    """A cached refusal would block every fetch until the cache expires."""
    refusal = {"chart": {"result": None, "error": {"code": "Too Many Requests",
                                                   "description": "Too Many Requests"}}}
    pool = mock.MagicMock()
    pool.get_json.return_value = refusal
    with mock.patch.object(metals, "CACHE", tmp_path), \
         mock.patch.object(metals.egress, "rotation") as rotation:
        rotation.return_value.__enter__.return_value = pool
        with pytest.raises(RuntimeError):
            metals.yahoo_json("GC=F")
    assert list(tmp_path.iterdir()) == [], "a refusal must not become the cached payload"


def test_the_session_in_progress_is_not_stored_as_a_close():
    """Yahoo serves today's unfinished session as a daily bar carrying the live price."""
    exchange_today = (datetime.now(tz=UTC) + timedelta(seconds=NY_OFFSET)).date()
    yesterday = exchange_today - timedelta(days=1)
    got = metals.usd_records(chart([(yesterday, 4000.0), (exchange_today, 4123.0)]))
    assert [d for d, _ in got] == [yesterday], "the day in progress is not a close"


def test_mismatched_timestamp_and_close_arrays_fail_loudly():
    """The arrays are positional; a length mismatch would shift every date after the gap."""
    payload = chart([(date(2026, 9, 20), 4000.0), (date(2026, 9, 21), 4010.0)])
    payload["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [4000.0]
    with pytest.raises(RuntimeError, match="timestamps and 1 closes"):
        metals.usd_records(payload)


def test_a_fetch_replaces_older_rows_and_does_not_claim_an_auction_fix(tmp_path):
    """Rows older than Yahoo's 2000-08-30 start are removed, and the metadata calls it a COMEX close."""
    db = connect(tmp_path / "t.sqlite3")
    db.execute("INSERT INTO series (name, d, v) VALUES (?,?,?)",
               ("silver-inr", day(date(2000, 1, 13)), 7777.0))
    db.commit()

    prices = [(TODAY - timedelta(days=2), 48.0), (TODAY - timedelta(days=1), 48.5),
              (TODAY, 49.0)]
    with mock.patch.object(metals, "yahoo_json", return_value={}), \
         mock.patch.object(metals, "usd_records", return_value=prices), \
         mock.patch.object(metals, "check_recency", return_value=None), \
         mock.patch.object(metals, "MIN_ROWS", 1):
        written = metals.fetch_one(db, "silver-inr", FX)

    assert written == len(prices)
    days = [undate(r[0]) for r in
            db.execute("SELECT d FROM series WHERE name='silver-inr' ORDER BY d")]
    assert days == [d for d, _ in prices], "older rows must not survive"

    meta = json.loads(db.execute(
        "SELECT meta FROM series_meta WHERE name='silver-inr'").fetchone()[0])
    assert meta["first"] == prices[0][0].isoformat()
    blob = " ".join(str(v) for v in meta.values())
    assert "query1.finance.yahoo.com" in meta["source"] and "SI=F" in meta["source"]
    assert "futures close" in blob and "not an auction fix" in blob
    assert "1.59%" in blob, "silver's measured basis is the number that stops it being misread"
