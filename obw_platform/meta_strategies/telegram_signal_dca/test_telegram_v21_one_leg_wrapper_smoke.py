#!/usr/bin/env python3
"""Smoke checks for Telegram one-leg V21 wrapper wiring."""
from __future__ import annotations

import unittest

from obw_platform.telegram_signal_tools.telegram_v21_one_leg_wrapper import (
    load_one_leg_config,
    make_bar,
    make_strategy,
    open_external_signal,
)


CFG = "obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml"


class TelegramV21OneLegWrapperSmokeTest(unittest.TestCase):
    def test_long_signal_uses_only_long_class_and_overrides_capital(self) -> None:
        cfg = load_one_leg_config(CFG, "long", 100.0)
        self.assertEqual(cfg["telegram_v21_wrapper"]["active_side"], "LONG")
        self.assertEqual(
            cfg["telegram_v21_wrapper"]["active_strategy_class"],
            "strategies.cryptomine_pack_dual_full.CryptomineLongPackAdaptiveEven",
        )
        self.assertFalse(cfg["telegram_v21_wrapper"]["opposite_leg_enabled"])
        self.assertEqual(float(cfg["strategy_params_long"]["equityForSizingUSDT"]), 100.0)
        self.assertEqual(float(cfg["strategy_params_long"]["baseOrderPctEq"]), 5.0)
        strat = make_strategy(cfg, "long")
        opened = open_external_signal(strat, "BTC/USDT:USDT", make_bar("BTC/USDT:USDT", 100.0, "2026-05-19T00:00:00+00:00"))
        self.assertEqual(opened["side"], "LONG")
        self.assertGreater(opened["qty"] * opened["entry_price"], 0.0)
        self.assertEqual(strat.base_order_pct_eq, 5.0)
        self.assertTrue(strat.use_trend_adaptive_sizing)
        self.assertEqual(opened["state"]["num_fills"], 1)

    def test_short_signal_uses_only_short_class_and_overrides_capital(self) -> None:
        cfg = load_one_leg_config(CFG, "short", 200.0)
        self.assertEqual(cfg["telegram_v21_wrapper"]["active_side"], "SHORT")
        self.assertEqual(
            cfg["telegram_v21_wrapper"]["active_strategy_class"],
            "strategies.cryptomine_pack_dual_full.CryptomineShortPackAdaptiveEven",
        )
        self.assertFalse(cfg["telegram_v21_wrapper"]["opposite_leg_enabled"])
        self.assertEqual(float(cfg["strategy_params_short"]["equityForSizingUSDT"]), 200.0)
        self.assertEqual(float(cfg["strategy_params_short"]["baseOrderPctEq"]), 5.0)
        strat = make_strategy(cfg, "short")
        opened = open_external_signal(strat, "ETH/USDT:USDT", make_bar("ETH/USDT:USDT", 100.0, "2026-05-19T00:00:00+00:00"))
        self.assertEqual(opened["side"], "SHORT")
        self.assertGreater(opened["qty"] * opened["entry_price"], 0.0)
        self.assertEqual(strat.base_order_pct_eq, 5.0)
        self.assertTrue(strat.use_trend_adaptive_sizing)
        self.assertEqual(opened["state"]["num_fills"], 1)


if __name__ == "__main__":
    unittest.main()
