import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import compare_binance_copy_positions_dca as cmp  # noqa: E402


class CompareCopyPositionsLeverageTest(unittest.TestCase):
    def test_intrabar_low_counts_liquidation_touch_missed_by_close(self):
        pos = cmp.CopyPosition(
            id="p1",
            symbol="AMDUSDT",
            side="LONG",
            opened=datetime(2026, 6, 2, tzinfo=timezone.utc),
            closed=datetime(2026, 6, 2, 0, 2, tzinfo=timezone.utc),
            entry=100.0,
            exit=100.0,
            lead_pnl=0.0,
            lead_roi=0.0,
            leverage=10.0,
            margin_mode="Cross",
        )
        policy = {"fee": 0.0, "slippage": 0.0, "target_notional": 100.0, "long": {"base_notional": 100.0, "steps": [], "adds": []}, "short": {"base_notional": 100.0, "steps": [], "adds": []}}
        rows = [{"t": 1, "open": 100.0, "high": 101.0, "low": 89.0, "close": 100.0}]
        out = cmp.simulate_position(pos, rows, policy=policy, dca_count=0, leverage_mode="copy", account_equity=100.0)
        self.assertEqual(out["effective_leverage"], 10.0)
        self.assertEqual(out["liq_touch_count"], 1)
        self.assertLess(out["min_liq_buffer_pct"], 0.0)

    def test_copy_div2_halves_source_leverage_flooring_to_integer(self):
        pos = cmp.CopyPosition(
            id="p2",
            symbol="AMDUSDT",
            side="LONG",
            opened=datetime(2026, 6, 2, tzinfo=timezone.utc),
            closed=datetime(2026, 6, 2, 0, 1, tzinfo=timezone.utc),
            entry=100.0,
            exit=101.0,
            lead_pnl=0.0,
            lead_roi=0.0,
            leverage=7.0,
            margin_mode="Cross",
        )
        self.assertEqual(cmp.effective_leverage_for_mode(pos, "copy_div2"), 3.0)


if __name__ == "__main__":
    unittest.main()
