"""Synthetic hybrid benchmarks: an equity TRI and a debt series, rebalanced monthly.

CRISIL's hybrid indices are proprietary. The debt leg is a gilt fund's regular-plan NAV, since no NSE
fixed-income series here reaches back as far; its fees make the blend slightly easier to beat.
"""
import json
import sqlite3
from datetime import date, timedelta

from ..db import undate
from ..sources import store_series

EQUITY = "nifty-500"
GILT = 100369            # ICICI Prudential Gilt Fund, regular growth
# series name -> (label, equity weight). `longterm.CATEGORIES` refers to these names.
BLENDS = {
    "50-50-nifty-500-gilt": ("50:50 NIFTY 500 GILT", 0.50),
    "65-35-nifty-500-gilt": ("65:35 NIFTY 500 GILT", 0.65),
}
START = 1000.0


def blend(equity: dict[date, float], debt: dict[date, float], w_equity: float) -> dict[date, float]:
    """The blend on every day both legs have a value.

    Each leg compounds on its own within a month; weights reset on the month's last common close.
    """
    days = sorted(set(equity) & set(debt))
    if not days:
        raise ValueError("the two legs share no trading days")
    out: dict[date, float] = {}
    value = base_value = START
    eq_base, debt_base, month, prev = equity[days[0]], debt[days[0]], days[0].month, days[0]
    for d in days:
        if d.month != month:
            base_value, eq_base, debt_base, month = value, equity[prev], debt[prev], d.month
        value = base_value * (w_equity * equity[d] / eq_base + (1 - w_equity) * debt[d] / debt_base)
        out[d] = round(value, 4)
        prev = d
    return out


def legs(db: sqlite3.Connection) -> tuple[dict[date, float], dict[date, float]]:
    equity = {undate(d): v for d, v in db.execute("SELECT d, v FROM series WHERE name=? AND v>0", (EQUITY,))}
    # Raw NAVs: a gilt fund never trips nav.py's outlier screen.
    debt = {undate(d): v for d, v in db.execute("SELECT d, nav FROM nav WHERE scheme_code=? AND nav>0", (GILT,))}
    return equity, debt


def build(db: sqlite3.Connection) -> dict[str, tuple[dict[date, float], dict]]:
    """Every blend, with the metadata `store_series` is given for it."""
    equity, debt = legs(db)
    eq_meta = json.loads((db.execute("SELECT meta FROM series_meta WHERE name=?", (EQUITY,)).fetchone()
                          or ["{}"])[0])
    debt_row = db.execute("SELECT name FROM fund WHERE scheme_code=?", (GILT,)).fetchone()
    eq_name = eq_meta.get("scheme_name") or EQUITY
    debt_name = debt_row[0] if debt_row else str(GILT)
    # From the rows: after an incremental fetch, series_meta's span covers only the new points.
    eq_span = f"{min(equity)} to {max(equity)}" if equity else "no rows"
    out = {}
    for name, (label, w) in BLENDS.items():
        series = blend(equity, debt, w)
        pct_e, pct_d = f"{w * 100:g}", f"{100 - w * 100:g}"
        meta = {
            "scheme_name": f"{label} blend", "index": label, "kind": "blend", "source": "investing.blend",
            "legs": [{"series": EQUITY, "weight": w, "name": eq_name},
                     {"scheme_code": GILT, "weight": round(1 - w, 10), "name": debt_name}],
            "rebalance": "monthly",
            "method": f"{pct_e}% {eq_name} and {pct_d}% {debt_name}, each leg compounding on its own "
                      "within a calendar month and reset to the target weights on the month's last "
                      f"common close; {START:g} on the first day both legs have a value",
            "sources": [
                {"name": f"{eq_meta.get('index', eq_name)} Total Return Index (NSE Indices)",
                 "url": "https://www.niftyindices.com/reports/historical-data",
                 "what": f"equity leg, {pct_e}% of the synthetic benchmark; daily total-return values "
                         f"{eq_span}",
                 "retrieved": (eq_meta.get("retrieved_at") or "")[:10] or None},
                {"name": f"{debt_name} (AMFI NAV history)",
                 "url": "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx",
                 "what": f"debt leg, {pct_d}% of the synthetic benchmark; a gilt fund's regular-plan NAV "
                         "stands in for a bond index because NSE's fixed-income series served here start "
                         "too late; its fees make the blend slightly easier to beat",
                 "retrieved": str(max(debt)) if debt else None},
            ],
        }
        out[name] = (series, meta)
    return out


def write(db: sqlite3.Connection) -> str:
    """Rebuild and store both blends. Runs before `longterm.write_all`."""
    done = []
    for name, (series, meta) in build(db).items():
        # Full rewrite: every point depends on the rebalances before it.
        rows = store_series(db, name, "index", series, meta, replace_after=min(series) - timedelta(days=1))
        last = db.execute("SELECT MAX(d) FROM series WHERE name=?", (name,)).fetchone()[0]
        done.append(f"{name}: {rows} days, through {undate(last)}")
    return "; ".join(done)
