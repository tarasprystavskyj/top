import argparse
import unittest
from datetime import datetime, timezone

from obw_platform.meta_strategies.telegram_signal_dca import paper_live_binance_copy_public_positions as mod


NOW = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)


def args(**overrides):
    base = {
        "notional_usdt": 100.0,
        "sizing_mode": "margin_fraction_mirror",
        "allocation_usdt": 310.2,
        "effective_leverage": 3.0,
        "max_notional_usdt": 310.2,
        "min_adjust_notional_usdt": 0.0,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def pos(amount=2.0, entry=50.0, mark=50.0, fraction=0.02):
    return {
        "key": "HYPEUSDT:LONG",
        "id": "p1",
        "symbol": "HYPEUSDT",
        "side": "LONG",
        "entry_price": entry,
        "mark_price": mark,
        "position_amount": amount,
        "notional_value": amount * mark,
        "leverage": "6",
        "lead_margin_balance_usdt": 1000.0,
        "source_position_margin_usdt": 20.0,
        "source_position_margin_source": "notional_value_div_leverage",
        "source_margin_fraction": fraction,
        "source_margin_fraction_reason": "ok",
        "raw": {"symbol": "HYPEUSDT"},
    }


class PaperLiveBinanceCopyPublicPositionsTest(unittest.TestCase):
    def test_margin_fraction_mirror_opens_and_resizes_on_source_size_changes(self):
        state = mod.default_state("4300516091842181632")
        a = args()
        events = mod.apply_snapshot(state, [pos(fraction=0.02)], [], now=NOW, notional=a.notional_usdt, args=a)
        self.assertEqual(events[0]["type"], "paper_entry")
        trade = state["open_positions"]["HYPEUSDT:LONG"]
        self.assertAlmostEqual(trade["paper_notional_usdt"], 18.612)
        self.assertAlmostEqual(trade["entry_price"], 50.0)

        events = mod.apply_snapshot(state, [pos(mark=48.0, fraction=0.04)], [], now=NOW, notional=a.notional_usdt, args=a)
        self.assertEqual(events[0]["type"], "paper_resize_increase")
        trade = state["open_positions"]["HYPEUSDT:LONG"]
        self.assertAlmostEqual(trade["paper_notional_usdt"], 37.224)
        self.assertLess(trade["entry_price"], 50.0)

        events = mod.apply_snapshot(state, [pos(mark=52.0, fraction=0.01)], [], now=NOW, notional=a.notional_usdt, args=a)
        self.assertEqual(events[0]["type"], "paper_resize_decrease")
        trade = state["open_positions"]["HYPEUSDT:LONG"]
        self.assertAlmostEqual(trade["paper_notional_usdt"], 9.306)
        self.assertEqual(len(trade["partial_closes"]), 1)
        self.assertGreater(trade["realized_pnl_usdt"], 0.0)


if __name__ == "__main__":
    unittest.main()
