# Architecture

## Why a database

Questions like "which funds held this ISIN in July" need joins across funds, so everything lands in
one SQLite file (`data/investing.sqlite3`, gitignored, filled by the fetchers). Time series are `WITHOUT ROWID`
keyed on `(entity, day)`, so one entity's whole history is contiguous on disk and reading it is a
single range scan.

Pages render from the database on request, so there is no build step after a refresh.

## What is in it

Mutual fund schemes with their NAV history, stocks with daily closes, fund holdings by month,
benchmark, currency, metal and CPI series, derived analysis documents, and IPO prospectus analyses
linked to the company once it lists.

## Layout

### `src/investing/`

| path | what it does |
|---|---|
| `config.py` | settings read from the environment and `.env` |
| `db.py` | schema, WAL, date helpers |
| `coin_tradebook.py` | the `coin-tradebook` command |
| `core/` | shared plumbing: `clock.py` (the IST clock), `enums.py` (shared string enums), `egress.py` (spreads fetches over the machine's own addresses) |
| `sources/` | one module per upstream feed |
| `analysis/nav.py` | NAV repair and screening: which funds publish bad data, and how to fix it |
| `analysis/metrics.py` | CAGR windows, rolling returns and SIP XIRR for the category documents |
| `analysis/longterm.py`, `arbitrage.py`, `attribution.py`, `tracking.py`, `blend.py` | the category comparison, the arbitrage category, active share and attribution, index fund tracking, hybrid benchmarks; each has the `write` or `write_all` function `refresh.py` calls to regenerate its documents |
| `nse/` | live NSE data: `client.py` (the site API client), `slb.py` (SLB quotes), `options.py` (option chains) |
| `web/app.py` | the FastAPI app: mounts `static/` and includes every router |
| `web/api.py` | the JSON API, read-only over the database |
| `web/pages.py` | the HTML site, rendered server-side from the same database; holds `TEMPLATES`, `STATIC` and `STATIC_VERSION` |
| `web/queries.py` | the read layer behind the pages and the API |
| `web/errors.py`, `web/ratelimit.py` | HTTP errors, per-address limits |
| `web/views/` | page logic: `currency.py` (the `/currency` calculator), `sif.py` (the `/sif` tracker), `portfolio.py` and `summary.py` (the portfolio and summary pages, each with its own router) |
| `web/templates/` | every page; the analyser's are under `ipo/` |
| `web/static/` | the stylesheet (no library) and the scripts |
| `ipo/` | IPO prospectus analysis and allotment lookup (`ipo/allotment/` for the registrars) |

### `scripts/`

| script | what it does |
|---|---|
| `refresh.py` | what the systemd timers run: take the refresh lock, fetch every source into the database, then regenerate the derived documents through the package's `write` functions |
| `deploy.sh` | update the server's git checkout and restart |

### Elsewhere

- `deploy/`: systemd user units for the API and the refresh timers.
- `tests/`: runs against the real database, or a small seeded one when there is none.
- `docs/`: how it works and why.

`uv run coin-tradebook --since 2019-04-01` downloads your Zerodha Console mutual-fund tradebooks,
one financial year at a time, for `/portfolio`.
