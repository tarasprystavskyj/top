import argparse
import unittest

from obw_platform.meta_strategies.telegram_signal_dca import hype_grounded_compound_paper_live as mod


class HypeGroundedCompoundPaperLiveTest(unittest.TestCase):
    def test_champion_parameters_match_promoted_candidate(self):
        self.assertEqual(mod.CHAMPION_NAME, "rnd5337_t500_b12_s0p953-1p3-1p442-1p767_w0p597-0p82-1p151-1p868")
        self.assertEqual(mod.BASE_FRAC, 0.12)
        self.assertEqual(mod.STEPS_PCT, (0.953, 1.3, 1.442, 1.767))
        self.assertEqual(mod.ADD_WEIGHTS, (0.597, 0.82, 1.151, 1.868))

    def test_fill_plan_uses_promoted_base_and_add_weights(self):
        args = argparse.Namespace(initial_target_notional=500.0, initial_equity=500.0)
        plan = mod.fill_plan(500.0, args, "LONG", 100.0)

        self.assertAlmostEqual(plan.target_notional, 500.0)
        self.assertAlmostEqual(plan.base_notional, 60.0)
        self.assertEqual(len(plan.add_notionals), 4)
        self.assertAlmostEqual(sum(plan.add_notionals), 440.0)
        self.assertEqual(len(plan.levels), 4)
        self.assertLess(plan.levels[-1], plan.levels[0])


if __name__ == "__main__":
    unittest.main()
