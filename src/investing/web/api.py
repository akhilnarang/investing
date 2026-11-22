"""JSON routes over queries.py.

The connection is read-only. `limit` is validated against the same caps queries.py clamps to.
"""
import sqlite3
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from ..core.enums import Plan, SeriesKind
from ..db import get_db
from ..nse import options, slb
from . import errors, ratelimit
from . import queries as q

Limit = Annotated[int, Query(ge=1, le=q.MAX_LIMIT, description="Maximum points to return.")]
RowLimit = Annotated[int, Query(ge=1, le=q.MAX_ROW_LIMIT, description="Maximum rows to return.")]
Offset = Annotated[int, Query(ge=0, description="Rows to skip, for paging.")]
Start = Annotated[date | None, Query(description="Earliest date, inclusive (YYYY-MM-DD).")]
End = Annotated[date | None, Query(description="Latest date, inclusive (YYYY-MM-DD).")]


DB = Annotated[sqlite3.Connection, Depends(get_db)]

# ---------------------------------------------------------------- routers
# One tagged router per resource, so /docs groups the endpoints.

funds_api = APIRouter(tags=["funds"])
stocks_api = APIRouter(tags=["stocks"])
holdings_api = APIRouter(tags=["holdings"])
series_api = APIRouter(tags=["series"])
slb_api = APIRouter(tags=["slb"])
options_api = APIRouter(tags=["options"])
meta_api = APIRouter(tags=["meta"])


@meta_api.get("/health")
def health(db: DB) -> dict:
    """Liveness, plus the fund count to show the database answers."""
    return {"status": "ok", "funds": db.execute("SELECT COUNT(*) FROM fund").fetchone()[0]}


# ---------------------------------------------------------------- funds

@funds_api.get("/api/v1/funds/")
def list_funds(db: DB, q_: Annotated[str | None, Query(alias="q", min_length=2, max_length=100)] = None,
               category: str | None = None,
               plan: Annotated[Plan | None, Query(description="Only this plan.")] = None,
               limit: RowLimit = q.DEFAULT_ROW_LIMIT, offset: Offset = 0) -> list[dict]:
    """Search funds by name (`q`) or list a category; one of the two is required."""
    if category:
        return q.funds_in_category(db, category, plan=plan, limit=limit, offset=offset)
    if q_:
        return q.search_funds(db, q_, plan=plan, limit=limit, offset=offset)
    raise errors.unprocessable("Pass q= to search by name or category= to list a category.")


@meta_api.get("/api/v1/categories")
def list_categories(db: DB) -> list[dict]:
    """Every fund category."""
    return q.categories(db)


@funds_api.get("/api/v1/funds/by-isin/{isin}")
def funds_by_isin(db: DB, isin: str) -> list[dict]:
    """The funds that carry this ISIN."""
    return q.funds_by_isin(db, isin)


@funds_api.get("/api/v1/funds/{scheme_code}")
def get_fund(db: DB, scheme_code: int) -> dict:
    """One fund."""
    fund = q.fund(db, scheme_code)
    if fund is None:
        raise errors.not_found("fund")
    return fund


@funds_api.get("/api/v1/funds/{scheme_code}/nav")
def get_nav(db: DB, scheme_code: int, start: Start = None, end: End = None,
            limit: Limit = q.DEFAULT_LIMIT, offset: Offset = 0,
            screen: Annotated[bool, Query(description="Apply nav.py's repairs. False is what AMFI "
                                                      "published, warts and all.")] = True) -> dict:
    """The NAV history of one fund."""
    if q.fund(db, scheme_code) is None:
        raise errors.not_found("fund")
    points = q.nav_series(db, scheme_code, start=start, end=end, limit=limit, offset=offset, screen=screen)
    return {"scheme_code": scheme_code, "screened": screen, "count": len(points), "points": points}


