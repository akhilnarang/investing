"""Fund metrics shared by every category document: CAGR windows, rolling distributions, SIP XIRR.

`longterm` and `arbitrage` both use these, so a change here moves every category page.
"""
from datetime import date, timedelta
from statistics import median

from ..core.clock import month_end
from . import nav


def rolling_dated(series: dict[date, float], years: int, as_of: date) -> list[tuple[date, float]]:
    """(window end, CAGR) for every rolling window on a calendar month-end schedule, oldest first."""
    out = []
    first = min(series)
    end = min(month_end(as_of.year, as_of.month), as_of)
    while end - timedelta(days=int(365.25 * years)) >= first:
        value = nav.cagr(series, years, end)
        if value is not None:
            out.append((end, value))
        end = end.replace(day=1) - timedelta(days=1)
    return list(reversed(out))


def rolling_summary(series: dict[date, float], years: int, as_of: date, floor: float) -> dict:
    values = [v for _, v in rolling_dated(series, years, as_of)]
    if not values:
        return {"windows": 0, "median": None, "worst": None, "best": None, "above_floor": None}
    return {"windows": len(values), "median": round(median(values), 2),
            "worst": round(min(values), 2), "best": round(max(values), 2),
            "above_floor": round(sum(v >= floor for v in values) / len(values), 3)}


def month_end_navs(series: dict[date, float], as_of: date) -> list[list]:
    out = []
    y, m = min(series).year, min(series).month
    while (y, m) <= (as_of.year, as_of.month):
        day = min(month_end(y, m), as_of)
        value = nav.nav_on(series, day)
        if value is not None:
            out.append([f"{y:04d}-{m:02d}", round(value, 4)])
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def detail_series(series: dict[date, float], as_of: date, rolling: tuple[int, ...] = (3, 5)) -> dict:
    return {"monthly": month_end_navs(series, as_of),
            "rolling_series": {str(y): [[str(d), round(v, 2)] for d, v in rolling_dated(series, y, as_of)]
                               for y in rolling}}


def max_drawdown(series: dict[date, float] | None, as_of: date, years: int) -> float | None:
    if not series:
        return None
    start = as_of - timedelta(days=round(365.25 * years))
    values = [series[d] for d in sorted(series) if start <= d <= as_of]
    if not values:
        return None
    peak, worst = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst * 100


def history_years(series: dict[date, float] | None, as_of: date) -> float:
    return 0.0 if not series else max(0.0, (as_of - min(series)).days / 365.25)


def since_inception(series: dict[date, float] | None, as_of: date) -> float | None:
    years = history_years(series, as_of)
    return nav.cagr(series, years, as_of) if years >= 1 else None


def calendar_years(series: dict[date, float], as_of: date) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for year in range(min(series).year + 1, as_of.year):
        start = nav.nav_on(series, date(year - 1, 12, 31))
        end = nav.nav_on(series, date(year, 12, 31))
        out[str(year)] = round((end / start - 1) * 100, 1) if start and end else None
    ytd_start = nav.nav_on(series, date(as_of.year - 1, 12, 31))
    ytd_end = nav.nav_on(series, as_of)
    out[f"{as_of.year} YTD"] = round((ytd_end / ytd_start - 1) * 100, 1) if ytd_start and ytd_end else None
    return out


def xirr(cashflows: list[tuple[date, float]]) -> float | None:
    if len(cashflows) < 2:
        return None
    t0 = cashflows[0][0]

    def npv(rate: float) -> float:
        # Actual/365, as Zerodha Console, Coin and Excel compute XIRR.
        return sum(cf / (1 + rate) ** ((d - t0).days / 365) for d, cf in cashflows)

    low, high = -0.99, 10.0
    f_low = npv(low)
    if f_low * npv(high) > 0:
        return None
    mid = (low + high) / 2
    for _ in range(200):
        mid = (low + high) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9:
            break
        if f_low * f_mid < 0:
            high = mid
        else:
            low, f_low = mid, f_mid
    return mid * 100


def sip_schedule(as_of: date, years: float) -> list[date]:
    months = round(years * 12)
    y, m = as_of.year, as_of.month
    y, m = y - (months - 1) // 12, m - (months - 1) % 12
    if m < 1:
        y, m = y - 1, m + 12
    out = []
    for _ in range(months):
        out.append(date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def sip_xirr(series: dict[date, float], years: float, as_of: date, amount: float = 10_000.0) -> float | None:
    """A monthly SIP bought on the first with a NAV, redeemed on the NAV date as_of resolves to."""
    flows: list[tuple[date, float]] = []
    units = 0.0
    for day in sip_schedule(as_of, years):
        bought = next(((day + timedelta(days=k), series[day + timedelta(days=k)])
                       for k in range(7) if day + timedelta(days=k) in series), None)
        if bought is None:
            return None
        units += amount / bought[1]
        flows.append((bought[0], -amount))
    final = next(((as_of - timedelta(days=k), series[as_of - timedelta(days=k)])
                  for k in range(12) if as_of - timedelta(days=k) in series), None)
    if final is None:
        return None
    flows.append((final[0], units * final[1]))
    return xirr(flows)


def rolling_excess(active: dict[date, float], benchmark: dict[date, float],
                   window_years: int, as_of: date, sample_years: int) -> dict:
    values = []
    for month in range(sample_years * 12 + 1):
        end = as_of - timedelta(days=round(month * 30.4375))
        av = nav.cagr(active, window_years, end)
        bv = nav.cagr(benchmark, window_years, end)
        if av is not None and bv is not None:
            values.append(av - bv)
    if not values:
        return {"observations": 0, "median_excess_pp": None, "hit_rate": None}
    return {"observations": len(values), "median_excess_pp": round(median(values), 3),
            "hit_rate": round(sum(x > 0 for x in values) / len(values), 4)}


def rnd(value: float | None, places: int = 2) -> float | None:
    return None if value is None else round(value, places)
