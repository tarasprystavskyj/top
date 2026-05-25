#!/usr/bin/env python3
"""Smoke checks for Telegram one-leg V21 wrapper wiring."""
from __future__ import annotations

import unittest

from obw_platform.telegram_signal_tools.telegram_v21_one_leg_wrapper import (
    load_one_leg_config,
    make_bar,
    make_strategy,
    manage_existing_position,
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

    def test_no_trend_filter_variant_disables_warmup_dependent_sizing_gate(self) -> None:
        cfg = load_one_leg_config(CFG, "long", 100.0, disable_trend_filter=True)
        params = cfg["strategy_params_long"]
        self.assertEqual(float(params["baseOrderPctEq"]), 5.0)
        self.assertEqual(float(params["useTrendAdaptiveSizing"]), 0.0)
        self.assertEqual(float(params["entryTrendStrengthMin"]), 0.0)
        self.assertTrue(cfg["telegram_v21_wrapper"]["disable_trend_filter"])
        strat = make_strategy(cfg, "long")
        opened = open_external_signal(
            strat,
            "BTC/USDT:USDT",
            make_bar("BTC/USDT:USDT", 100.0, "2026-05-19T00:00:00+00:00"),
        )
        self.assertFalse(strat.use_trend_adaptive_sizing)
        self.assertEqual(opened["state"]["num_fills"], 1)

    def test_optional_regime_off_can_force_close_long(self) -> None:
        cfg = load_one_leg_config(
            CFG,
            "long",
            100.0,
            regime_off={
                "enabled": True,
                "mode": "hard_close",
                "lookback_hours": 0.05,
                "required_new_high_within_hours": 0.0166667,
                "bar_minutes": 1.0,
            },
        )
        strat = make_strategy(cfg, "long")
        opened = open_external_signal(
            strat,
            "BTC/USDT:USDT",
            make_bar("BTC/USDT:USDT", 102.0, "2026-05-19T00:00:00+00:00"),
        )
        state = opened["state"]
        result = {"event": None, "state": state}
        regime_event = None
        for i, px in enumerate([101.0, 100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 94.0, 93.0, 92.0, 91.0], start=1):
            result = manage_existing_position(
                strat,
                "BTC/USDT:USDT",
                make_bar("BTC/USDT:USDT", px, f"2026-05-19T00:{i:02d}:00+00:00"),
                result["state"],
            )
            event = result["event"]
            if event is not None and "RegimeOff" in event.get("reason", ""):
                regime_event = event
                break
        self.assertIsNotNone(regime_event)
        self.assertEqual(regime_event["action"], "EXIT")
        self.assertEqual(float(result["state"]["pos_size"]), 0.0)


if __name__ == "__main__":
    unittest.main()
