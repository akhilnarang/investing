"""CPI, metals and IBJA: the maths directly, and the fetchers from their cached upstream."""
import json
import shutil
import subprocess
from datetime import date

import pytest

from investing.core.clock import month_ends, today_ist
from investing.db import connect, undate
from investing.sources import cpi, ibja, metals

# ---------------------------------------------------------------- CPI

# India's published World Bank rates.
INDIA = {2000: 4.009, 2001: 3.779, 2002: 4.297, 2003: 3.806, 2004: 3.767, 2005: 4.246,
         2006: 6.373, 2007: 6.373, 2008: 8.349, 2009: 10.882, 2010: 11.989, 2011: 8.858,
         2012: 9.312, 2013: 10.908, 2014: 6.353, 2015: 5.872, 2016: 4.941, 2017: 2.491,
         2018: 4.861, 2019: 7.660, 2020: 6.623, 2021: 5.131, 2022: 6.699, 2023: 5.649,
         2024: 4.953}


def test_january_2000_is_one_month_of_inflation_above_the_base():
    """The base is 100 on 1 January 2000, so the 31 January point carries one month of inflation."""
    series, _ = cpi.levels(INDIA, 2000, date(2026, 9, 24))
    first = min(series)
    assert first == date(2000, 1, 31)
    assert series[first] == pytest.approx(100 * (1 + 4.009 / 100) ** (1 / 12), rel=1e-12)
    assert 100.3 < series[first] < 100.35


def test_a_completed_year_is_exactly_twelve_points_and_compounds_to_its_rate():
    """2015's twelve month ends compound to exactly 5.872%."""
    series, _ = cpi.levels(INDIA, 2000, date(2026, 9, 24))
    in_2015 = sorted(d for d in series if d.year == 2015)
    assert len(in_2015) == 12
    assert [d.month for d in in_2015] == list(range(1, 13))
    dec14 = series[date(2014, 12, 31)]
    assert series[in_2015[-1]] / dec14 == pytest.approx(1.05872, rel=1e-9)


def test_cumulative_inflation_since_2000_lands_in_a_sane_band():
    series, _ = cpi.levels(INDIA, 2000, date(2026, 9, 24))
    assert 3.5 < series[max(series)] / 100 < 6.0


def test_unpublished_years_carry_the_last_rate_and_are_flagged():
    """2025 and 2026 are not in INDIA, so they compound at 2024's 4.953% and say so."""
    series, flags = cpi.levels(INDIA, 2000, date(2026, 9, 24))
    assert flags["assumed_from"] == "2025-01-01"
    assert (series[date(2025, 12, 31)] / series[date(2024, 12, 31)]
            == pytest.approx(1.04953, rel=1e-9))


def test_the_month_in_progress_is_prorated_not_booked_whole():
    """A run on the 24th carries 24/30ths of September, dated the 24th."""
    series, flags = cpi.levels(INDIA, 2000, date(2026, 9, 24))
    assert flags["partial_day"] == "2026-09-24"
    assert max(series) == date(2026, 9, 24)
    monthly = (1 + 4.953 / 100) ** (1 / 12)
    assert (series[date(2026, 9, 24)] / series[date(2026, 8, 31)]
            == pytest.approx(monthly ** (24 / 30), rel=1e-12))


def test_a_run_on_a_month_end_books_the_whole_month():
    series, flags = cpi.levels(INDIA, 2000, date(2026, 9, 30))
    assert flags["partial_day"] is None
    assert (series[date(2026, 9, 30)] / series[date(2026, 8, 31)]
            == pytest.approx((1 + 4.953 / 100) ** (1 / 12), rel=1e-12))


