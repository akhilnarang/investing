"""Offline tests for the prospectus package. No network, no OpenAI, no secrets."""
import sqlite3
from pathlib import Path

import pytest

from investing.db import connect, writing
from investing.ipo.cache import CacheStore, analysis_slug, company_slug
from investing.ipo.link import link_analyses, normalize, prospectus_for, resolve
from investing.ipo.synthesis import clean_company_name

# Filed as "Meridian" but the document is Milky Mist's; the name comes from the document.
MILKY_MIST = "Milky Mist Dairy Food Limited"
MILKY_MIST_ISIN = "INE00IT01020"


def rhp_db(path: Path) -> sqlite3.Connection:
    """A main database; connect() creates the prospectus tables with the rest of the schema."""
    return connect(path)


def put_job(store: CacheStore, analysis_id: str = "a" * 64, *, filename: str = "DRHP.pdf",
            sha: str = "c" * 64, status: str = "completed", market_data: dict | None = None) -> None:
    store.put_job(analysis_id=analysis_id, extraction_key="b" * 64, pdf_sha256=sha, filename=filename,
                  pdf_bytes=10, model="m", retries=0, sections=["offer"], market_data=market_data or {},
                  pdf_path=None, status=status)


def test_normalize_survives_nse_truncation():
    """NSE caps its names at 25 characters, so the listed Milky Mist loses most of 'LIMITED'."""
    assert normalize(MILKY_MIST) == "milky mist dairy food"
    assert normalize("MILKY MIST DAIRY FOOD L") == "milky mist dairy food"
    # The legal form is dropped.
    assert normalize("Acme Pvt. Ltd.") == normalize("ACME PRIVATE LIMITED") == "acme"
    assert normalize("Tips & Toes Ltd") == "tips and toes"


def test_resolve_links_a_drhp_to_a_listed_company(tmp_path):
    db = rhp_db(tmp_path / "t.sqlite3")
    with writing(db):
        db.execute("INSERT INTO company VALUES (?,?,?,?)",
                   (MILKY_MIST_ISIN, "MILKYMIST", "MILKY MIST DAIRY FOOD L", None))
    assert resolve(db, MILKY_MIST) == MILKY_MIST_ISIN


def test_resolve_refuses_a_short_name_that_prefixes_everything(tmp_path):
    """NSE lists a company called 'MIL', a prefix of hundreds of names."""
    db = rhp_db(tmp_path / "t.sqlite3")
    with writing(db):
        db.executemany("INSERT INTO company VALUES (?,?,?,?)", [
            ("INE087J01028", "MIL", "MIL", None),
            (MILKY_MIST_ISIN, "MILKYMIST", "MILKY MIST DAIRY FOOD L", None),
        ])
    assert resolve(db, MILKY_MIST) == MILKY_MIST_ISIN
    assert resolve(db, "MIL Limited") is None


def test_resolve_refuses_an_ambiguous_share_class(tmp_path):
    """One issuer with two ISINs (a DVR class) resolves to nothing."""
    db = rhp_db(tmp_path / "t.sqlite3")
    with writing(db):
        db.executemany("INSERT INTO company VALUES (?,?,?,?)", [
            ("INE00NI01015", None, "USHANTI COLOUR CHEM LTD", None),
            ("INE00NI20015", None, "USHANTI COLOUR CHEM LTD", None),
        ])
    assert resolve(db, "Ushanti Colour Chem Limited") is None


def test_resolve_does_not_swallow_a_longer_different_company(tmp_path):
    """Only a name at NSE's 25-character cap may be a truncated prefix."""
    db = rhp_db(tmp_path / "t.sqlite3")
    with writing(db):
        db.execute("INSERT INTO company VALUES ('INE000A01011', NULL, 'SUN PHARMA', NULL)")
    assert resolve(db, "Sun Pharmaceutical Advanced Research Company Limited") is None
    assert resolve(db, "Sun Pharma Limited") == "INE000A01011"


def test_link_is_nullable_and_filled_in_after_listing(tmp_path):
    """A prospectus is analyzed while the company is private and linked after listing."""
    db = rhp_db(tmp_path / "t.sqlite3")
    store = CacheStore(tmp_path / "t.sqlite3")
    put_job(store)
    store.update_job_company_name("a" * 64, MILKY_MIST)

    # Pre-IPO: name known, no ISIN.
    job = store.get_job("a" * 64)
    assert job is not None
    assert job["company_name"] == MILKY_MIST
    assert job["isin"] is None

    with writing(db):
        db.execute("INSERT INTO company VALUES (?,?,?,?)",
                   (MILKY_MIST_ISIN, "MILKYMIST", "MILKY MIST DAIRY FOOD L", None))
    with writing(db):
        assert link_analyses(db) == [("a" * 64, MILKY_MIST, MILKY_MIST_ISIN)]

    job = store.get_job("a" * 64)
    assert job is not None and job["isin"] == MILKY_MIST_ISIN
    assert [r["analysis_id"] for r in prospectus_for(db, MILKY_MIST_ISIN)] == ["a" * 64]
    # Re-running changes nothing.
    with writing(db):
        assert link_analyses(db) == []