@funds_api.get("/api/v1/funds/{scheme_code}/returns")
def get_returns(db: DB, scheme_code: int, as_of: Annotated[date | None, Query()] = None) -> dict:
    """Trailing returns of one fund."""
    if q.fund(db, scheme_code) is None:
        raise errors.not_found("fund")
    # None means unusable history, not a missing fund, so 409 rather than 404.
    r = q.fund_returns(db, scheme_code, as_of=as_of)
    if r is None:
        raise errors.conflict("The fund has no usable NAV history (too short, or rejected "
                              "by screening). GET ?screen=false on /nav to see the raw rows.")
    return r


@funds_api.get("/api/v1/funds/{scheme_code}/portfolio")
def get_portfolio(db: DB, scheme_code: int, on: Annotated[date | None, Query(description="Disclosure date; "
                  "the newest one when omitted.")] = None,
                  limit: RowLimit = q.DEFAULT_ROW_LIMIT, offset: Offset = 0) -> dict:
    """The disclosed holdings of one fund."""
    if q.fund(db, scheme_code) is None:
        raise errors.not_found("fund")
    return q.portfolio(db, scheme_code, on=on, limit=limit, offset=offset)


@funds_api.get("/api/v1/funds/{scheme_code}/portfolio/dates")
def get_portfolio_dates(db: DB, scheme_code: int, limit: RowLimit = q.DEFAULT_ROW_LIMIT) -> list[str]:
    """The disclosure dates of one fund."""
    return q.portfolio_dates(db, scheme_code, limit=limit)


@funds_api.get("/api/v1/funds/{scheme_code}/holdings/{isin}")
def get_position_history(db: DB, scheme_code: int, isin: str,
                         limit: RowLimit = q.DEFAULT_ROW_LIMIT) -> list[dict]:
    """One fund's weight in one security over time."""
    return q.position_history(db, scheme_code, isin, limit=limit)


@funds_api.get("/api/v1/funds/{scheme_code}/overlap/{other}")
def get_overlap(db: DB, scheme_code: int, other: int, on: Annotated[date | None, Query()] = None,
                limit: RowLimit = q.DEFAULT_ROW_LIMIT) -> dict:
    """The holdings two funds share."""
    return q.overlap(db, scheme_code, other, on=on, limit=limit)


# ---------------------------------------------------------------- holdings

# `on` pins one disclosure date; otherwise `start` and `end` bound the range and the result is paged
# newest first.
@holdings_api.get("/api/v1/holdings/by-isin/{isin}")
def holdings_by_isin(db: DB, isin: str, on: Annotated[date | None, Query(description="One disclosure date.")] = None,
                     start: Start = None, end: End = None,
                     limit: RowLimit = q.DEFAULT_ROW_LIMIT, offset: Offset = 0) -> dict:
    """Which funds held this ISIN, and when, with weights, newest first."""
    return {"isin": isin, "company": q.stock(db, isin),
            "holdings": q.holders_of(db, isin, on=on, start=start, end=end, limit=limit, offset=offset)}


@holdings_api.get("/api/v1/holdings/by-isin/{isin}/dates")
def holding_dates(db: DB, isin: str, limit: RowLimit = q.DEFAULT_ROW_LIMIT) -> list[str]:
    """The disclosure dates on which this ISIN was held."""
    return q.holding_dates_for_isin(db, isin, limit=limit)


@holdings_api.get("/api/v1/holdings/by-isin/{isin}/ownership")
def isin_ownership(db: DB, isin: str, limit: RowLimit = q.DEFAULT_ROW_LIMIT) -> list[dict]:
    """Holder count and total disclosed weight per date."""
    return q.isin_ownership(db, isin, limit=limit)


# ---------------------------------------------------------------- stocks

@stocks_api.get("/api/v1/stocks/")
def list_stocks(db: DB, q_: Annotated[str, Query(alias="q", min_length=1, max_length=100)],
                limit: RowLimit = q.DEFAULT_ROW_LIMIT, offset: Offset = 0) -> list[dict]:
    """Search stocks by name or symbol."""
    return q.search_stocks(db, q_, limit=limit, offset=offset)


