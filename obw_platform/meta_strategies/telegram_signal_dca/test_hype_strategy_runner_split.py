import argparse
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from obw_platform.meta_strategies.telegram_signal_dca import hype_cap100_bingx_live_canary as live
from obw_platform.meta_strategies.telegram_signal_dca import hype_cap100_champion_dca_strategy as champion
from obw_platform.meta_strategies.telegram_signal_dca import hype_cap100_live_disabled_canary as paper
from obw_platform.meta_strategies.telegram_signal_dca import hype_copy_signal_meta_strategy as meta
from obw_platform.meta_strategies.telegram_signal_dca import meta_strategy_policy_config


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
        meta_strategy_config_dir="",
        strategy_config="",
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

    def test_meta_strategy_defaults_to_hype_champion_without_config(self):
        args = make_args()
        state = {"open_trades": {}, "equity": 30.0}
        result = meta.build_strategy_intents(state, [source_long()], [], 50.0, NOW, args, allow_dca=False, iso_fn=paper.iso)
        self.assertEqual(len(result["intents"]), 1)
        intent = result["intents"][0]
        self.assertEqual(intent["candidate_index"], champion.CHAMPION_CANDIDATE_INDEX)
        self.assertAlmostEqual(intent["notional"], 8.4)
        self.assertEqual(intent["strategy_policy"], "copy_source_open_base_entry")

    def test_meta_strategy_uses_symbol_config_for_amd_contract_sized_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp)
            (cfg_dir / "AMDUSDT.json").write_text(
                """
{
  "schema": "telegram_signal_dca_meta_strategy_config_v1",
  "name": "unit_amd_contract_policy",
  "symbols": ["AMDUSDT"],
  "candidate_index": 21001,
  "strategy_policy": "unit_configured_dca",
  "dca": {
    "contract_size_base": 0.01,
    "base_contracts": 1,
    "add_contracts": [1, 2],
    "drops_pct": [1.0, 2.0]
  }
}
""".strip(),
                encoding="utf-8",
            )
            args = make_args(
                symbol="AMDUSDT",
                live_symbol="AMD/USDT:USDT",
                initial_equity=100.0,
                initial_target_notional=100.0,
                max_gross_notional_usdt=100.0,
                max_one_side_notional_usdt=100.0,
                meta_strategy_config_dir=str(cfg_dir),
            )
            pos = {
                "key": "AMDUSDT:LONG",
                "id": "amd-1",
                "symbol": "AMDUSDT",
                "side": "LONG",
                "entry_price": 500.0,
            }
            result = meta.build_strategy_intents({}, [pos], [], 494.0, NOW, args, allow_dca=True, iso_fn=paper.iso)
        self.assertEqual([intent["intent_type"] for intent in result["intents"]], ["open_entry", "dca_entry"])
        base, dca_intent = result["intents"]
        self.assertEqual(base["candidate_index"], 21001)
        self.assertEqual(base["strategy_policy"], "unit_configured_dca")
        self.assertAlmostEqual(base["notional"], 5.0)
        self.assertAlmostEqual(dca_intent["expected_price"], 495.0)
        self.assertAlmostEqual(dca_intent["notional"], 4.95)

    def test_single_strategy_config_overrides_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "custom.json"
            cfg.write_text(
                '{"name":"single_override","symbols":["AMDUSDT"],"candidate_index":21002,"dca":{"base_notional_usdt":7,"add_notionals_usdt":[3],"drops_pct":[1]}}',
                encoding="utf-8",
            )
            args = make_args(symbol="AMDUSDT", strategy_config=str(cfg), initial_equity=20, initial_target_notional=20, max_gross_notional_usdt=20)
            policy = meta_strategy_policy_config.resolve_policy(args, "AMDUSDT")
            plan = policy.build_plan(20.0, args, 500.0)
        self.assertEqual(plan["candidate_index"], 21002)
        self.assertAlmostEqual(plan["base_notional"], 7.0)
        self.assertEqual(plan["add_notionals"], [3.0])


if __name__ == "__main__":
    unittest.main()
