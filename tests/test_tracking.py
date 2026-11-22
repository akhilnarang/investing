"""Index-fund tracking and the hybrid blend benchmarks.

The blend is checked by hand arithmetic; a rebalance on the wrong close still looks plausible.
"""
from datetime import date

import pytest

from investing.analysis.blend import blend
from investing.analysis.tracking import index_of


def test_blend_resets_to_target_weights_at_each_month_end():
    # Equity doubles in January, debt is flat. 50:50 from 1000 ends January at 1500; February starts
    # from 750/750, so equity doubling again gives 750*2 + 750 = 2250.
    equity = {date(2026, 1, 1): 100.0, date(2026, 1, 31): 200.0, date(2026, 2, 28): 400.0}
    debt = {d: 10.0 for d in equity}
    out = blend(equity, debt, 0.5)
    assert out[date(2026, 1, 31)] == 1500.0
    assert out[date(2026, 2, 28)] == 2250.0


@pytest.mark.parametrize("name, key", [
    ("Motilal Oswal Nifty 50 Index Fund - Direct plan - Growth", "nifty_50"),
    ("SBI NIFTY INDEX FUND - DIRECT PLAN - GROWTH", "nifty_50"),
    ("Mirae Asset Nifty Total Market Index Fund - Direct Plan - Growth", "nifty_total_market"),
    ("Edelweiss NIFTY Large Midcap 250 Index Fund - Direct Plan Growth", "nifty_largemidcap_250"),
    # strategies on a parent index, not the index itself
    ("Motilal Oswal Nifty 500 Momentum 50 Index Fund - Direct Plan - Growth", None),
    ("Kotak Nifty 50 Equal Weight Index Fund - Direct Plan - Growth", None),
    ("ICICI Prudential Nifty500 Multicap 50:25:25 Index Fund - Direct Plan - Growth", None),
])
def test_index_of_real_names(name, key):
    assert index_of(name) == key