@stocks_api.get("/api/v1/stocks/by-symbol/{symbol}")
def stocks_by_symbol(db: DB, symbol: str) -> list[dict]:
    """The stocks that trade under this symbol."""
    return q.stock_by_symbol(db, symbol)


@stocks_api.get("/api/v1/stocks/{isin}")
def get_stock(db: DB, isin: str) -> dict:
    """One stock."""
    stock = q.stock(db, isin)
    if stock is None:
        raise errors.not_found("stock")
    return stock


@stocks_api.get("/api/v1/stocks/{isin}/prices")
def get_prices(db: DB, isin: str, start: Start = None, end: End = None,
               limit: Limit = q.DEFAULT_LIMIT, offset: Offset = 0) -> dict:
    """The closing prices of one stock."""
    if q.stock(db, isin) is None:
        raise errors.not_found("stock")
    points = q.stock_prices(db, isin, start=start, end=end, limit=limit, offset=offset)
    return {"isin": isin, "count": len(points), "points": points}


@stocks_api.get("/api/v1/stocks/{isin}/actions")
def get_actions(db: DB, isin: str, start: Start = None, end: End = None,
                limit: RowLimit = q.DEFAULT_ROW_LIMIT, offset: Offset = 0) -> list[dict]:
    """The splits, bonuses and dividends of one stock."""
    return q.corporate_actions(db, isin, start=start, end=end, limit=limit, offset=offset)


# ---------------------------------------------------------------- named series

@series_api.get("/api/v1/series/")
def list_series(db: DB, kind: Annotated[SeriesKind | None, Query(description="Only this kind.")] = None) -> list[dict]:
    """The named series, optionally of one kind."""
    return q.series_list(db, kind)


@series_api.get("/api/v1/series/{name}")
def get_series(db: DB, name: str, start: Start = None, end: End = None,
               limit: Limit = q.DEFAULT_LIMIT, offset: Offset = 0) -> dict:
    """One named series."""
    meta = q.series_meta(db, name)
    if meta is None:
        raise errors.not_found("series")
    points = q.series_points(db, name, start=start, end=end, limit=limit, offset=offset)
    return meta | {"count": len(points), "points": points}


# ---------------------------------------------------------------- live NSE

# Both live routes call NSE on request (cached 30 s). The rate limit protects NSE from one client.

@slb_api.get("/api/v1/slb/{symbol}", dependencies=[Depends(ratelimit.limit(ratelimit.NSE))])
def get_slb(symbol: Annotated[str, Path(pattern=r"^[A-Za-z0-9&-]{1,20}$")],
            rate: Annotated[float, Query(ge=0, le=0.5)] = 0.07) -> dict:
    """Live SLB bids and asks per series, with the fair fee where a future matches."""
    try:
        return slb.quote(symbol.upper(), rate)
    except (OSError, ValueError, KeyError) as e:
        raise errors.bad_gateway(f"NSE did not answer usefully: {type(e).__name__}") from e


# `expiry` defaults to the nearest and `expiries` in the reply lists the rest. `strikes` is how many
# strikes either side of the at-the-money strike to return (0 for all). The summary (PCR, max pain,
# ATM IV) covers the whole expiry either way.
@options_api.get("/api/v1/options/{symbol}", dependencies=[Depends(ratelimit.limit(ratelimit.NSE))])
def get_option_chain(symbol: Annotated[str, Path(pattern=r"^[A-Za-z0-9&-]{1,20}$")],
                     expiry: date | None = None,
                     strikes: Annotated[int, Query(ge=0, le=200)] = 10) -> dict:
    """Live NSE option chain for an index or a stock, one expiry."""
    try:
        return options.chain(symbol.upper(), expiry, strikes)
    except LookupError as e:
        raise errors.not_found("option chain") from e
    except (OSError, ValueError, KeyError) as e:
        raise errors.bad_gateway(f"NSE did not answer usefully: {type(e).__name__}") from e
