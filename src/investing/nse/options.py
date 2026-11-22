"""Live option chains from NSE (see client.py), one expiry at a time, with the usual summary numbers.

`option-chain-contract-info` lists a symbol's expiries for indices and stocks alike; the chain itself
(`option-chain-v3`) wants to be told which of the two it is.

Summary, over every strike of the expiry, not just the ones returned:

* pcr: put open interest over call open interest.
* max_pain: the strike at which option holders would collect least at expiry, i.e. where
  sum(call OI * max(0, K - strike)) + sum(put OI * max(0, strike - K)) is smallest.
* atm_strike: the strike nearest spot; atm_iv is the mean of its call and put IVs where both trade.
"""
from datetime import date, datetime
from urllib.parse import quote

from ..core.clock import today_ist
from . import client

INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def _side(c: dict | None) -> dict | None:
    if not c:
        return None
    return {"last": client.num(c.get("lastPrice")), "change": c.get("change") or 0,
            "bid": client.num(c.get("buyPrice1")), "bid_qty": c.get("buyQuantity1") or 0,
            "ask": client.num(c.get("sellPrice1")), "ask_qty": c.get("sellQuantity1") or 0,
            "iv": client.num(c.get("impliedVolatility")),
            "oi": c.get("openInterest") or 0, "oi_change": c.get("changeinOpenInterest") or 0,
            "volume": c.get("totalTradedVolume") or 0}


def expiries(symbol: str) -> list[date]:
    info = client.get(f"option-chain-contract-info?symbol={quote(symbol, safe='')}", ttl=3600)
    return sorted(datetime.strptime(e, "%d-%b-%Y").date() for e in info.get("expiryDates") or [])


def max_pain(rows: list[dict]) -> float | None:
    strikes = [r["strike"] for r in rows]

    def paid(k: float) -> float:
        return sum((r["call"] or {}).get("oi", 0) * max(0.0, k - r["strike"])
                   + (r["put"] or {}).get("oi", 0) * max(0.0, r["strike"] - k) for r in rows)
    return min(strikes, key=paid) if strikes else None


def chain(symbol: str, expiry: date | None = None, strikes: int = 10) -> dict:
    """`strikes` each side of the money; 0 returns the whole chain. Raises LookupError for a symbol
    with no options or an expiry it does not have."""
    listed = expiries(symbol)
    if not listed:
        raise LookupError(f"{symbol} has no listed options")
    expiry = expiry or listed[0]
    if expiry not in listed:
        raise LookupError(f"{symbol} has no {expiry} expiry; listed: {', '.join(map(str, listed))}")
    kind = "Indices" if symbol in INDICES else "Equity"
    payload = client.get(f"option-chain-v3?type={kind}&symbol={quote(symbol, safe='')}&expiry={expiry:%d-%b-%Y}")
    records = payload.get("records") or {}
    spot = client.num(records.get("underlyingValue"))
    rows: list[dict] = sorted(({"strike": float(d["strikePrice"]), "call": _side(d.get("CE")), "put": _side(d.get("PE"))}
                   for d in records.get("data") or []), key=lambda r: r["strike"])

    call_oi = sum((r["call"] or {}).get("oi", 0) for r in rows)
    put_oi = sum((r["put"] or {}).get("oi", 0) for r in rows)
    atm = min(rows, key=lambda r: abs(r["strike"] - spot)) if rows and spot else None
    ivs = [s["iv"] for s in (atm["call"], atm["put"]) if s and s["iv"]] if atm else []
    shown = rows
    if strikes and atm:
        i = rows.index(atm)
        shown = rows[max(0, i - strikes):i + strikes + 1]
    return {
        "symbol": symbol, "type": kind.lower(), "spot": spot, "as_of": records.get("timestamp"),
        "expiry": expiry.isoformat(), "days": (expiry - today_ist()).days,
        "expiries": [e.isoformat() for e in listed],
        "summary": {"call_oi": call_oi, "put_oi": put_oi,
                    "pcr": round(put_oi / call_oi, 3) if call_oi else None,
                    "max_pain": max_pain(rows), "atm_strike": atm["strike"] if atm else None,
                    "atm_iv": round(sum(ivs) / len(ivs), 2) if ivs else None,
                    "strikes": len(rows)},
        "strikes": shown,
    }
