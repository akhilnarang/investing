"""The SQLite schema and connections. WAL, so the refresh can write while the API reads.

Dates are INTEGER days since 1970-01-01; convert with `day()` and `undate()`. Time-series tables are
WITHOUT ROWID keyed entity first, so one series is a single range scan.
"""
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from . import config

EPOCH = date(1970, 1, 1)
DEFAULT_PATH = config.DB_PATH  # INVESTING_DB


def day(d: date) -> int:
    return (d - EPOCH).days


def undate(n: int) -> date:
    return date.fromordinal(EPOCH.toordinal() + n)


# The prospectus tables. ipo/cache.py also uses them on their own.
RHP_SCHEMA = """
-- One extraction per (pdf, model, sections, prompt version). cache_key is the canonical hash of
-- those, so re-analyzing the same PDF with the same settings is free.
CREATE TABLE IF NOT EXISTS extraction_cache (
    cache_key      TEXT PRIMARY KEY,   -- sha256 of the extraction inputs
    pdf_sha256     TEXT NOT NULL,      -- the document itself, so the same PDF is recognized under any filename
    filename       TEXT NOT NULL,
    pdf_bytes      INTEGER NOT NULL,
    model          TEXT NOT NULL,
    retries        INTEGER NOT NULL,
    sections_json  TEXT NOT NULL,      -- JSON list of section names
    prompt_version TEXT NOT NULL,      -- bumping this invalidates every cached extraction
    records_json   TEXT NOT NULL,      -- JSON: per-section answers, evidence quotes and page numbers
    metadata_json  TEXT NOT NULL,      -- JSON: token usage and elapsed time
    created_at     TEXT NOT NULL       -- ISO-8601 UTC
);
CREATE INDEX IF NOT EXISTS extraction_pdf_sha256_idx ON extraction_cache(pdf_sha256);

-- The written report. Separate from the extraction because the report prompt changes far more often
-- than the extraction prompt, and re-writing a report must not re-read the PDF.
CREATE TABLE IF NOT EXISTS report_cache (
    cache_key       TEXT PRIMARY KEY,
    extraction_key  TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    report_markdown TEXT NOT NULL,
    metadata_json   TEXT NOT NULL,     -- JSON: usage, the caller's market data, the extracted company name
    created_at      TEXT NOT NULL,
    FOREIGN KEY(extraction_key) REFERENCES extraction_cache(cache_key)
);
CREATE INDEX IF NOT EXISTS report_extraction_key_idx ON report_cache(extraction_key);

-- One row per analysis the user asked for: the job that produces the two rows above, and afterwards
-- the record that they exist. analysis_id equals the report cache_key.
CREATE TABLE IF NOT EXISTS analysis_jobs (
    analysis_id         TEXT PRIMARY KEY,
    public_slug         TEXT,                        -- the URL: 'milky-mist-dairy-food' once the name is known
    public_slug_version INTEGER NOT NULL DEFAULT 3,  -- the slug scheme this row's public_slug was made with
    company_name        TEXT,                        -- legal issuer name as extracted, null until the report exists
    isin                TEXT REFERENCES company(isin),  -- null until the company lists; see ipo/link.py
    extraction_key      TEXT NOT NULL,
    pdf_sha256          TEXT NOT NULL,
    filename            TEXT NOT NULL,
    pdf_bytes           INTEGER NOT NULL,
    model               TEXT NOT NULL,
    retries             INTEGER NOT NULL,
    sections_json       TEXT NOT NULL,
    market_data_json    TEXT NOT NULL DEFAULT '{}',  -- JSON: caller-supplied GMP, price band and subscription
    pdf_path            TEXT,                        -- the queued upload, cleared once the job ends
    status              TEXT NOT NULL,               -- queued | running | completed | failed
    stage               TEXT NOT NULL,
    message             TEXT NOT NULL,
    completed_sections  INTEGER NOT NULL,
    total_sections      INTEGER NOT NULL,
    error               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS analysis_job_status_idx ON analysis_jobs(status);
CREATE UNIQUE INDEX IF NOT EXISTS analysis_job_public_slug_idx ON analysis_jobs(public_slug);
-- Keeps the prospectus and the market data one join apart: the prospectus for an ISIN, and from
-- there its price series and every fund that holds it.
CREATE INDEX IF NOT EXISTS analysis_job_isin_idx ON analysis_jobs(isin);

-- A slug the company was published under before its legal name was known, kept so an old link keeps
-- resolving after the slug changes from 'registration-meridian' to 'milky-mist-dairy-food'.
CREATE TABLE IF NOT EXISTS analysis_job_slug_aliases (
    public_slug TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL,
    FOREIGN KEY(analysis_id) REFERENCES analysis_jobs(analysis_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS analysis_job_slug_alias_analysis_idx ON analysis_job_slug_aliases(analysis_id);
"""

