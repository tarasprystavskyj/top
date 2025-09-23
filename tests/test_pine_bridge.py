import tempfile
import unittest
from pathlib import Path

from obw_platform.strategies.pine_bridge import PineBridgeStrategy
from obw_platform.strategies.breakout_avaai_full_with_universe_4 import BreakoutAVAAIFull


class PineBridgeStrategyTests(unittest.TestCase):
    def test_dummy_compiler_instantiates_breakout_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pine_script = tmp_path / "Breakout AVAAI BITGET.pine"
            pine_script.write_text(
                "\n".join(
                    [
                        "//@version=5",
                        "strategy(\"Breakout AVAAI\", overlay=true)",
                        "//@obw_strategy obw_platform.strategies.breakout_avaai_full_with_universe_4.BreakoutAVAAIFull",
                        "//@obw_strategy_params {\"tp_atr_mult\": 5.0, \"top_n\": 12}",
                    ]
                ),
                encoding="utf-8",
            )

            cfg = {
                "strategy_params": {
                    "pine_script_path": str(pine_script),
                    "compiler": {"name": "dummy", "cache_dir": str(tmp_path / "cache")},
                    "base_strategy_params": {"min_atr_ratio": 0.05},
                }
            }

            bridge = PineBridgeStrategy(cfg)

            self.assertIsInstance(bridge._impl, BreakoutAVAAIFull)
            self.assertAlmostEqual(bridge._impl.tp_mult, 5.0)
            self.assertEqual(bridge._impl.top_n, 12)
            self.assertAlmostEqual(bridge._impl.min_atr_ratio, 0.05)
            self.assertIn("cache_package", bridge.metadata)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
