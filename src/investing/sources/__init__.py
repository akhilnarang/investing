"""Fetchers: pull from upstream, cache the raw response, parse into the database.

Each module exposes `fetch(db, **kw) -> str`, returning a one-line summary; `scripts/refresh.py`
runs them. Raw responses are cached under `~/.cache/investing/` so a parse fix can re-run without
re-fetching. Most fetchers are incremental against what the database holds.
"""
import json
from datetime import UTC, date, datetime
from pathlib import Path

from .. import config
from ..db import day, undate, writing

CACHE = config.CACHE_DIR  # INVESTING_CACHE

UA = config.USER_AGENT


class Throttled(RuntimeError):
    """Yahoo refused this request for a transient reason; the item is left for the next run.

    Raised only for a known list of transient codes. Other Yahoo errors are a plain RuntimeError.
    """


def cache_dir(name: str) -> Path:
    d = CACHE / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_series(db, name: str, kind: str, series: dict, meta: dict,
                 replace_after: date | None = None) -> int:
    """One named series and its metadata: benchmarks, currencies, metals and CPI all land here.

    `kind` is a `SeriesKind` chosen by the fetcher, not the "kind" in meta, which uses other values.
    """
    days = sorted(series)
    if not days:
        return 0
    with writing(db):
        if replace_after is not None:
            # A series whose newest point is re-dated every run (CPI's month in progress) would
            # otherwise keep one stale point per run.
            db.execute("DELETE FROM series WHERE name=? AND d>?", (name, day(replace_after)))
        db.execute("INSERT OR REPLACE INTO series_meta (name, kind, meta) VALUES (?,?,?)",
                   (name, kind, json.dumps({**meta,
                                            "retrieved_at": datetime.now(UTC).isoformat(),
                                            "first": days[0].isoformat(), "last": days[-1].isoformat(),
                                            "rows": len(days)}, ensure_ascii=False)))
        db.executemany("INSERT OR REPLACE INTO series (name, d, v) VALUES (?,?,?)",
                       [(name, day(d), float(series[d])) for d in days])
    return len(days)


def last_day(db, name: str):
    """The newest day a series already holds, for an incremental fetch."""
    row = db.execute("SELECT MAX(d) FROM series WHERE name=?", (name,)).fetchone()
    return undate(row[0]) if row and row[0] is not None else None
