from datetime import UTC, datetime, timedelta

from investing.sources import kuvera


def test_unmatched_fund_is_skipped_until_due():
    now = datetime(2026, 10, 9, tzinfo=UTC)
    misses = {"1": (now - timedelta(days=3)).isoformat(), "2": (now - timedelta(days=30)).isoformat()}
    assert not kuvera._due(misses, 1, now)
    assert kuvera._due(misses, 2, now)
    assert kuvera._due(misses, 3, now)


def test_an_unreachable_candidate_is_not_a_confirmed_miss(monkeypatch):
    def down(code):
        raise OSError("timed out")

    monkeypatch.setattr(kuvera, "scheme_isin", down)
    entries = [{"code": "K1", "name": "Alpha Flexi Cap Fund Direct Growth"}]
    try:
        kuvera.resolve("Alpha Flexi Cap Fund - Direct - Growth", {"INE1"}, entries)
    except RuntimeError:
        pass
    else:
        raise AssertionError("an outage must not read as 'no such fund'")
    monkeypatch.setattr(kuvera, "scheme_isin", lambda code: "INE9")
    assert kuvera.resolve("Alpha Flexi Cap Fund - Direct - Growth", {"INE1"}, entries) is None