SCHEMA = (
    """
-- Every price, holding and prospectus hangs off an ISIN, so a fund's holding of a stock and that
-- stock's price series are one join apart.
CREATE TABLE IF NOT EXISTS company (
    isin    TEXT PRIMARY KEY,
    symbol  TEXT,
    name    TEXT NOT NULL,
    slug    TEXT
);
CREATE INDEX IF NOT EXISTS company_symbol_idx ON company(symbol);
CREATE INDEX IF NOT EXISTS company_slug_idx   ON company(slug);

-- AMFI scheme. meta is the per-fund dict, kept verbatim: its shape varies and nothing queries it.
CREATE TABLE IF NOT EXISTS fund (
    scheme_code  INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    isin_growth  TEXT,
    fund_house   TEXT,
    category     TEXT,
    plan         TEXT,          -- direct | regular
    meta         TEXT NOT NULL  -- JSON
);
CREATE INDEX IF NOT EXISTS fund_isin_idx     ON fund(isin_growth);
CREATE INDEX IF NOT EXISTS fund_category_idx ON fund(category, plan);

CREATE TABLE IF NOT EXISTS nav (
    scheme_code INTEGER NOT NULL,
    d           INTEGER NOT NULL,
    nav         REAL    NOT NULL,
    PRIMARY KEY (scheme_code, d)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS stock_price (
    isin  TEXT    NOT NULL,
    d     INTEGER NOT NULL,
    close REAL    NOT NULL,
    PRIMARY KEY (isin, d)
) WITHOUT ROWID;

-- Benchmarks, currencies, metals: one shape for everything that is a named series of numbers.
CREATE TABLE IF NOT EXISTS series (
    name TEXT NOT NULL,          -- 'NIFTY 500 TRI', 'usd-inr', 'dxy', 'gold-inr'
    d    INTEGER NOT NULL,
    v    REAL    NOT NULL,
    PRIMARY KEY (name, d)
) WITHOUT ROWID;

-- The importer sets kind, not the fetcher's own meta "kind" (price_index, blend, ...), which uses
-- other words. The CHECK makes a wrong kind fail at load.
CREATE TABLE IF NOT EXISTS series_meta (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('index', 'fx', 'metal', 'cpi')),
    meta TEXT NOT NULL           -- the fetcher's own meta dict, verbatim: source URLs, DXY basket, pegs
);

-- Disclosed monthly portfolios. "Which funds held this ISIN in July" is a two-column index lookup.
CREATE TABLE IF NOT EXISTS holding (
    scheme_code INTEGER NOT NULL,
    d           INTEGER NOT NULL,   -- portfolio date as disclosed, not the fetch date
    name        TEXT    NOT NULL,   -- security_name as the source spells it, NOT unique on its own
    isin        TEXT,               -- null for cash, TREPS, receivables and other non-securities
    company     TEXT,               -- issuer, where it differs from the instrument
    weight      REAL,               -- percent of net assets
    value       REAL,               -- market value in millions, as Kuvera reports it
    quantity    REAL,
    sector      TEXT,
    asset_class TEXT,
    side        TEXT,               -- long | short, for SIFs
    -- A portfolio can list one name many times ("India (Republic of)" for several bonds), and a SIF
    -- can hold one security long and short with no ISIN, so name, ISIN and side together are the key.
    -- SQLite treats NULLs as distinct in a PK, so the key uses COALESCEd copies. A generated column
    -- cannot be in a PK, so the importer writes them and a CHECK ties them to their source.
    isin_key    TEXT    NOT NULL CHECK (isin_key = COALESCE(isin, '')),
    side_key    TEXT    NOT NULL CHECK (side_key = COALESCE(side, '')),
    PRIMARY KEY (scheme_code, d, isin_key, side_key, name)
) WITHOUT ROWID;
-- Which funds held this ISIN, and when.
CREATE INDEX IF NOT EXISTS holding_isin_idx ON holding(isin, d);
CREATE INDEX IF NOT EXISTS holding_date_idx ON holding(d);

-- Corporate actions, so a price series can be joined back to why it moved.
CREATE TABLE IF NOT EXISTS corporate_action (
    isin   TEXT    NOT NULL,
    d      INTEGER NOT NULL,
    kind   TEXT    NOT NULL,   -- split | dividend | bonus
    ratio  REAL,
    amount REAL,
    PRIMARY KEY (isin, d, kind)
) WITHOUT ROWID;

"""
    + RHP_SCHEMA
    + """
-- Derived analysis documents (comparisons, active share, attribution, tracking, TER, fund facts),
-- one per kind and slug. Written and read whole as JSON.
CREATE TABLE IF NOT EXISTS analysis (
    kind      TEXT NOT NULL,     -- longterm | active_share | attribution | index_tracking | ter | fund_facts
    slug      TEXT NOT NULL,     -- 'flexi-cap', 'flexi-cap-regular', 'mid-cap-10y', or '' when there is one
    as_of     TEXT,              -- the analysis date the document carries, for staleness
    body      TEXT NOT NULL,     -- JSON, exactly as the analysis script wrote it
    PRIMARY KEY (kind, slug)
) WITHOUT ROWID;

-- Kuvera's own scheme codes (PP001ZG-GR) mapped to AMFI codes by ISIN. Kept because resolving
-- costs requests against a rate-limited API.
CREATE TABLE IF NOT EXISTS kuvera_code (
    scheme_code INTEGER PRIMARY KEY,
    kuvera_code TEXT NOT NULL,
    isin        TEXT NOT NULL,      -- the ISIN both sides agreed on
    kuvera_name TEXT,
    resolved_at TEXT NOT NULL
) WITHOUT ROWID;

-- Bookkeeping: which importer last touched what, so an incremental refresh knows where to resume.
CREATE TABLE IF NOT EXISTS source_state (
    source     TEXT PRIMARY KEY,
    last_run   TEXT,
    last_data  INTEGER,        -- newest data day seen, not wall clock
    note       TEXT
);
""")

