"""Shared NAV helpers: load a series, repair it, compute CAGR.

AMFI NAV histories have these discontinuities:

  * A single bogus point the next observation reverts (Axis funds' 0.0 on
    2013-04-07, Quant funds on 2015-05-27). Dropped.
  * A power-of-ten redenomination (HDFC Liquid 100x on 2015-08-30, UTI Liquid
    1/10 on 2026-06-20). Earlier history is rescaled by the rounded factor, so
    the break day keeps its own market move.
  * A persistent shift that is not a power of ten, or a large move at the end
    of the series. Kept and noted, since it may be a real move.
  * A move that neither reverts nor persists. The series is rejected.

Repairs and rejections go to the optional `notes` list; for a rejected series
they are the only record of why it is missing.
"""
from datetime import date, timedelta
from itertools import pairwise

from ..db import undate

MIN_POINTS = 150
DROP, SPIKE = -0.25, 0.35
# Observations that must confirm a level shift before it counts as a redenomination.
PERSIST = 3
# A persistent shift within SPLIT_TOLERANCE of one of these is a redenomination.
SPLIT_FACTORS = (0.001, 0.01, 0.1, 10.0, 100.0, 1000.0)
# Bonus issues and other non-power-of-ten redenominations: (scheme code, day) -> factor for earlier NAVs.
# Curated by hand where a one-day move of that size is impossible for the asset.
KNOWN_REDENOMINATIONS = {
    ("100664", date(2009, 6, 5)): 0.5,    # UTI Large & Mid Cap regular: 1:1 bonus units
    ("103225", date(2007, 8, 7)): 0.75,   # quant Liquid regular: 1:3 bonus units
}
SPLIT_TOLERANCE = 0.05


def split_factor(ratio: float) -> float | None:
    """The power-of-ten factor `ratio` is within tolerance of, or None."""
    for f in SPLIT_FACTORS:
        if abs(ratio / f - 1) <= SPLIT_TOLERANCE:
            return f
    return None

def _ok(previous: float, current: float) -> bool:
    return previous > 0 and DROP < current / previous - 1 < SPIKE

def _drop_outliers(days: list[date], values: list[float],
                   notes: list[str]) -> tuple[list[date], list[float]]:
    """Remove non-positive NAVs and lone points the next observation contradicts."""
    kept_days: list[date] = []
    kept_values: list[float] = []
    for i, current in enumerate(values):
        day = days[i]
        if current <= 0:
            notes.append(f"dropped non-positive NAV {current:g} on {day}")
            continue
        if not kept_values:
            kept_days.append(day)
            kept_values.append(current)
            continue
        previous = kept_values[-1]
        if _ok(previous, current):
            kept_days.append(day)
            kept_values.append(current)
            continue
        following = next((v for v in values[i + 1:] if v > 0), None)
        if following is None:
            notes.append(f"kept unconfirmed trailing NAV {current:g} on {day}")
            kept_days.append(day)
            kept_values.append(current)
            continue
        if _ok(previous, following):
            notes.append(f"dropped outlier NAV {current:g} on {day}")
            continue
        kept_days.append(day)
        kept_values.append(current)
    return kept_days, kept_values

def _rescale_steps(days: list[date], values: list[float],
                   notes: list[str], code: str | None = None) -> list[float] | None:
    """Rescale history before a power-of-ten redenomination; leave other persistent
    shifts alone; None only for a break that neither persists nor reverts."""
    out = list(values)
    for i in range(1, len(out)):
        if _ok(out[i - 1], out[i]):
            continue
        window = out[i:i + 1 + PERSIST]
        if len(window) <= PERSIST:
            notes.append(f"kept unconfirmed NAV move on {days[i]} ({out[i - 1]:g} -> {out[i]:g}), "
                         "too near the end of the series to classify")
            continue
        confirmed = out[i - 1] > 0 and all(_ok(a, b) for a, b in pairwise(window))
        if not confirmed:
            notes.append(f"rejected series: unexplained NAV break on {days[i]} "
                         f"({out[i - 1]:g} -> {out[i]:g})")
            return None
        ratio = out[i] / out[i - 1]
        factor = KNOWN_REDENOMINATIONS.get((str(code), days[i])) or split_factor(ratio)
        if factor is None:
            notes.append(f"kept persistent NAV move on {days[i]} ({out[i - 1]:g} -> {out[i]:g}, "
                         f"x{ratio:.3g}): not a power-of-ten redenomination")
            continue
        for j in range(i):
            out[j] *= factor
        notes.append(f"rescaled history before {days[i]} by {factor:g} (redenomination; observed x{ratio:.4g})")
    return out

def screen_series(s: dict[date, float], record: list[str], code: str | int = "") -> dict[date, float] | None:
    """The repairs and rejections above, applied to an already-read series."""
    if len(s) < MIN_POINTS:
        return None
    days = sorted(s)
    days, values = _drop_outliers(days, [s[d] for d in days], record)
    if len(days) < MIN_POINTS:
        return None
    values = _rescale_steps(days, values, record, str(code))
    if values is None:
        return None
    return dict(zip(days, values, strict=True))


def load_db(code: str | int, db, screen: bool = True,
            notes: list[str] | None = None) -> dict[date, float] | None:
    """A fund's series from the nav table, screened unless `screen` is False; None when it is too short."""
    rows = db.execute("SELECT d, nav FROM nav WHERE scheme_code=? ORDER BY d", (int(code),)).fetchall()
    if not rows:
        return None
    s = {undate(d): v for d, v in rows}
    if not screen:
        return s if len(s) >= MIN_POINTS else None
    return screen_series(s, [] if notes is None else notes, code)


def nav_on(s: dict[date, float], d: date, back: int = 12) -> float | None:
    for k in range(back):
        v = s.get(d - timedelta(days=k))
        if v is not None:
            return v
    return None

def cagr(s: dict[date, float] | None, years: float, as_of: date) -> float | None:
    if not s:
        return None
    now = nav_on(s, as_of)
    then = nav_on(s, as_of - timedelta(days=int(365.25 * years)))
    if now is None or then is None or then <= 0:
        return None
    return ((now / then) ** (1 / years) - 1) * 100

def inception(s: dict[date, float]) -> date:
    return min(s)
