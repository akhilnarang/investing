"""String values that several modules compare or store."""
from enum import StrEnum


class Plan(StrEnum):
    DIRECT = "direct"
    REGULAR = "regular"


class SeriesKind(StrEnum):
    """The kinds series_meta allows."""

    INDEX = "index"
    FX = "fx"
    METAL = "metal"
    CPI = "cpi"


class AnalysisKind(StrEnum):
    LONGTERM = "longterm"
    ACTIVE_SHARE = "active_share"
    ATTRIBUTION = "attribution"
    INDEX_TRACKING = "index_tracking"
    TER = "ter"
    FUND_FACTS = "fund_facts"


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


class AssetClass(StrEnum):
    """The asset classes sources/sif.py stores."""

    EQUITY = "equity"
    DERIVATIVE = "derivative"
    DEBT = "debt"
    CASH = "cash"
    OTHER = "other"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
