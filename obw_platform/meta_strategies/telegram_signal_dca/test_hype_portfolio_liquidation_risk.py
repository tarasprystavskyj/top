import unittest
from datetime import datetime, timezone

import numpy as np

from obw_platform.meta_strategies.telegram_signal_dca.backtest_hype_portfolio_liquidation_risk import (
    LegConfig,
    effective_leverage,
    load_legs,
    run_backtest,
)
from obw_platform.meta_strategies.telegram_signal_dca.compare_binance_copy_positions_dca import CopyPosition


def ts_minute(n: int) -> int:
    return 1_700_000_000_000 + n * 60_000


def synthetic_arrays():
    return {
        "t": np.array([ts_minute(i) for i in range(3)], dtype=np.int64),
        "open": np.array([100.0, 100.0, 100.0], dtype=float),
        "high": np.array([100.0, 101.0, 100.0], dtype=float),
        "low": np.array([100.0, 89.0, 100.0], dtype=float),
        "close": np.array([100.0, 100.0, 100.0], dtype=float),
    }


def synthetic_position():
    return CopyPosition(
        id="p1",
        symbol="HYPEUSDT",
        side="LONG",
        opened=datetime.fromtimestamp(ts_minute(0) / 1000, tz=timezone.utc),
        closed=datetime.fromtimestamp(ts_minute(2) / 1000, tz=timezone.utc),
        entry=100.0,
        exit=100.0,
        lead_pnl=0.0,
        lead_roi=0.0,
        leverage=10.0,
        margin_mode="Cross",
    )


class HypePortfolioLiquidationRiskTest(unittest.TestCase):
    def test_inline_json_configures_multiple_portfolio_legs(self):
        legs = load_legs(
            legs_json=(
                '[{"name":"a","allocation_usdt":50,"leverage":3,"margin_mode":"isolated"},'
                '{"name":"b","allocation_usdt":75,"leverage":2,"margin_mode":"cross",'
                '"base_frac":0.5,"dca_steps_pct":[1,2],"dca_add_weights":[1,3]}]'
            ),
            portfolio_config=None,
            allocation_usdt=10.0,
            leverage=1.0,
        )

        self.assertEqual([x.name for x in legs], ["a", "b"])
        self.assertEqual(legs[0].margin_mode, "isolated")
        self.assertEqual(legs[1].margin_mode, "cross")
        self.assertEqual(legs[1].dca_steps_pct, (1.0, 2.0))
        self.assertEqual(legs[1].dca_add_weights, (1.0, 3.0))

    def test_source_leverage_mode_uses_position_leverage(self):
        pos = synthetic_position()
        self.assertEqual(effective_leverage(pos, LegConfig(name="fixed", allocation_usdt=100.0, leverage=2.0)), 2.0)
        self.assertEqual(
            effective_leverage(pos, LegConfig(name="source", allocation_usdt=100.0, leverage=2.0, leverage_mode="source")),
            10.0,
        )

    def test_isolated_breach_can_be_cross_safe_with_larger_wallet(self):
        summary, trades, snapshots, cross_rows = run_backtest(
            [synthetic_position()],
            synthetic_arrays(),
            [LegConfig(name="hype", allocation_usdt=100.0, leverage=10.0)],
            initial_equity=100.0,
            fill_mode="close_beyond_skip_boundary",
            entry_source="avgCost",
            fee=0.0,
            slippage=0.0,
            maintenance_margin_pct=0.0,
            include_boundary_risk=False,
        )

        self.assertEqual(len(trades), 1)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(summary["isolated"]["breach_bar_count"], 1)
        self.assertEqual(summary["isolated"]["breach_trade_count"], 1)
        self.assertLess(summary["isolated"]["min_buffer_usd"], 0.0)
        self.assertEqual(summary["cross"]["breach_bar_count"], 0)
        self.assertGreater(cross_rows[0]["cross_equity_buffer_usd"], 0.0)

    def test_cross_breach_counts_concurrent_adverse_mtm(self):
        summary, _, _, _ = run_backtest(
            [synthetic_position()],
            synthetic_arrays(),
            [
                LegConfig(name="hype_a", allocation_usdt=100.0, leverage=10.0),
                LegConfig(name="hype_b", allocation_usdt=100.0, leverage=10.0),
            ],
            initial_equity=20.0,
            fill_mode="close_beyond_skip_boundary",
            entry_source="avgCost",
            fee=0.0,
            slippage=0.0,
            maintenance_margin_pct=0.0,
            include_boundary_risk=False,
        )

        self.assertEqual(summary["cross"]["breach_bar_count"], 1)
        self.assertLessEqual(summary["cross"]["min_equity_buffer_usd"], 0.0)


if __name__ == "__main__":
    unittest.main()