def _check_shape(db: sqlite3.Connection) -> None:
    """Raise if an existing table's columns differ from the current schema.

    CREATE TABLE IF NOT EXISTS leaves an old table as it is. The expected columns come from building
    the schema in an in-memory database, so the DDL is parsed by SQLite.
    """
    have = {r[0]: {c[1] for c in db.execute(f"PRAGMA table_info({r[0]})")}
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not have:
        return
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(SCHEMA)
        want = {r[0]: {c[1] for c in reference.execute(f"PRAGMA table_info({r[0]})")}
                for r in reference.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        reference.close()
    for table, cols in want.items():
        seen = have.get(table)
        if seen and seen != cols:
            missing, extra = sorted(cols - seen), sorted(seen - cols)
            raise RuntimeError(
                f"{table} in this database predates the current schema"
                + (f"; missing {missing}" if missing else "")
                + (f"; unexpected {extra}" if extra else "")
                + ". Rebuild the file, or migrate the table, before writing to it.")


def connect(path: Path | str = DEFAULT_PATH, *, readonly: bool = False,
            check_same_thread: bool = True) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=check_same_thread)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, isolation_level=None, check_same_thread=check_same_thread)
        db.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
        _check_shape(db)          # before SCHEMA, whose CREATE INDEX would fail on a stale table
        db.executescript(SCHEMA)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


# FastAPI runs a sync dependency and the endpoint on different threads; one request owns the handle.
def get_db() -> Iterator[sqlite3.Connection]:
    """One read-only connection per request, closed when the request ends."""
    db = connect(readonly=True, check_same_thread=False)
    try:
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


@contextmanager
def writing(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One transaction around a bulk load."""
    db.execute("BEGIN")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
