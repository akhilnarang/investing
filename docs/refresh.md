# Refreshing the data

Every source is fetched by this repo, straight into the database.

    uv run python scripts/refresh.py              # every source
    uv run python scripts/refresh.py --daily      # the ones that change every trading day
    uv run python scripts/refresh.py --weekly     # daily plus the weekly ones
    uv run python scripts/refresh.py --only fx,amfi
    uv run python scripts/refresh.py --check      # sanity checks only, fetches nothing

The server refreshes itself (see Scheduling). `scripts/deploy.sh --with-db` copies a local database up
instead, for example to seed a new server.

## Cadence

| | sources | why |
|---|---|---|
| `--daily` | amfi, bhavcopy, fx, nse_index, sif | NAVs and closes move every trading day |
| `--weekly` | every source: plus universe, kuvera, fund_facts, us_indexes, metals, ibja, events, constituents, cpi, ter, amc | disclosures, holdings, expense ratios and corporate actions move slowly |

AMFI publishes NAVs late in the evening, so a run before about 21:00 IST gets the previous day's.
NSE's bhavcopy lands soon after the market closes. An early run is harmless:
`bhavcopy` leaves an unpublished day to the next run instead of recording a holiday.

## Order

`refresh.py` runs them in dependency order and carries on past a failure.

- `amfi` first: it creates the `fund` rows `universe`, `ter`, `kuvera` and `amc` all key off.
- `fx` before `us_indexes`, which reads the `usd-inr` series back out rather than refetching it.
- `bhavcopy` before `events`, which takes its symbols from the `company` table.

## Derived documents

After the fetches, `refresh.py` regenerates the analysis documents that read the fetched tables.
Each builder is a package function that takes the database connection and returns its summary line.
Each runs once per refresh, even when two sources name it, in this order:

| function | writes |
|---|---|
| `arbitrage.write` | the arbitrage category documents, direct and regular |
| `blend.write` | the 50:50 and 65:35 Nifty 500 and gilt hybrid benchmark series |
| `longterm.write_all` | every other category's long-term comparison documents |
| `tracking.write` | how index funds tracked their index |
| `attribution.write_all` | active share and tilt/selection attribution |

| fetched | regenerates |
|---|---|
| `amfi` | `arbitrage.write`, `blend.write`, `longterm.write_all`, `tracking.write`, `attribution.write_all` |
| `nse_index` | `blend.write`, `longterm.write_all`, `tracking.write`, `attribution.write_all` |
| `metals` | `longterm.write_all` |
| `ter` | `arbitrage.write`, `longterm.write_all`, `tracking.write` |
| `kuvera`, `amc` | `attribution.write_all` |

A failing builder does not stop the run. `refresh.py` logs its traceback after the summary lines and
counts it as failed, so the run exits 1.

## Incremental, and what that means per source

Each source fetches only what the database is missing. Three are not incremental:

- **`us_indexes`** refetches its whole history every run. Yahoo rescales earlier adjusted closes on
  every dividend, so appending would splice two scales together.
- **`cpi`** rewrites: the level at any month is the product of every rate before it, so one more
  published year moves every point after it.
- **`ibja`** merges rather than rewrites. Its source is a rolling 30-day PDF and the database is the
  only copy of anything older, so a rewrite would destroy the archive.

## The raw cache

Responses are cached under `~/.cache/investing/` before parsing, so a parser fix can re-run against
the cache instead of refetching. A full AMFI pull takes most of an hour. Deleting the cache is safe.

## Scheduling

On the server, `investing-refresh-daily.timer` runs `--daily` at 11:00 IST Monday to Saturday and
`investing-refresh-weekly.timer` runs `--weekly` at 11:00 IST on Sunday. Both are `Persistent=true`
with a 20 minute random delay, and both run `scripts/refresh.py` directly. It:

- logs to stdout and stderr only; the systemd journal keeps it, so read a run with
  `journalctl --user -u investing-refresh-daily` (or `-weekly`);
- holds an exclusive lock on `data/refresh.lock`, beside the database, so a second run waits up to
  an hour and then exits 1. The units set `TimeoutStartSec=4h` to cover that wait plus the run;
- exits 1 when any source or builder failed, so `systemctl --user status` shows the failed state.