def test_link_reaches_the_price_series(tmp_path):
    """prospectus -> ISIN -> closes, in one query."""
    db = rhp_db(tmp_path / "t.sqlite3")
    store = CacheStore(tmp_path / "t.sqlite3")
    with writing(db):
        db.execute("INSERT INTO company VALUES (?,?,?,?)",
                   (MILKY_MIST_ISIN, "MILKYMIST", "MILKY MIST DAIRY FOOD L", None))
        db.executemany("INSERT INTO stock_price VALUES (?,?,?)",
                       [(MILKY_MIST_ISIN, 20683 + i, 300.0 + i) for i in range(5)])
    put_job(store)
    store.update_job_company_name("a" * 64, MILKY_MIST)

    row = db.execute(
        "SELECT company.name, count(stock_price.d) AS closes FROM analysis_jobs "
        "JOIN company ON company.isin = analysis_jobs.isin "
        "JOIN stock_price ON stock_price.isin = analysis_jobs.isin "
        "WHERE analysis_jobs.analysis_id = ?", ("a" * 64,)).fetchone()
    assert row["name"] == "MILKY MIST DAIRY FOOD L"
    assert row["closes"] == 5


def test_foreign_key_rejects_an_isin_that_is_not_a_company(tmp_path):
    db = rhp_db(tmp_path / "t.sqlite3")
    put_job(CacheStore(tmp_path / "t.sqlite3"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE analysis_jobs SET isin = 'INNOSUCHISIN' WHERE analysis_id = ?", ("a" * 64,))


# --- slugs and titles ------------------------------------------------------------------------


def test_slug_from_a_real_prospectus_filename():
    """The registration prefix and the DRHP marker come off."""
    assert analysis_slug("Registration_21072025200223_MeridianDRHP.pdf") == "meridian"
    assert analysis_slug("RHP-V6-Sumax-17.08.2026.pdf") == "sumax"
    assert company_slug(MILKY_MIST) == "milky-mist-dairy-food-limited"


def test_company_name_must_look_like_a_legal_name():
    assert clean_company_name(f"{MILKY_MIST} — DRHP Research Report") == MILKY_MIST
    assert clean_company_name("Executive Summary") is None
    assert clean_company_name("Milky Mist Dairy Food") is None


def test_slug_survives_a_rename(tmp_path):
    """/analysis/meridian keeps resolving after the company name is found."""
    rhp_db(tmp_path / "t.sqlite3")
    store = CacheStore(tmp_path / "t.sqlite3")
    put_job(store, filename="Registration_21072025200223_MeridianDRHP.pdf", status="queued")
    job = store.get_job("a" * 64)
    assert job is not None and job["public_slug"] == "meridian"
    store.update_job_company_name("a" * 64, MILKY_MIST)
    assert store.public_analysis_id("a" * 64) == "milky-mist-dairy-food-limited"
    assert store.resolve_analysis_id("meridian") == "a" * 64


# --- the job list --------------------------------------------------------------


def test_list_jobs_shows_one_entry_per_company(tmp_path):
    """The newest job per company is listed; unnamed jobs stay separate."""
    rhp_db(tmp_path / "t.sqlite3")
    store = CacheStore(tmp_path / "t.sqlite3")
    for n, subscription in enumerate(["0.96x", "1.15x", "2.53x"]):
        put_job(store, f"{n:064d}", market_data={"subscription": subscription})
        store.update_job_company_name(f"{n:064d}", MILKY_MIST)
    # Failed before a name was found.
    for n in (8, 9):
        put_job(store, f"{n:064d}", filename="broken.pdf", sha="d" * 64, status="failed")

    jobs = store.list_jobs()
    assert [j["company_name"] for j in jobs].count(MILKY_MIST) == 1, "one card per company"
    assert len(jobs) == 3, "the collapsed company plus both unnamed failures"
    named = next(j for j in jobs if j["company_name"] == MILKY_MIST)
    assert named["market_data"]["subscription"] == "2.53x", "the newest run, not the first"
