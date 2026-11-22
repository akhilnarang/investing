# investing

Indian investing data in one SQLite database: mutual funds, SIFs, stocks, currencies, metals and IPO
prospectuses, served as a FastAPI site and JSON API.

## Quick start

Needs Linux, Python 3.15 and [uv](https://docs.astral.sh/uv/). Prospectus analysis also needs
`bwrap`, `pdftotext` and `prlimit` on `PATH`.

    uv sync
    cp .env.example .env          # optional: every key has a default
    uv run python scripts/refresh.py --daily
    uv run fastapi dev

The first refresh pulls the full AMFI NAV history and takes about an hour. `uv run pytest` does not
need it; it builds a small seeded database.

`uv run coin-tradebook --since 2019-04-01` downloads your Zerodha Console mutual-fund tradebooks for
the /portfolio page, which reads them in the browser.

## Docs

- [Architecture](docs/architecture.md): why a database, what is in it, where the code lives.
- [Refreshing the data](docs/refresh.md): sources, cadence, order, caching and the timers.
- [Deploying](docs/deploy.md): the git-based deploy, and what a public deployment must set up.
- [Data sources and terms](docs/data-sources.md): where the data comes from and what the fetchers do.
- [Contributing](CONTRIBUTING.md).

## License

MIT; see [LICENSE](LICENSE).
