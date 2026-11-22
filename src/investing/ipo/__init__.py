"""IPO prospectus analysis: extract facts from an RHP or DRHP with page-level evidence.

The prospectus tables live in the main database next to `company`, so an analyzed prospectus can
carry the ISIN of the company it is about once that company lists. See link.py.
"""

from .cache import CacheStore
from .link import link_analyses, prospectus_for, resolve
from .service import AnalysisService

__all__ = ["AnalysisService", "CacheStore", "link_analyses", "prospectus_for", "resolve"]
