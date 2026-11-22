# Working on this repo

    uv sync                       # once
    uv run pytest                 # no network
    uv run ruff check
    uv run uvicorn investing.web.app:app --reload   # http://127.0.0.1:8000

`python3 -m pytest` does not work: the API and prospectus modules need the project's dependencies.

## Tests on a fresh clone

The suite needs no fetched data. When `data/investing.sqlite3` is absent, `tests/conftest.py` builds
a small seeded database through the real schema and points `investing.config.DB_PATH` at it. Set
`INVESTING_TEST_SEED=1` to use the seed even when you have the real database.

- A test marked `@pytest.mark.real_db` needs the full database and skips on the seed.
- A test skips when it needs cached upstream files (bhavcopy zips, IBJA PDFs, a gold feed) or the
  `pdftotext` or `bwrap` binary.

Tests never use the network: mock at the fetcher boundary or parse a recorded payload. Never put
real personal data in a test. See [AGENTS.md](AGENTS.md) for what a good test asserts.

CI (`.github/workflows/ci.yml`) runs `uv sync`, `uv run ruff check` and `uv run pytest` on every push.

## The rules that matter

**`db.py` owns the schema.** Add columns there and nowhere else. `connect()` refuses a
database with an older schema.

**`analysis/nav.py` is accumulated knowledge about which funds carry bad data** (the Axis zero on
2013-04-07, UTI Liquid's 1/10 redenomination, quant's bonus issues). A wrong repair changes every
return computed across it, so change it with care.

**Assert on real values, not shapes.** An off-by-one read still has the right row count. Web tests
follow links from a page instead of stopping at its 200.

## Dates

INTEGER days since 1970-01-01 in the database, `YYYY-MM-DD` at every boundary. Use `day()` and
`undate()` from `db.py`. For the current Indian market date use
`today_ist()` from `investing/core/clock.py`, not `date.today()`.

## Code rules

- Raise HTTP errors through `investing/web/errors.py` (`not_found`, `bad_request`, `rate_limited` and so
  on), not `HTTPException` in a route.
- Repeated string literals are StrEnums in `investing/core/enums.py`.
- Use `logging.getLogger(__name__)` in each module. No `print()` in library code.
- `scripts/` holds only `refresh.py` and `deploy.sh`. A new document builder is a package function
  that takes a `sqlite3.Connection`, returns its summary line, and is listed in `refresh.py`'s `DERIVED`.
- Write route paths as full string literals, so a grep for a URL finds its handler.
- Validate input at the boundary with Pydantic or FastAPI parameters.
- An async handler never blocks: call blocking code through `asyncio.to_thread`.
- Give every query an explicit `ORDER BY` with a tie-breaker.
- Fix a lint finding instead of adding `# noqa`.
- A comment explains why, in the present tense, and leaves out history.
- No em dashes and no en dashes in code, comments or copy.
- Personal data (PANs, names, tradebooks) is never logged, cached or stored server-side.

## Configuration

Every key is optional. The real environment wins over `.env`. [.env.example](.env.example) lists all
of them with defaults. By group:

- General: `INVESTING_USER_AGENT`, `INVESTING_ENV_FILE` (an alternative to `.env`, read from the real
  environment only), `INVESTING_DB`, `INVESTING_CACHE`, `INVESTING_ADDRESSES`.
- Rate limits: `INVESTING_RATE_LIMIT_NSE`, `INVESTING_RATE_LIMIT_ALLOTMENT`,
  `INVESTING_RATE_LIMIT_ANALYZE`.
- Prospectus analyser: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `RHP_DEFAULT_MODEL`,
  `RHP_DEFAULT_RETRIES`, `RHP_MAX_PDF_BYTES`, `RHP_SECTION_CONCURRENCY`, `RHP_JOB_CONCURRENCY`,
  `RHP_DATABASE_PATH`, `RHP_API_TOKENS`.
- Allotment lookup: `IPO_ALLOTMENT_TIMEOUT_SECONDS`, `IPO_ALLOTMENT_CATALOGUE_TTL_SECONDS`,
  `KFINTECH_PROXY_URL`, `PURVA_PROXY_URL`.
- coin-tradebook: `CONSOLE_COOKIE`, `CONSOLE_CSRF`.
- `scripts/deploy.sh`: `DEPLOY_HOST`, `DEPLOY_DEST`, `DEPLOY_BRANCH`, `DEPLOY_REF`, `DEPLOY_SSH_OPTS`.

Never commit `.env`, the database, `tradebooks/` or `reports/`.
