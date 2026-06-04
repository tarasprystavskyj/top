import copy
import unittest
from pathlib import Path

from obw_platform.meta_strategies.telegram_signal_dca.callme_meta_strategy_config import (
    load_callme_meta_strategy_config,
    resolve_symbol_strategy_config,
)


CFG = Path("obw_platform/meta_strategies/telegram_signal_dca/configs/callme_meta_strategy_live.json")


class CallmeMetaStrategyConfigTest(unittest.TestCase):
    def test_checked_in_callme_default_is_not_hype_silent(self):
        cfg = load_callme_meta_strategy_config(str(CFG))

        self.assertEqual(cfg["lead"]["portfolio_id"], "4512404768792222208")
        self.assertIn(cfg["default_symbol_config"]["tune_status"], {"complete_research", "research_pending"})
        self.assertIn("tuning", cfg)
        self.assertEqual(cfg["runtime"]["live_executor_status"], "todo_multi_symbol_live_adapter")

    def test_known_callme_symbols_inherit_default_without_override_fields(self):
        cfg = load_callme_meta_strategy_config(str(CFG))

        amd = resolve_symbol_strategy_config(cfg, "AMDUSDT")
        avgo = resolve_symbol_strategy_config(cfg, "AVGO/USDT:USDT")
        wildcard = resolve_symbol_strategy_config(cfg, "*")

        self.assertEqual(amd["config_source"], "default_symbol_config")
        self.assertEqual(avgo["config_source"], "default_symbol_config")
        self.assertEqual(wildcard["config_source"], "default_symbol_config")
        self.assertEqual(amd["strategy_config"], cfg["default_symbol_config"])
        self.assertEqual(avgo["strategy_config"], cfg["default_symbol_config"])
        self.assertEqual(wildcard["strategy_config"], cfg["default_symbol_config"])

    def test_per_symbol_strategy_override_deep_merges_default_without_exchange_metadata(self):
        cfg = load_callme_meta_strategy_config(str(CFG))
        tuned = copy.deepcopy(cfg)
        tuned["symbols"]["AMDUSDT"]["strategy_override"] = {
            "inherits": "default_symbol_config",
            "tune_scope": "symbol_only",
            "override_fields": {
                "tune_status": "complete_research",
                "baseline_quality": "per_symbol_research",
                "artifact_kind": "callme_symbol_tune",
                "sizing": {
                    "base_order_policy": "callme_amd_tuned_v21",
                    "base_order_pct_eq": 17.5,
                },
            },
        }

        resolved = resolve_symbol_strategy_config(tuned, "AMD-USDT")

        self.assertTrue(resolved["has_symbol_override"])
        self.assertEqual(resolved["config_source"], "symbols.AMDUSDT.strategy_override.override_fields")
        self.assertEqual(resolved["strategy_config"]["tune_status"], "complete_research")
        self.assertEqual(resolved["strategy_config"]["baseline_quality"], "per_symbol_research")
        self.assertEqual(resolved["strategy_config"]["sizing"]["base_order_policy"], "callme_amd_tuned_v21")
        self.assertEqual(resolved["strategy_config"]["sizing"]["base_order_pct_eq"], 17.5)
        self.assertEqual(resolved["strategy_config"]["sizing"]["dca_eval_interval_sec"], 60.0)
        self.assertNotIn("exchange_symbols", resolved["strategy_config"])


if __name__ == "__main__":
    unittest.main()
