"""Live securities lending and borrowing (SLB) quotes from NSE, with a fair lending fee.

NSE's own site APIs (see client.py), fetched on request: the SLB order book per series and the
derivatives quote for spot and stock futures.

A series ends on the first Tuesday of its month (Dec-2026 on 1 Dec, Feb-2027 on 2 Feb). Holidays
are not modelled: a series whose Tuesday is a holiday ends a day later than `end_date` says.

Fair fee, per share, for lending until the series ends:

    spot * (1 + rate * days / 365) - futures_at_end

the carry a holder gives up against selling spot, holding cash at `rate` and buying back through
futures. `futures_at_end` is interpolated linearly by days between spot and the listed futures;
past the last listed future there is no fair fee.

A month can have two series, A and B (codes 02 and X2 for Feb), with the same label. The per-symbol
call does not say which is which, so codes are matched through NSE's per-series lists.
"""
from datetime import date, datetime, timedelta
from itertools import pairwise
from urllib.parse import quote as urlquote

from ..core.clock import today_ist
from . import client


def series_end(label: str) -> date:
    """First Tuesday of the series' month. "Dec-2026(D1)**" is a special series NSE opens around
    a corporate action; its month is still the first eight characters."""
    first = datetime.strptime(label[:8], "%b-%Y").date()
    return first + timedelta(days=(1 - first.weekday()) % 7)


def futures_at(days: int, spot: float, futures: list[tuple[int, float]]) -> float | None:
    """Futures price `days` out, linear between spot at day 0 and the listed expiries."""
    points = [(0, spot), *sorted(futures)]
    for (d0, p0), (d1, p1) in pairwise(points):
        if d0 <= days <= d1:
            return p0 + (p1 - p0) * (days - d0) / (d1 - d0) if d1 != d0 else p1
    return None


def fair_fee(spot: float, futures: float, days: int, rate: float) -> float:
    return spot * (1 + rate * days / 365) - futures


def _codes() -> dict[str, list[tuple[str, str]]]:
    """Label -> [(code, group)], e.g. "Feb-2027" -> [("02", "A"), ("X2", "B")]."""
    master = client.get("live-analysis-slb-series-master", ttl=3600).get("data") or {}
    out: dict[str, list[tuple[str, str]]] = {}
    for group, items in master.items():
        for it in items:
            out.setdefault(it["value"], []).append((it["key"], group.removeprefix("Series ")))
    return out


def _row_key(r: dict) -> tuple:
    return tuple(r.get(k) for k in ("buyOrderPrice1", "buyOrderQty1", "sellOrderPrice1", "sellQty1",
                                    "lastTradedPrice", "openPositions"))


def quote(symbol: str, rate: float = 0.07, today: date | None = None) -> dict:
    today = today or today_ist()
    slb = client.get(f"live-analysis-slb?symbol={urlquote(symbol, safe='')}")
    deriv = client.get(f"NextApi/apiClient/GetQuoteApi?functionName=getSymbolDerivativesData&symbol={urlquote(symbol, safe='')}")
    spot = next((client.num(d.get("underlyingValue")) for d in deriv.get("data") or []
                 if client.num(d.get("underlyingValue"))), None)
    futures = sorted((datetime.strptime(c["expiryDate"], "%d-%b-%Y").date(), client.num(c.get("lastPrice")))
                     for c in deriv.get("data") or [] if c.get("instrumentType") == "FUTSTK")
    curve = [((e - today).days, p) for e, p in futures if p and e >= today]

    codes = _codes()
    rows = slb.get("data") or []
    # Which code each row is: look the symbol up in each of its label's per-series lists.
    code_of: dict[int, tuple[str, str]] = {}
    for label in {r["series"] for r in rows}:
        for code, group in codes.get(label, []):
            mine = [r for r in client.get(f"live-analysis-slb?series={code}").get("data") or []
                    if r.get("symbol") == symbol]
            for m in mine:
                for i, r in enumerate(rows):
                    if i not in code_of and r["series"] == label and _row_key(r) == _row_key(m):
                        code_of[i] = (code, group)
                        break

    series: list[dict] = []
    for i, r in enumerate(rows):
        end = series_end(r["series"])
        days = (end - today).days
        # NSE keeps listing a series for a while after it ends.
        if days < 0:
            continue
        fut = futures_at(days, spot, curve) if spot and days > 0 else None
        bid = client.num(r.get("buyOrderPrice1"))
        code, group = code_of.get(i, (None, None))
        series.append({
            "series": r["series"], "code": code, "group": group,
            "end_date": end.isoformat(), "days": days,
            "bid": bid, "bid_qty": r.get("buyOrderQty1") or 0,
            "ask": client.num(r.get("sellOrderPrice1")), "ask_qty": r.get("sellQty1") or 0,
            "last": client.num(r.get("lastTradedPrice")),
            "open_positions": r.get("openPositions") or 0,
            "annualised_yield_pct": client.num(r.get("annualisedYieldPer")),
            # NSE's yield uses whichever of last, ask or bid exists; this is always at the bid.
            "yield_at_bid_pct": round(bid / spot * 365 / days * 100, 2) if bid and spot and days > 0 else None,
            "futures_at_end": round(fut, 2) if fut else None,
            "fair_fee": round(fair_fee(spot, fut, days, rate), 2) if fut and spot else None,
        })
    series.sort(key=lambda s: (s["end_date"], s["group"] or ""))

    bids = [s for s in series if s["bid"]]
    best = max(bids, key=lambda s: s["bid"], default=None)
    # NSE attaches the stock's last corporate action to every row, however old.
    ca = next(((r.get("ca"), r.get("caExpDate")) for r in rows if r.get("ca")), (None, None))
    upcoming = ca[0] if ca[1] and datetime.strptime(ca[1], "%d-%b-%Y").date() >= today else None
    return {"symbol": symbol, "spot": spot, "rate": rate, "as_of": slb.get("timestamp"),
            "corporate_action": upcoming,
            "best": {k: best[k] for k in ("series", "code", "group", "end_date", "days", "bid",
                                          "bid_qty", "yield_at_bid_pct", "fair_fee")} if best else None,
            "futures": [{"expiry": e.isoformat(), "price": p} for e, p in futures],
            "series": series}
