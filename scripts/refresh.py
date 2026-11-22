"""Fetch everything that is new, straight into the database.

    scripts/refresh.py                  # every source
    scripts/refresh.py --only fx,amfi   # a subset
    scripts/refresh.py --check          # sanity checks only, fetches nothing
    scripts/refresh.py --daily          # what the daily timer runs

Each source fetches only what the database is missing. A failed source is reported and the rest
still run. Exit 1 means at least one source or document failed.

One refresh runs at a time, behind a lock file next to the database; a second run waits up to an hour.
"""
import argparse
import fcntl
import logging
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

from investing.analysis import arbitrage, attribution, blend, longterm, tracking
from investing.db import DEFAULT_PATH, connect
from investing.sources import (
    amc,
    amfi,
    bhavcopy,
    constituents,
    cpi,
    events,
    fund_facts,
    fx,
    ibja,
    kuvera,
    metals,
    nse_index,
    sif,
    ter,
    universe,
    us_indexes,
)

# Order matters: amfi creates the fund rows universe, ter, kuvera and amc key off, and fx writes the
# usd-inr series us_indexes converts with.
SOURCES = {
    "amfi": amfi.fetch,
    "universe": universe.fetch,
    "bhavcopy": bhavcopy.fetch,
    "fx": fx.fetch,
    "nse_index": nse_index.fetch,
    "us_indexes": us_indexes.fetch,
    "metals": metals.fetch,
    "ibja": ibja.fetch,
    "cpi": cpi.fetch,
    "kuvera": kuvera.fetch,
    "fund_facts": fund_facts.fetch,
    "amc": amc.fetch,
    "sif": sif.fetch,
    "events": events.fetch,
    "ter": ter.fetch,
    "constituents": constituents.fetch,
}

DAILY = ["amfi", "bhavcopy", "fx", "nse_index", "sif"]
WEEKLY = list(SOURCES)

# Documents to rebuild after each source. Each builder runs once, after all fetches, in list order:
# blend before longterm (which benchmarks against the blends), attribution last (it reads the
# category documents).
_AFTER_NAV = [arbitrage.write, blend.write, longterm.write_all, tracking.write, attribution.write_all]
DERIVED: dict[str, list[Callable]] = {
    "amfi": _AFTER_NAV,
    "nse_index": [blend.write, longterm.write_all, tracking.write, attribution.write_all],
    "metals": [longterm.write_all],
    "ter": [arbitrage.write, longterm.write_all, tracking.write],
    "kuvera": [attribution.write_all],
    "amc": [attribution.write_all],
}

LOCK_WAIT = 3600        # seconds a run waits for another refresh before giving up


def builder_name(fn: Callable) -> str:
    return f"{fn.__module__.rpartition('.')[2]}.{fn.__name__}"


def take_lock(lock: IO[str]) -> None:
    """Take an exclusive flock on the open file, waiting up to LOCK_WAIT, or exit 1."""
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                print(f"another refresh still holds {lock.name} after {LOCK_WAIT // 60} minutes; giving up",
                      file=sys.stderr, flush=True)
                raise SystemExit(1) from None
            time.sleep(5)


def settle(db: sqlite3.Connection) -> None:
    """Roll back whatever a failed step left open, so the next one starts on committed data."""
    if db.in_transaction:
        db.execute("ROLLBACK")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--only", default="", help="comma-separated: " + ", ".join(SOURCES))
    ap.add_argument("--daily", action="store_true", help="the sources that change every trading day")
    ap.add_argument("--weekly", action="store_true", help="daily plus the weekly ones")
    ap.add_argument("--check", action="store_true", help="run the sanity checks and exit")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    if a.check:
        print(fx.check(connect(a.db)))
        return

    wanted = ([s.strip() for s in a.only.split(",") if s.strip()]
              or (DAILY if a.daily else WEEKLY if a.weekly else list(SOURCES)))
    unknown = [s for s in wanted if s not in SOURCES]
    if unknown:
        raise SystemExit(f"unknown source(s): {', '.join(unknown)}")

    a.db.parent.mkdir(parents=True, exist_ok=True)
    with open(a.db.parent / "refresh.lock", "w") as lock:
        take_lock(lock)
        failed = refresh(connect(a.db), wanted)
    if failed:
        raise SystemExit(f"{len(failed)} step(s) failed: {', '.join(failed)}")


def refresh(db: sqlite3.Connection, wanted: list[str]) -> list[str]:
    """Fetch each wanted source, then rebuild what derives from them. Returns what failed."""
    failed = []
    for name in wanted:
        started = time.time()
        try:
            print(f"{SOURCES[name](db)}  [{time.time() - started:.0f}s]", flush=True)
        except Exception as exc:
            print(f"{name}: FAILED after {time.time() - started:.0f}s: {exc}", file=sys.stderr, flush=True)
            settle(db)
            failed.append(name)

    # Rebuild the documents derived from each source that succeeded.
    builders: list[Callable] = []
    for name in wanted:
        if name in failed:
            continue
        builders += [b for b in DERIVED.get(name, []) if b not in builders]
    for build in builders:
        started = time.time()
        try:
            print(f"{build(db)}  [{time.time() - started:.0f}s]", flush=True)
        except Exception:
            # A missing input raises LookupError; that fails one document, not the run.
            logging.exception("%s: FAILED after %.0fs", builder_name(build), time.time() - started)
            settle(db)
            failed.append(builder_name(build))

    if "fx" in wanted and "fx" not in failed:
        print(fx.check(db))
    return failed


if __name__ == "__main__":
    main()
