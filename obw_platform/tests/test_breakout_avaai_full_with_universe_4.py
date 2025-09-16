import os, sys, pytest
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from obw_platform.strategies.breakout_avaai_full_with_universe_4 import BreakoutAVAAIFull

def make_strategy(extra_cfg=None):
    cfg = {"strategy_params": {}}
    if extra_cfg:
        cfg.update(extra_cfg)
    return BreakoutAVAAIFull(cfg)

class DummyPos:
    def __init__(self, side="LONG", entry=100.0, tp=110.0, sl=95.0, qty=1.0):
        self.side = side
        self.entry = entry
        self.entry_price = entry
        self.take_profit = tp
        self.tp = tp
        self.stop_price = sl
        self.sl = sl
        self.qty = qty
        self.size = qty
        self.notional = entry * qty
        self.meta = {}

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


def test_manage_position_moves_sl_to_be_and_partial_long():
    strat = make_strategy()
    pos = DummyPos()
    row = {
        "close": 106.0,
        "dp6h": 0.1,
        "dp12h": 0.1,
        "atr_ratio": 0.03,
        "qv_24h": 1e6,
        "quote_volume": 1e5,
    }
    sig = strat.manage_position("AAA", row, pos)
    assert sig.action == "TP_PARTIAL"
    assert pos.stop_price == pytest.approx(pos.entry_price)
    assert pos.sl == pytest.approx(pos.entry_price)


def test_manage_position_moves_sl_to_be_without_partial():
    strat = make_strategy({"partial_tp_enable": False})
    pos = DummyPos()
    row = {
        "close": 106.0,
        "dp6h": 0.1,
        "dp12h": 0.1,
        "atr_ratio": 0.03,
        "qv_24h": 1e6,
        "quote_volume": 1e5,
    }
    sig = strat.manage_position("AAA", row, pos)
    assert sig.action == "HOLD"
    assert pos.stop_price == pytest.approx(pos.entry_price)
    assert pos.sl == pytest.approx(pos.entry_price)


def test_manage_position_moves_sl_to_be_short():
    strat = make_strategy()
    pos = DummyPos(side="SHORT", entry=100.0, tp=90.0, sl=105.0)
    row = {
        "close": 94.0,
        "dp6h": -0.1,
        "dp12h": -0.1,
        "atr_ratio": 0.03,
        "qv_24h": 1e6,
        "quote_volume": 1e5,
    }
    sig = strat.manage_position("AAA", row, pos)
    assert sig.action == "TP_PARTIAL"
    assert pos.stop_price == pytest.approx(pos.entry_price)
    assert pos.sl == pytest.approx(pos.entry_price)
