import argparse
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from obw_platform.meta_strategies.telegram_signal_dca import hype_cap100_bingx_live_canary as live
from obw_platform.meta_strategies.telegram_signal_dca import hype_cap100_champion_dca_strategy as champion
from obw_platform.meta_strategies.telegram_signal_dca import hype_cap100_live_disabled_canary as paper
from obw_platform.meta_strategies.telegram_signal_dca import hype_copy_signal_meta_strategy as meta


NOW = datetime(2026, 5, 25, 21, 0, tzinfo=timezone.utc)


def make_args(**overrides):
    base = dict(
        portfolio_id="mock",
        symbol="HYPEUSDT",
        live_symbol="HYPE-USDT",
        initial_equity=30.0,
        initial_target_notional=30.0,
        max_gross_notional_usdt=30.0,
        max_one_side_notional_usdt=30.0,
        max_daily_loss_usdt=5.0,
        max_orders_per_hour=20,
        deadline_utc="2026-05-26T09:00:00Z",
        long_only=True,
        live_exchange_profile="gateio_current",
        live_exchange="gateio",
        position_mode="oneway",
        control_dir="",
        out_dir="unused",
        session_db="",
        run_id="run-split",
        order_error_backoff_sec=300.0,
        order_error_circuit_sec=1800.0,
        order_error_max_consecutive=3,
        entry_failure_cooldown_sec=3600.0,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def source_long(entry=50.0):
    return {
        "key": "HYPEUSDT:LONG",
        "id": "lead-1",
        "symbol": "HYPEUSDT",
        "side": "LONG",
        "entry_price": entry,
    }


def open_trade():
    plan = champion.build_plan(30.0, make_args(), 50.0)
    trade = champion.build_trade_from_source(source_long(), plan, now=NOW, mark=50.0, iso_fn=paper.iso)
    trade.update({"qty": 0.2, "notional": 10.0, "avg_entry": 50.0, "fees_paid": 0.0, "fills": []})
    return trade


class HypeStrategyRunnerSplitTest(unittest.TestCase):
    def test_champion_dca_policy_owns_candidate_189_levels_and_notionals(self):
        args = make_args()
        plan = champion.build_plan(30.0, args, 50.0)
        self.assertEqual(champion.CHAMPION_CANDIDATE_INDEX, 189)
        self.assertEqual(plan["candidate_index"], 189)
        self.assertAlmostEqual(plan["base_notional"], 8.4)
        self.assertEqual(len(plan["levels"]), 4)
        self.assertEqual(len(plan["add_notionals"]), 4)
        self.assertAlmostEqual(plan["levels"][0], 50.0 * (1.0 - 0.25 / 100.0))
        self.assertGreaterEqual(min(plan["add_notionals"]), champion.CHAMPION_PARAMS["dca_min_order_usdt"])

    def test_liquid_base_scaled_policy_uses_safe_ridge_weights(self):
        args = make_args(initial_equity=310.2, initial_target_notional=310.2, max_gross_notional_usdt=310.2, max_one_side_notional_usdt=310.2)
        sizing = {
            "box_config_class": "LiquidBaseScaledBoxConfig",
            "base_order_pct_eq": 9.4,
            "dca_profile": "liquid_base_scaled_dca8",
            "selected_dca_count": 8,
            "dca_steps_pct": [2.159927, 0.700485, 0.541293, 0.458814, 0.406087, 0.368628, 0.340242, 0.317771],
            "dca_add_weights": [0.408249, 0.461742, 0.666465, 0.949181, 1.040554, 1.370149, 2.040652, 2.627936],
            "dca_min_order_usdt": 2.0,
        }
        plan = champion.build_plan(310.2, args, 50.0, sizing=sizing)
        self.assertEqual(plan["box_config_class"], "LiquidBaseScaledBoxConfig")
        self.assertEqual(plan["dca_add_mode"], "base_scaled_weights")
        self.assertEqual(len(plan["levels"]), 8)
        self.assertEqual(len(plan["add_notionals"]), 8)
        self.assertAlmostEqual(plan["base_notional"], 29.1588)
        self.assertAlmostEqual(plan["add_notionals"][0], 29.1588 * 0.408249)
        self.assertLess(sum(plan["add_notionals"]) + plan["base_notional"], 310.2)
        self.assertGreater(sum(plan["add_notionals"]) + plan["base_notional"], 308.0)
        self.assertAlmostEqual(plan["levels"][0], 50.0 * (1.0 - 2.159927 / 100.0))

    def test_meta_strategy_emits_source_close_policy_as_explicit_intent(self):
        args = make_args()
        trade = open_trade()
        state = {"open_trades": {trade["key"]: trade}, "equity": 30.0}
        result = meta.build_strategy_intents(state, [], [], 52.0, NOW, args, allow_dca=False, iso_fn=paper.iso)
        self.assertEqual(len(result["intents"]), 1)
        intent = result["intents"][0]
        self.assertEqual(intent["action"], "CLOSE")
        self.assertEqual(intent["intent_type"], "close_position")
        self.assertEqual(intent["strategy_policy"], "source_close_closes_follower")
        self.assertEqual(intent["reason"], "lead_position_disappeared_mark_fallback")
        self.assertAlmostEqual(intent["expected_exit"], 52.0)

    def test_live_runner_does_not_close_without_strategy_close_intent(self):
        args = make_args()
        trade = open_trade()
        state = {"open_trades": {trade["key"]: trade}, "equity": 30.0}
        with patch.object(live.copy_signal_meta, "build_strategy_intents", return_value={"current_keys": set(), "events": [], "intents": []}), patch.object(
            live, "live_close_trade"
        ) as close_trade:
            events = live.apply_live_snapshot(state, [], [], 52.0, NOW, args, allow_dca=False)
        close_trade.assert_not_called()
        self.assertEqual(events, [])
        self.assertIn(trade["key"], state["open_trades"])

    def test_live_runner_executes_explicit_open_and_dca_intents(self):
        args = make_args()
        trade = open_trade()
        trade.update({"qty": 0.0, "notional": 0.0, "avg_entry": 0.0, "fees_paid": 0.0, "fills": [], "next_level_idx": 0})
        intents = [
            champion.base_entry_intent(trade, expected_price=50.0),
            champion.dca_entry_intents(trade, mark=49.0, allow_dca=True)[0],
        ]
        state = {"open_trades": {}, "equity": 30.0}
        with patch.object(live.copy_signal_meta, "build_strategy_intents", return_value={"current_keys": set(), "events": [], "intents": intents}), patch.object(
            live, "live_add_fill", return_value={"type": "live_fill", "key": trade["key"]}
        ) as add_fill:
            live.apply_live_snapshot(state, [], [], 49.0, NOW, args, allow_dca=True)
        self.assertEqual(add_fill.call_count, 2)
        self.assertIn(trade["key"], state["open_trades"])
        self.assertEqual(trade["next_level_idx"], 1)

    def test_paper_executor_stops_dca_intents_after_first_blocked_dca(self):
        args = make_args()
        trade = open_trade()
        trade["levels"] = [49.9, 49.7, 49.5]
        trade["add_notionals"] = [2.0, 2.0, 2.0]
        trade["next_level_idx"] = 0
        intents = champion.dca_entry_intents(trade, mark=49.0, allow_dca=True)
        state = {"open_trades": {trade["key"]: trade}, "equity": 30.0}
        blocked = {"type": "paper_entry_blocked", "key": trade["key"], "fill_type": "dca_add_1", "reason": "one_side_notional_guard"}
        with patch.object(paper.copy_signal_meta, "build_strategy_intents", return_value={"current_keys": {trade["key"]}, "events": [], "intents": intents}), patch.object(
            paper, "add_fill", return_value=blocked
        ) as add_fill:
            events = paper.apply_snapshot(state, [], [], 49.0, NOW, args)
        self.assertEqual(add_fill.call_count, 1)
        self.assertEqual(events, [blocked])
        self.assertEqual(trade["next_level_idx"], 0)

    def test_live_executor_stops_dca_intents_after_first_blocked_dca(self):
        args = make_args()
        trade = open_trade()
        trade["levels"] = [49.9, 49.7, 49.5]
        trade["add_notionals"] = [2.0, 2.0, 2.0]
        trade["next_level_idx"] = 0
        intents = champion.dca_entry_intents(trade, mark=49.0, allow_dca=True)
        state = {"open_trades": {trade["key"]: trade}, "equity": 30.0}
        blocked = {"type": "live_entry_blocked", "key": trade["key"], "fill_type": "dca_add_1", "reason": "one_side_notional_guard"}
        with patch.object(live.copy_signal_meta, "build_strategy_intents", return_value={"current_keys": {trade["key"]}, "events": [], "intents": intents}), patch.object(
            live, "sync_trade_from_exchange", return_value={"synced": False}
        ), patch.object(live, "live_add_fill", return_value=blocked) as add_fill:
            events = live.apply_live_snapshot(state, [], [], 49.0, NOW, args, allow_dca=True)
        self.assertEqual(add_fill.call_count, 1)
        self.assertEqual(events, [blocked])
        self.assertEqual(trade["next_level_idx"], 0)

    def test_paper_and_live_use_same_meta_and_champion_modules(self):
        self.assertIs(paper.copy_signal_meta, live.copy_signal_meta)
        self.assertIs(paper.champion_dca, champion)


if __name__ == "__main__":
    unittest.main()
