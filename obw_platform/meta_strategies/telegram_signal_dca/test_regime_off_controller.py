import unittest

from obw_platform.telegram_signal_tools.regime_off_controller import (
    RegimeOffConfig,
    RegimeOffController,
)


class RegimeOffControllerTest(unittest.TestCase):
    def test_hard_close_blocks_after_new_high_timeout(self) -> None:
        cfg = RegimeOffConfig(
            enabled=True,
            mode="hard_close",
            lookback_bars=5,
            required_new_high_bars=2,
            min_history_bars=3,
        )
        ctl = RegimeOffController(cfg)
        for px in [100.0, 101.0, 102.0]:
            self.assertTrue(ctl.update(px).allow_new_long)

        self.assertTrue(ctl.update(101.0).allow_new_long)
        self.assertTrue(ctl.update(100.0).allow_new_long)
        decision = ctl.update(99.0, has_long_position=True)
        self.assertFalse(decision.allow_new_long)
        self.assertTrue(decision.should_close_long)
        self.assertEqual(decision.reason, "regime_off")

    def test_fresh_override_allows_fresh_signal_when_regime_off(self) -> None:
        cfg = RegimeOffConfig(
            enabled=True,
            mode="fresh_override",
            lookback_bars=5,
            required_new_high_bars=1,
            min_history_bars=3,
        )
        ctl = RegimeOffController(cfg)
        for px in [100.0, 101.0, 102.0, 100.0]:
            ctl.update(px)

        blocked = ctl.update(99.0, signal_fresh=False, has_long_position=True)
        self.assertFalse(blocked.allow_new_long)
        self.assertTrue(blocked.should_close_long)

        allowed = ctl.update(98.0, signal_fresh=True, has_long_position=True)
        self.assertTrue(allowed.allow_new_long)
        self.assertFalse(allowed.should_close_long)
        self.assertEqual(allowed.reason, "signal_fresh_override")

    def test_from_hours_converts_to_bars(self) -> None:
        cfg = RegimeOffConfig.from_hours(
            lookback_hours=168,
            required_new_high_within_hours=72,
            bar_minutes=1,
        )
        self.assertEqual(cfg.lookback_bars, 168 * 60)
        self.assertEqual(cfg.required_new_high_bars, 72 * 60)


if __name__ == "__main__":
    unittest.main()
