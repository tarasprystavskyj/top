import os, sys, pytest
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from obw_platform.strategies.breakout_avaai_full_with_universe_4 import BreakoutAVAAIFull

def make_strategy(extra_cfg=None):
    cfg = {"strategy_params": {}}
    if extra_cfg:
        cfg.update(extra_cfg)
    return BreakoutAVAAIFull(cfg)

def test_entry_caps_per_bar():
    strat = make_strategy({"first_bar_max_positions": 2, "max_new_positions_per_bar": 1})
    row = {
        "dp6h": 0.05,
        "dp12h": 0.05,
        "atr_ratio": 0.02,
        "qv_24h": 1e6,
        "quote_volume": 1e5,
        "close": 100.0,
    }
    t0 = 1
    assert strat.entry_signal(t0, "AAA", row) is not None
    assert strat.entry_signal(t0, "BBB", row) is not None
    assert strat.entry_signal(t0, "CCC", row) is None
    t1 = 2
    assert strat.entry_signal(t1, "DDD", row) is not None
    assert strat.entry_signal(t1, "EEE", row) is None
