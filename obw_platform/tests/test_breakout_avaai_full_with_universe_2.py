import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from obw_platform.strategies.breakout_avaai_full_with_universe_2 import BreakoutAVAAIFull


def make_strategy(extra_cfg=None):
    cfg = {"strategy_params": {}}
    if extra_cfg:
        cfg.update(extra_cfg)
    return BreakoutAVAAIFull(cfg)


def test_pct_gap_functions():
    strat = make_strategy()
    assert strat._pct_gap(5, 10) == pytest.approx(0.5)
    assert strat._pct_gap(12, 10) == 0.0
    assert strat._pct_gap_rev(5, 10) == pytest.approx(0.5)


def test_vol_ok_and_gap():
    strat = make_strategy()
    row = {"qv_24h": 240000, "quote_volume": 6000}
    ok, gap, ctx = strat._vol_ok_and_gap(row, vol_mult=1.2)
    assert not ok
    assert gap == pytest.approx(0.5)
    assert ctx["need"] == pytest.approx(12000)


def test_universe_and_rank():
    cfg = {
        "top-n": 2,
        "side": "BOTH",
        "min_momentum_sum": 0.02,
        "min_atr_ratio": 0.0,
        "min_qv_24h": 0.0,
        "min_qv_1h": 0.0,
    }
    strat = make_strategy(cfg)
    md_slice = {
        "AAA": {"dp6h": 0.05, "dp12h": 0.07, "atr_ratio": 0.02, "qv_24h": 1e6, "quote_volume": 1e5},
        "BBB": {"dp6h": 0.03, "dp12h": 0.05, "atr_ratio": 0.02, "qv_24h": 1e6, "quote_volume": 1e5},
        "CCC": {"dp6h": 0.01, "dp12h": 0.01, "atr_ratio": 0.02, "qv_24h": 1e6, "quote_volume": 1e5},
    }
    uni = strat.universe(0, md_slice)
    assert uni == ["AAA", "BBB"]
    assert strat.rank(0, md_slice, universe=uni) == ["AAA", "BBB"]


def test_entry_distance_reason_and_heat():
    strat = make_strategy({"min_qv_1h": 15000})
    row = {
        "dp6h": 0.04,
        "dp12h": 0.05,
        "atr_ratio": 0.02,
        "qv_24h": 240000,
        "quote_volume": 12000,
    }
    dist = strat.entry_distance(0, "AAA", row, breadth=1.0)
    assert "qv1h low" in dist["reason"]
    heat = strat.heat(0, "AAA", row)
    assert heat < 1.0


def test_best_entry_distance_selects_smallest_gap():
    strat = make_strategy()
    md_slice = {
        "AAA": {"dp6h": 0.05, "dp12h": 0.05, "atr_ratio": 0.02, "qv_24h": 300000, "quote_volume": 20000},
        "BBB": {"dp6h": 0.07, "dp12h": 0.07, "atr_ratio": 0.02, "qv_24h": 300000, "quote_volume": 6000},
    }
    best = strat.best_entry_distance(0, md_slice)
    assert best["symbol"] == "AAA"


def test_entry_signal_heat_fallback():
    cfg = {
        "open_on_heat": True,
        "open_heat_min": 0.8,
    }
    strat = make_strategy(cfg)
    row = {
        "dp6h": 0.05,
        "dp12h": 0.05,
        "atr_ratio": 0.02,
        "qv_24h": 300000,
        "quote_volume": 20000,
        "close": 100,
    }
    sig = strat.entry_signal(0, "AAA", row)
    assert sig is not None
    assert sig["side"] == "LONG"
    assert sig["heat"] >= 0.8