def test_month_ends_are_real_month_ends():
    ends = month_ends(date(2024, 12, 31), 12)
    assert ends[1] == date(2024, 2, 29)      # leap year
    assert [d.day for d in ends] == [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


# ---------------------------------------------------------------- customs duty

def test_the_july_2024_duty_cut_is_applied_on_the_right_day():
    """15% to 6% on 24 July 2024."""
    assert metals.duty_on(date(2024, 7, 23)) == 15.0
    assert metals.duty_on(date(2024, 7, 24)) == 6.0


def test_every_duty_step_holds_on_its_boundary():
    from datetime import timedelta
    schedule = metals.DUTY
    for i, (start, pct) in enumerate(schedule):
        assert metals.duty_on(start) == pct, f"{start} should be {pct}%"
        if i:
            assert metals.duty_on(start - timedelta(days=1)) == schedule[i - 1][1]


def test_the_duty_schedule_is_the_one_the_upstream_fetcher_carries():
    """Customs duty history on gold and silver."""
    assert metals.DUTY == [
        (date(2000, 1, 1), 0.0), (date(2012, 1, 17), 2.0), (date(2012, 3, 17), 4.0),
        (date(2013, 1, 22), 6.0), (date(2013, 6, 5), 8.0), (date(2013, 8, 13), 10.0),
        (date(2019, 7, 6), 12.5), (date(2021, 2, 2), 10.75), (date(2022, 7, 1), 15.0),
        (date(2024, 7, 24), 6.0), (date(2026, 5, 13), 15.0)]


def test_pre_2012_carries_no_duty():
    assert metals.duty_on(date(2005, 6, 1)) == 0.0
    assert metals.duty_on(date(2012, 1, 16)) == 0.0


# ---------------------------------------------------------------- metal pricing

def test_gold_is_priced_per_ten_grams_with_the_duty_of_the_day():
    """$2400/oz at 83.5 rupees, per 10 g, plus the day's duty."""
    usd = [(date(2024, 7, 23), 2400.0), (date(2024, 7, 24), 2400.0)]
    fx = {date(2024, 7, 23): 83.5, date(2024, 7, 24): 83.5}
    series = metals.rupee_series(usd, fx, metals.METALS["gold-inr"]["per_ounce"])
    bare = 2400.0 * 83.5 * 10 / 31.1035
    assert series[date(2024, 7, 23)] == pytest.approx(bare * 1.15, rel=1e-12)
    assert series[date(2024, 7, 24)] == pytest.approx(bare * 1.06, rel=1e-12)
    assert 66000 < series[date(2024, 7, 24)] < 70000      # a plausible mid-2024 rate per 10 g


def test_silver_is_priced_per_kilogram():
    usd = [(date(2024, 7, 24), 29.0)]
    fx = {date(2024, 7, 24): 83.5}
    series = metals.rupee_series(usd, fx, metals.METALS["silver-inr"]["per_ounce"])
    assert series[date(2024, 7, 24)] == pytest.approx(29.0 * 83.5 * 1000 / 31.1035 * 1.06, rel=1e-12)
    assert 75000 < series[date(2024, 7, 24)] < 90000


def test_a_stale_fx_fix_is_not_carried_forward_forever():
    """A day months past the last FX fix is dropped, not priced at the old rate."""
    fx = {date(2024, 1, 2): 83.0}
    usd = [(date(2024, 1, 5), 2000.0), (date(2024, 6, 5), 2000.0)]
    series = metals.rupee_series(usd, fx, metals.METALS["gold-inr"]["per_ounce"])
    assert set(series) == {date(2024, 1, 5)}      # the June day is five months from the last fix


def _chart(timestamps, closes, error=None):
    """A Yahoo chart payload: timestamps zipped against quote closes."""
    if error is not None:
        return {"chart": {"error": error, "result": None}}
    return {"chart": {"error": None, "result": [
        {"timestamp": list(timestamps), "indicators": {"quote": [{"close": list(closes)}]}}]}}


def test_usd_records_reject_rubbish_and_sort():
    """Null and negative closes are dropped and the rest sorted."""
    # Exchange-local midnights, out of order.
    raw = _chart([1721779200, 1721606400, 1721692800, 1721865600],
                 [2400.0, 2390.0, None, -1.0])
    assert metals.usd_records(raw) == [(date(2024, 7, 22), 2390.0), (date(2024, 7, 24), 2400.0)]


def test_a_yahoo_error_object_splits_throttle_from_unknown_symbol():
    """Yahoo answers HTTP 200 with an error object for both; only a throttle raises Throttled."""
    with pytest.raises(metals.Throttled):
        metals.usd_records(_chart(None, None, error={"code": "Too Many Requests", "description": "x"}))
    with pytest.raises(RuntimeError) as caught:
        metals.usd_records(_chart(None, None, error={"code": "Not Found", "description": "no symbol"}))
    assert not isinstance(caught.value, metals.Throttled), "an unknown symbol must not be tolerated"


def test_a_stale_feed_refuses_to_rewrite():
    today = date(2026, 9, 24)
    assert metals.check_recency(date(2026, 9, 23), date(2026, 9, 22), today) is None
    why = metals.check_recency(date(2026, 8, 1), date(2026, 8, 1), today)
    assert why is not None and "more than" in why
    why = metals.check_recency(date(2026, 9, 23), date(2026, 8, 1), today)
    assert why is not None and "behind" in why


CACHED_GOLD = metals.CACHE / "GC=F.json"


@pytest.mark.skipif(not CACHED_GOLD.exists(), reason="no cached gold feed")
def test_the_real_gold_feed_prices_a_recent_day_in_a_plausible_band():
    """The cached feed at a fixed USD/INR gives gold per 10 g in a plausible band, duty included."""
    usd = metals.usd_records(json.loads(CACHED_GOLD.read_text()))
    recent = [(d, price) for d, price in usd if d >= date(2024, 7, 24)]
    assert recent, "the cached feed stops before mid-2024"
    fx = {d: 84.0 for d, _ in recent}
    series = metals.rupee_series(recent, fx, metals.METALS["gold-inr"]["per_ounce"],
                                 since=date(2024, 7, 24))
    assert all(50_000 < v < 250_000 for v in series.values())
    d, usd = recent[0]
    assert series[d] / (usd * 84.0 * 10 / 31.1035) == pytest.approx(1.06, rel=1e-9)


# ---------------------------------------------------------------- IBJA

PDFS = sorted(ibja.CACHE.glob("*.pdf"))


@pytest.fixture(scope="module")
def text():
    """The newest cached IBJA rate card as text; skipped when there is none or no pdftotext."""
    if not PDFS:
        pytest.skip("no cached IBJA PDF to parse")
    if shutil.which("pdftotext") is None:
        pytest.skip("pdftotext is not installed")
    return subprocess.run(["pdftotext", "-layout", "-", "-"], input=PDFS[-1].read_bytes(),
                          capture_output=True, check=True).stdout.decode("utf-8", "replace")


def test_the_ibja_pdf_parses_into_real_rates(text):
    rows = ibja.parse(text)
    assert len(rows) >= ibja.MIN_ROWS
    for d, v in rows.items():
        assert date(2020, 1, 1) < d <= today_ist()
        # Gold 999 per 10 g and silver 999 per kg, in rupees.
        assert 30_000 < v["gold_pm"] < 400_000, f"{d}: gold {v['gold_pm']}"
        assert 40_000 < v["silver_pm"] < 600_000, f"{d}: silver {v['silver_pm']}"
        # AM and PM fixes are within a few percent.
        assert 0.85 < v["gold_am"] / v["gold_pm"] < 1.15
        assert 0.85 < v["silver_am"] / v["silver_pm"] < 1.15


def test_gold_999_is_not_a_lower_purity_column(text):
    """The parse is positional and gold 916 reads close to 999; silver per kg anchors the columns."""
    rows = ibja.parse(text)
    for d, v in rows.items():
        assert v["silver_pm"] / v["gold_pm"] > 1.0, f"{d}: silver looks like a gold column"
        assert v["gold_pm"] > 100_000, f"{d}: gold 999 looks like a lower purity"


def test_a_reordered_header_refuses_to_parse(text):
    with pytest.raises(ValueError):
        ibja.parse(text.replace("Gold 999", "Gold ZZZ"))


def test_ibja_merges_rather_than_replacing_its_history(tmp_path, text):
    """The source is a rolling 30-day window; older stored days survive and a re-run changes nothing."""
    db = connect(tmp_path / "t.sqlite3")
    old = date(2020, 1, 2)
    db.execute("INSERT INTO series_meta (name, kind, meta) VALUES ('ibja-gold','metal','{}')")
    db.execute("INSERT INTO series (name, d, v) VALUES ('ibja-gold', ?, 40000.0)",
               ((old - date(1970, 1, 1)).days,))
    ibja.fetch(db, pdf=PDFS[-1].read_bytes())
    kept = {undate(d): v for d, v in db.execute("SELECT d, v FROM series WHERE name='ibja-gold'")}
    assert kept[old] == 40000.0, "the pre-window day was dropped"
    assert len(kept) == len(ibja.parse(text)) + 1
    assert db.execute("SELECT kind FROM series_meta WHERE name='ibja-gold'").fetchone()[0] == "metal"
    before = sorted(kept.items())
    ibja.fetch(db, pdf=PDFS[-1].read_bytes())
    after = sorted((undate(d), v) for d, v in
                   db.execute("SELECT d, v FROM series WHERE name='ibja-gold'"))
    assert after == before


def test_ibja_stores_the_pm_fix(tmp_path, text):
    db = connect(tmp_path / "t.sqlite3")
    ibja.fetch(db, pdf=PDFS[-1].read_bytes())
    rows = ibja.parse(text)
    d = max(rows)
    got = db.execute("SELECT v FROM series WHERE name='ibja-silver' AND d=?",
                     ((d - date(1970, 1, 1)).days,)).fetchone()[0]
    assert got == float(rows[d]["silver_pm"])
