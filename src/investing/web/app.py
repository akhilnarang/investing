"""The FastAPI app: JSON routes, HTML pages and the prospectus routers over one read-only database.

    uvicorn investing.web.app:app
"""
import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..ipo.api import build_routers
from . import api, pages
from .views import portfolio as _portfolio
from .views import summary as _summary

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Send the package's logs to stderr; uvicorn configures only its own loggers."""
    package = logging.getLogger("investing")
    if not package.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        package.addHandler(handler)
        package.setLevel(logging.INFO)
    # httpx logs each request URL at INFO and a registrar lookup URL can carry a PAN.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


_configure_logging()

try:
    rhp = build_routers()
except Exception:  # a bad analyser setting must not take the market pages down
    rhp = None
    logger.exception("prospectus analyser failed to start; /ipo/rhp and /api/v1/ipo are absent")

# Starlette runs only the outermost app's lifespan, so the prospectus routers are included, not mounted.
app = FastAPI(
    title="investing",
    description="Indian mutual funds, stocks, benchmarks and disclosed portfolios. The data is read-only.",
    version="0.1.0",
    lifespan=rhp.lifespan if rhp is not None else None,
)

# ---------------------------------------------------------------- the site

app.mount("/static", StaticFiles(directory=str(pages.STATIC)), name="static")
for _router in (api.meta_api, api.funds_api, api.stocks_api, api.holdings_api, api.series_api, api.slb_api,
                api.options_api):
    app.include_router(_router)
app.include_router(pages.router)
app.include_router(_summary.router)
app.include_router(_portfolio.router)

if rhp is not None:
    app.include_router(rhp.api)
    app.include_router(rhp.pages)
    # The prospectus routes read their services through request.app.state.
    for _key, _value in rhp.startup_state.items():
        setattr(app.state, _key, _value)
