import argparse
import unittest
from datetime import datetime, timezone

from obw_platform.meta_strategies.telegram_signal_dca.hype_cap100_live_disabled_canary import (
    CHAMPION_PARAMS,
    apply_snapshot,
    build_plan,
    build_arg_parser,
    deadline_reached,
    default_state,
    guard_new_entry,
    parse_optional_deadline_utc,
    status_payload,
)


def make_args(**overrides):
    base = dict(
        portfolio_id="mock",
        symbol="HYPEUSDT",
        initial_equity=30.0,
        initial_target_notional=30.0,
        max_gross_notional_usdt=30.0,
        max_one_side_notional_usdt=30.0,
        max_daily_loss_usdt=5.0,
        max_orders_per_hour=20,
        deadline_utc="2026-05-26T09:00:00Z",
        long_only=True,
        state_path="state.json",
        telemetry_path="telemetry.jsonl",
        max_events=2000,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def mock_long(entry=50.0, mark=50.0):
    return {
        "key": "HYPEUSDT:LONG",
        "id": "mock",
        "symbol": "HYPEUSDT",
        "side": "LONG",
        "entry_price": entry,
        "mark_price": mark,
    }


class HypeCap100LiveDisabledCanaryTest(unittest.TestCase):
    def test_deadline_is_disabled_by_default_and_when_empty(self):
        args = build_arg_parser().parse_args([])
        self.assertEqual(args.deadline_utc, "")
        self.assertIsNone(parse_optional_deadline_utc(""))
        self.assertIsNone(parse_optional_deadline_utc("never"))
        self.assertFalse(deadline_reached(datetime(2026, 5, 30, 19, 39, tzinfo=timezone.utc), ""))

    def test_guard_allows_entries_without_deadline_but_blocks_explicit_expired_deadline(self):
        now = datetime(2026, 5, 30, 19, 39, tzinfo=timezone.utc)
        state = default_state(make_args(deadline_utc=""))
        ok, reason, _ = guard_new_entry(state, side="LONG", add_notional=1.0, mark=50.0, now=now, args=make_args(deadline_utc=""))
        self.assertTrue(ok)
        self.assertEqual(reason, "allowed")
        ok, reason, _ = guard_new_entry(state, side="LONG", add_notional=1.0, mark=50.0, now=now, args=make_args(deadline_utc="2026-05-30T07:01:28Z"))
        self.assertFalse(ok)
        self.assertEqual(reason, "runtime_deadline_reached")

    def test_current_champion_params_use_96h_tp_freshness(self):
        self.assertEqual(CHAMPION_PARAMS["tp_freshness_ms"], 345600000)

    def test_build_plan_supports_fixed_dca_adds_independent_from_base_entry(self):
        args = make_args(initial_equity=100.0, initial_target_notional=100.0, max_gross_notional_usdt=100.0)
        old_mode = CHAMPION_PARAMS.get("dca_add_mode")
        old_fixed = CHAMPION_PARAMS.get("dca_add_notional_usdt")
        old_fresh = CHAMPION_PARAMS.get("fresh_base_pct")
        try:
            CHAMPION_PARAMS["dca_add_mode"] = "fixed"
            CHAMPION_PARAMS["dca_add_notional_usdt"] = 2.5
            CHAMPION_PARAMS["fresh_base_pct"] = 30.0
            plan = build_plan(100.0, args, 50.0)
        finally:
            CHAMPION_PARAMS["dca_add_mode"] = old_mode
            CHAMPION_PARAMS["dca_add_notional_usdt"] = old_fixed
            CHAMPION_PARAMS["fresh_base_pct"] = old_fresh
        self.assertEqual(plan["base_notional"], 30.0)
        self.assertEqual(plan["dca_add_mode"], "fixed")
        self.assertEqual(plan["add_notionals"], [2.5, 2.5, 2.5, 2.5])

    def test_build_plan_supports_min_order_dca_adds(self):
        args = make_args(initial_equity=100.0, initial_target_notional=100.0, max_gross_notional_usdt=100.0)
        old_mode = CHAMPION_PARAMS.get("dca_add_mode")
        old_min = CHAMPION_PARAMS.get("dca_min_order_usdt")
        try:
            CHAMPION_PARAMS["dca_add_mode"] = "min_order"
            CHAMPION_PARAMS["dca_min_order_usdt"] = 2.0
            plan = build_plan(100.0, args, 50.0)
        finally:
            CHAMPION_PARAMS["dca_add_mode"] = old_mode
            CHAMPION_PARAMS["dca_min_order_usdt"] = old_min
        self.assertEqual(plan["dca_add_mode"], "min_order")
        self.assertEqual(plan["add_notionals"], [2.0, 2.0, 2.0, 2.0])

    def test_build_plan_preserves_candidate_189_proportional_sizing(self):
        args = make_args(initial_equity=30.0, initial_target_notional=30.0, max_gross_notional_usdt=30.0)
        plan = build_plan(30.0, args, 58.887)
        self.assertNotIn("min_order_qty_hype", CHAMPION_PARAMS)
        self.assertEqual(plan["min_order_notional"], 0.0)
        self.assertAlmostEqual(plan["base_notional"], 8.4)
        self.assertEqual([round(x, 6) for x in plan["add_notionals"]], [3.2, 4.8, 8.8, 4.8])
        self.assertAlmostEqual(plan["base_notional"] + sum(plan["add_notionals"]), 30.0)

    def test_over_cap_entry_is_rejected_by_gross_guard(self):
        args = make_args(initial_target_notional=120.0)
        state = default_state(args)
        state["open_trades"]["existing"] = {"side": "LONG", "notional": 29.0, "avg_entry": 50.0, "fees_paid": 0.0}
        ok, reason, detail = guard_new_entry(
            state,
            side="LONG",
            add_notional=2.0,
            mark=50.0,
            now=datetime(2026, 5, 25, 21, 0, tzinfo=timezone.utc),
            args=args,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "gross_notional_guard")
        self.assertEqual(detail["projected_gross_open_notional"], 31.0)

    def test_apply_snapshot_blocks_over_cap_base_entry(self):
        args = make_args(
            initial_equity=120.0,
            initial_target_notional=120.0,
            max_gross_notional_usdt=120.0,
            max_one_side_notional_usdt=30.0,
        )
        state = default_state(args)
        events = apply_snapshot(
            state,
            [mock_long()],
            [],
            50.0,
            datetime(2026, 5, 25, 21, 0, tzinfo=timezone.utc),
            args,
        )
        self.assertEqual(events[0]["type"], "paper_entry_blocked")
        self.assertEqual(events[0]["reason"], "one_side_notional_guard")
        self.assertEqual(state["open_trades"], {})

    def test_daily_loss_blocks_new_entries(self):
        args = make_args()
        state = default_state(args)
        state["open_trades"]["loser"] = {
            "key": "loser",
            "symbol": "HYPEUSDT",
            "side": "LONG",
            "notional": 30.0,
            "avg_entry": 50.0,
            "fees_paid": 0.0,
        }
        ok, reason, _ = guard_new_entry(
            state,
            side="LONG",
            add_notional=1.0,
            mark=40.0,
            now=datetime(2026, 5, 25, 21, 0, tzinfo=timezone.utc),
            args=args,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "daily_loss_guard")

    def test_short_signal_ignored_when_long_only(self):
        args = make_args(long_only=True)
        state = default_state(args)
        short_pos = mock_long()
        short_pos["key"] = "HYPEUSDT:SHORT"
        short_pos["side"] = "SHORT"
        events = apply_snapshot(state, [short_pos], [], 50.0, datetime(2026, 5, 25, 21, 0, tzinfo=timezone.utc), args)
        self.assertEqual(events, [{"type": "signal_ignored", "reason": "long_only", "key": "HYPEUSDT:SHORT"}])
        self.assertEqual(state["open_trades"], {})

    def test_status_reports_paper_only_and_guards(self):
        args = make_args()
        state = default_state(args)
        payload = status_payload(state, 50.0, datetime(2026, 5, 25, 21, 0, tzinfo=timezone.utc), [], {"mock": True}, args)
        self.assertTrue(payload["paper_only"])
        self.assertFalse(payload["live_order_code_present"])
        self.assertEqual(payload["guards"]["max_gross_notional_usdt"], 30.0)


if __name__ == "__main__":
    unittest.main()
