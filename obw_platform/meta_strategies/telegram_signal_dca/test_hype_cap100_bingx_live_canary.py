import argparse
import csv
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

fake_live_runner_dual = types.ModuleType("obw_platform.runners.live_runner_dual")
fake_live_runner_dual._extract_order_id = lambda order: str((order or {}).get("id") or "")
fake_live_runner_dual._fetch_exchange_position = lambda *args, **kwargs: None
fake_live_runner_dual._fetch_order_fill = lambda *args, **kwargs: (None, None, None)
fake_live_runner_dual._normalize_order_qty = lambda _client, _symbol, qty, is_close=False, max_qty=None: min(qty, max_qty) if max_qty is not None else qty
sys.modules.setdefault("obw_platform.runners.live_runner_dual", fake_live_runner_dual)

from obw_platform.meta_strategies.telegram_signal_dca import hype_cap100_bingx_live_canary as live


NOW = datetime(2026, 5, 25, 21, 0, tzinfo=timezone.utc)


def make_args(**overrides):
    base = dict(
        portfolio_id="mock",
        symbol="HYPEUSDT",
        out_dir="unused",
        state_path="",
        status_path="",
        telemetry_path="",
        initial_equity=30.0,
        initial_target_notional=30.0,
        max_gross_notional_usdt=30.0,
        max_one_side_notional_usdt=30.0,
        max_daily_loss_usdt=5.0,
        max_orders_per_hour=20,
        deadline_utc="2026-05-26T09:00:00Z",
        long_only=True,
        history_page_size=50,
        timeout_sec=1.0,
        interval_sec=1.0,
        max_events=2000,
        mock_open_long=False,
        mock_open_short=False,
        mock_no_position=False,
        mock_entry=50.0,
        mock_mark=50.0,
        once=True,
        loop=False,
        live_exchange_profile="bingx_legacy",
        env_file="",
        live_exchange="bingx",
        live_symbol="HYPE-USDT",
        position_mode="oneway",
        dca_eval_interval_sec=60.0,
        history_poll_interval_sec=60.0,
        control_dir="",
        session_db="",
        run_id="run-1",
        order_sync_wait_sec=0.0,
        order_sync_poll_sec=0.01,
        order_error_backoff_sec=300.0,
        order_error_circuit_sec=1800.0,
        order_error_max_consecutive=3,
        max_order_attempts_per_hour=20,
        order_post_throttle_sec=2.0,
        entry_failure_cooldown_sec=3600.0,
        mark_poll_interval_sec=0.0,
        source_leverage_mode="ignore",
        source_margin_mode_override="",
        fixed_source_leverage=0.0,
        max_source_leverage=0.0,
        source_size_sync_mode="off",
        source_size_sync_interval_sec=60.0,
        source_size_sync_min_change_pct=0.0,
        source_size_sync_min_adjust_notional_usdt=0.0,
        hot_restart_snapshot_path="",
        resume_snapshot="",
        resume_snapshot_overwrite=False,
        stdout_log_path="",
        protection_account_loss_stop_usdt=0.0,
        protection_floating_pnl_stop_usdt=0.0,
        protection_emergency_account_loss_usdt=0.0,
        protection_stale_market_sec=0.0,
        protection_require_book_ok=False,
        protection_require_premium_ok=False,
        protection_auto_stop_new_orders=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class FakeExchange:
    def __init__(self, orders=None, failures=None, balance=None):
        self.orders = list(orders or [{"id": "ex-1", "average": 50.0, "filled": 0.2}])
        self.failures = list(failures or [])
        self.calls = []
        self.leverage_calls = []
        self.balance_calls = []
        self.balance = balance or {"USDT": {"total": 1.0}}
        self.markets = {"HYPE/USDT:USDT": {"contractSize": 0.1, "info": {"quanto_multiplier": "0.1"}}}

    def market(self, symbol):
        return self.markets[symbol]

    def create_order(self, *args):
        self.calls.append(args)
        if self.failures:
            raise Exception(self.failures.pop(0))
        return self.orders.pop(0)

    def set_leverage(self, *args):
        self.leverage_calls.append(args)
        if self.failures:
            raise Exception(self.failures.pop(0))
        return {"id": "lev-1", "leverage": args[0], "symbol": args[1], "params": args[2] if len(args) > 2 else {}}

    def fetch_balance(self, params=None):
        self.balance_calls.append(params or {})
        return self.balance


class FakeClient:
    credentials_present = True

    def __init__(self, exchange=None):
        self.ex = exchange or FakeExchange()

    def resolve_symbol(self, symbol):
        return "HYPE/USDT:USDT" if symbol else ""

    def debug_credentials_report(self):
        return {"key_found": True, "secret_found": True}


class StrictFakeClient(FakeClient):
    def __init__(self, symbols):
        super().__init__()
        self.symbols = dict(symbols)

    def resolve_symbol(self, symbol):
        return self.symbols.get(str(symbol or ""))


def lead_long(entry=50.0):
    return {
        "key": "HYPEUSDT:LONG",
        "id": "lead-1",
        "symbol": "HYPEUSDT",
        "side": "LONG",
        "entry_price": entry,
    }


def open_trade():
    return {
        "key": "HYPEUSDT:LONG",
        "lead_position_id": "lead-1",
        "symbol": "HYPEUSDT",
        "side": "LONG",
        "opened_at_utc": live.paper.iso(NOW),
        "lead_entry_price": 50.0,
        "source_leverage_raw": "6",
        "source_leverage": 6.0,
        "source_margin_mode": "Cross",
        "target_notional": 30.0,
        "base_notional": 8.4,
        "add_notionals": [1.0],
        "levels": [49.0],
        "next_level_idx": 0,
        "qty": 0.2,
        "notional": 10.0,
        "avg_entry": 50.0,
        "fees_paid": 0.0,
        "fills": [],
    }


class HypeCap100BingXLiveCanaryTest(unittest.TestCase):
    def test_callable_surface_inventory_is_intentional(self):
        expected = {
            "stable_client_order_id",
            "gateio_client_order_text",
            "order_id_from_response",
            "record_session_order",
            "upsert_session_position",
            "control_paths",
            "hot_stop_path",
            "control_state",
            "default_hot_restart_snapshot_path",
            "build_hot_restart_snapshot",
            "write_hot_restart_snapshot",
            "load_resume_snapshot",
            "handle_hot_stop_if_requested",
            "load_env_file",
            "safe_order",
            "avg_price",
            "live_client",
            "auth_probe",
            "live_balance_params",
            "parse_source_leverage",
            "normalize_source_margin_mode",
            "effective_source_leverage",
            "leverage_cache_key",
            "live_leverage_params",
            "ensure_symbol_leverage",
            "live_order_params",
            "reset_exchange_failures_on_switch",
            "submit_open",
            "submit_close",
            "dca_eval_due",
            "load_inputs_live",
            "sleep_until_next_poll",
            "sync_trade_from_exchange",
            "live_add_fill",
            "live_close_trade",
            "apply_live_snapshot",
            "status_payload",
            "poll_once",
            "build_arg_parser",
            "normalize_paths",
            "validate_args",
            "main",
        }
        actual = {name for name, obj in vars(live).items() if getattr(obj, "__module__", "") == live.__name__ and callable(obj)}
        self.assertTrue(expected.issubset(actual))

    def test_stable_client_order_id_is_deterministic_and_safe(self):
        first = live.stable_client_order_id("entry", "HYPEUSDT:LONG", "lead-1", 0)
        second = live.stable_client_order_id("entry", "HYPEUSDT:LONG", "lead-1", 0)
        other = live.stable_client_order_id("entry", "HYPEUSDT:LONG", "lead-1", 1)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertLessEqual(len(first), 29)
        self.assertTrue(first.startswith("hypecap100-"))

    def test_exchange_client_order_id_seed_includes_run_context(self):
        a = live.stable_client_order_id("entry", "run-a", "HYPEUSDT:LONG", "lead-1", "2026-05-25T21:00:00Z", "base", 0)
        b = live.stable_client_order_id("entry", "run-b", "HYPEUSDT:LONG", "lead-1", "2026-05-25T21:00:00Z", "base", 0)
        c = live.stable_client_order_id("entry", "run-a", "HYPEUSDT:LONG", "lead-1", "2026-05-25T21:01:00Z", "base", 0)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertLessEqual(len(a), 29)

    def test_order_id_safe_order_and_avg_price_helpers(self):
        order = {"id": "id-1", "clientOrderId": "client-1", "average": "51.5", "info": {"secret": "nope", "positionSide": "LONG"}}
        with patch.object(live, "_extract_order_id", side_effect=RuntimeError("bad")):
            self.assertEqual(live.order_id_from_response(order), "id-1")
        self.assertEqual(live.order_id_from_response(None), "")
        safe = live.safe_order(order)
        self.assertEqual(safe["id"], "id-1")
        self.assertEqual(safe["positionSide"], "LONG")
        self.assertNotIn("secret", safe)
        self.assertEqual(live.avg_price(order, 50.0), 51.5)
        self.assertEqual(live.avg_price({"average": "nan", "price": 0}, 50.0), 50.0)
        self.assertEqual(live.order_fee_usdt({"fee": {"cost": "0.1"}, "fees": [{"cost": "0.1"}]}), 0.1)

    def test_control_files_report_stop_and_kill(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "STOP_NEW_ORDERS").write_text("", encoding="utf-8")
            Path(td, "KILL").write_text("", encoding="utf-8")
            Path(td, "HOT_STOP").write_text("", encoding="utf-8")
            state = live.control_state(make_args(control_dir=td))
        self.assertTrue(state["stop_new_orders"])
        self.assertTrue(state["kill"])
        self.assertTrue(state["hot_stop"])
        self.assertTrue(state["stop_new_orders_path"].endswith("STOP_NEW_ORDERS"))
        self.assertTrue(state["hot_stop_path"].endswith("HOT_STOP"))

    def test_load_env_file_sets_missing_values_without_overwriting(self):
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text("export HYPE_TEST_KEY='abc def'\nHYPE_TEST_SECRET=shh\n", encoding="utf-8")
            old_key = os.environ.pop("HYPE_TEST_KEY", None)
            old_secret = os.environ.pop("HYPE_TEST_SECRET", None)
            try:
                loaded = live.load_env_file(str(env_path))
                self.assertEqual(os.environ["HYPE_TEST_KEY"], "abc def")
                self.assertEqual(os.environ["HYPE_TEST_SECRET"], "shh")
                self.assertEqual(loaded, {"HYPE_TEST_KEY": True, "HYPE_TEST_SECRET": True})
            finally:
                if old_key is not None:
                    os.environ["HYPE_TEST_KEY"] = old_key
                else:
                    os.environ.pop("HYPE_TEST_KEY", None)
                if old_secret is not None:
                    os.environ["HYPE_TEST_SECRET"] = old_secret
                else:
                    os.environ.pop("HYPE_TEST_SECRET", None)

    def test_live_client_and_auth_probe_use_injected_or_fake_client(self):
        args = make_args()
        fake = FakeClient()
        args._live_client = fake
        self.assertIs(live.live_client(args), fake)
        report = live.auth_probe(args)
        self.assertTrue(report["credentials_present"])
        self.assertTrue(report["fetch_balance_ok"])
        self.assertIn("USDT", report["balance_keys"])

    def test_gateio_auth_probe_uses_swap_balance_params(self):
        args = make_args(live_exchange="gateio")
        fake_exchange = FakeExchange()
        args._live_client = FakeClient(fake_exchange)
        report = live.auth_probe(args)
        self.assertTrue(report["fetch_balance_ok"])
        self.assertEqual(report["balance_params"], {"type": "swap", "settle": "USDT"})
        self.assertEqual(fake_exchange.balance_calls, [{"type": "swap", "settle": "USDT"}])

    def test_live_exchange_profile_applies_bingx_legacy_defaults_without_env_read(self):
        args = argparse.Namespace(
            live_exchange_profile="bingx_legacy",
            live_exchange=None,
            live_symbol=None,
            position_mode=None,
            env_file=None,
        )
        with patch.object(live, "load_env_file") as load_env:
            meta = live.apply_live_exchange_profile(args)
        self.assertFalse(load_env.called)
        self.assertEqual(meta["profile"], "bingx_legacy")
        self.assertEqual(args.live_exchange, "bingx")
        self.assertEqual(args.live_symbol, "HYPE-USDT")
        self.assertEqual(args.position_mode, "hedge")
        self.assertEqual(args.env_file, live.BINGX_LEGACY_ENV_FILE)

    def test_live_exchange_profile_preserves_explicit_cli_overrides(self):
        args = argparse.Namespace(
            live_exchange_profile="bingx_legacy",
            live_exchange="gateio",
            live_symbol="HYPE-USDT",
            position_mode="oneway",
            env_file="/tmp/explicit.env",
        )
        meta = live.apply_live_exchange_profile(args)
        self.assertEqual(meta["defaults_applied"], [])
        self.assertEqual(args.live_exchange, "gateio")
        self.assertEqual(args.position_mode, "oneway")
        self.assertEqual(args.env_file, "/tmp/explicit.env")

    def test_live_order_params_use_ccxt_hedged_flag_for_hedge_mode(self):
        params = live.live_order_params("client-1", "LONG", reduce_only=True)
        self.assertEqual(params, {"clientOrderId": "client-1", "reduceOnly": True, "positionSide": "LONG"})
        hedge_params = live.live_order_params("client-1", "LONG", reduce_only=True, position_mode="hedge")
        self.assertEqual(hedge_params, {"clientOrderId": "client-1", "reduceOnly": True, "hedged": True})
        gate_params = live.live_order_params("client-1", "LONG", reduce_only=True, exchange="gateio")
        self.assertTrue(gate_params["text"].startswith("t-hcap100-"))
        self.assertLessEqual(len(gate_params["text"]), 28)
        self.assertTrue(gate_params["reduceOnly"])
        long_gate_params = live.live_order_params("hypecap100-" + ("a" * 18), "LONG", reduce_only=False, exchange="gateio")
        self.assertLessEqual(len(long_gate_params["text"]), 28)
        self.assertTrue(long_gate_params["text"].startswith("t-hcap100-"))

    def test_reset_exchange_failures_on_switch_keeps_positions_and_clears_exchange_errors(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "live_exchange": "bingx",
                        "open_positions": {"HYPEUSDT:LONG": {"qty": 1}},
                        "order_error_backoff": {"consecutive": 4},
                        "entry_failures": {"k": {"attempts": 4}},
                        "events": [],
                    }
                ),
                encoding="utf-8",
            )
            args = make_args(state_path=str(state_path), live_exchange="gateio")
            meta = live.reset_exchange_failures_on_switch(args)
            state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(meta["changed"])
        self.assertEqual(state["live_exchange"], "gateio")
        self.assertEqual(state["open_positions"], {"HYPEUSDT:LONG": {"qty": 1}})
        self.assertEqual(state["order_error_backoff"], {})
        self.assertEqual(state["entry_failures"], {})

    def test_gateio_live_order_amount_converts_base_qty_to_contracts(self):
        args = make_args(live_exchange="gateio")
        client = FakeClient()
        with patch.object(live, "_normalize_order_qty", return_value=2.0):
            order_amount, base_qty, contract_size = live.live_order_amount_from_base_qty(
                args, client, "HYPE/USDT:USDT", 0.105, is_close=False
            )
        self.assertEqual(order_amount, 2.0)
        self.assertEqual(contract_size, 0.1)
        self.assertAlmostEqual(base_qty, 0.2)

    def test_gateio_live_order_amount_uses_integer_contracts(self):
        args = make_args(live_exchange="gateio")
        client = FakeClient()
        with patch.object(live, "_normalize_order_qty", return_value=11.8):
            order_amount, base_qty, contract_size = live.live_order_amount_from_base_qty(
                args, client, "HYPE/USDT:USDT", 1.174, is_close=False
            )
        self.assertEqual(order_amount, 12.0)
        self.assertEqual(contract_size, 0.1)
        self.assertAlmostEqual(base_qty, 1.2)

    def test_gateio_close_order_amount_floors_to_position_contracts(self):
        args = make_args(live_exchange="gateio")
        client = FakeClient()
        with patch.object(live, "_normalize_order_qty", return_value=11.8):
            order_amount, base_qty, contract_size = live.live_order_amount_from_base_qty(
                args, client, "HYPE/USDT:USDT", 1.174, is_close=True, max_base_qty=1.1
            )
        self.assertEqual(order_amount, 11.0)
        self.assertEqual(contract_size, 0.1)
        self.assertAlmostEqual(base_qty, 1.1)

    def test_live_open_preflight_reports_exchange_lot_upsize_without_blocking(self):
        args = make_args(live_exchange="gateio")
        args._live_client = FakeClient()
        with patch.object(live, "_normalize_order_qty", return_value=1.0):
            preflight = live.live_open_order_preflight(args, "HYPEUSDT", 58.887, 3.2)
        self.assertTrue(preflight["ok"])
        self.assertAlmostEqual(preflight["requested_base_qty"], 3.2 / 58.887)
        self.assertAlmostEqual(preflight["normalized_base_qty"], 0.1)
        self.assertGreater(preflight["normalization_resize_bp"], 1000.0)

    def test_live_add_fill_submits_even_when_exchange_lot_resizes_leg(self):
        args = make_args(live_exchange="gateio")
        args._live_client = FakeClient()
        with patch.object(live, "_normalize_order_qty", return_value=1.0), patch.object(
            live, "submit_open", return_value={"ok": False, "error": "synthetic_submit_reject"}
        ) as submit_open:
            event = live.live_add_fill({}, open_trade(), now=NOW, expected_price=58.887, notional=3.2, fill_type="dca_add_1", reason="test", mark=58.8, args=args)
        self.assertEqual(event["type"], "live_entry_failed")
        self.assertEqual(event["error"], "synthetic_submit_reject")
        self.assertTrue(submit_open.called)

    def test_source_size_increase_clamps_to_available_headroom_before_submit(self):
        args = make_args(max_gross_notional_usdt=30.0, max_one_side_notional_usdt=30.0, order_post_throttle_sec=0.0)
        args._live_client = FakeClient()
        trade = {**open_trade(), "notional": 29.0, "qty": 0.58}
        state = {"open_trades": {trade["key"]: trade}}
        with patch.object(live, "submit_open", return_value={"ok": False, "error": "synthetic_submit_reject"}) as submit_open:
            event = live.live_add_fill(
                state,
                trade,
                now=NOW,
                expected_price=50.0,
                notional=1644.0,
                fill_type="source_size_increase",
                reason="source_position_amount_increased",
                mark=50.0,
                args=args,
            )
        self.assertEqual(event["type"], "live_entry_failed")
        self.assertNotEqual(event.get("reason"), "gross_notional_guard")
        self.assertAlmostEqual(event["submitted_notional"], 1.0)
        self.assertAlmostEqual(event["effective_order_notional"], 1.0)
        self.assertTrue(event["guard"]["source_size_headroom"]["clamped"])
        self.assertAlmostEqual(submit_open.call_args.args[4], 1.0)

    def test_source_size_increase_blocks_when_no_headroom_without_submit(self):
        args = make_args(max_gross_notional_usdt=30.0, max_one_side_notional_usdt=30.0, order_post_throttle_sec=0.0)
        args._live_client = FakeClient()
        trade = {**open_trade(), "notional": 30.0, "qty": 0.6}
        state = {"open_trades": {trade["key"]: trade}}
        with patch.object(live, "submit_open", return_value={"ok": False, "error": "should_not_submit"}) as submit_open:
            event = live.live_add_fill(
                state,
                trade,
                now=NOW,
                expected_price=50.0,
                notional=298.0,
                fill_type="source_size_increase",
                reason="source_position_amount_increased",
                mark=50.0,
                args=args,
            )
        self.assertEqual(event["type"], "live_entry_blocked")
        self.assertEqual(event["reason"], "source_size_headroom_exhausted")
        self.assertFalse(submit_open.called)

    def test_submit_open_normalizes_qty_fetches_fill_and_reconciles_position(self):
        args = make_args()
        args._live_client = FakeClient(FakeExchange(orders=[{"id": "open-1", "filled": 0.25, "average": 50.0}]))
        with patch.object(live, "_normalize_order_qty", return_value=0.25), patch.object(
            live, "_fetch_order_fill", return_value=(50.5, NOW, {"id": "open-1", "average": 50.5})
        ), patch.object(live, "_fetch_exchange_position", return_value={"qty": 0.25, "entry": 50.5}):
            result = live.submit_open(args, "HYPEUSDT", "LONG", 50.0, 12.5, "client-open")
        self.assertTrue(result["ok"])
        self.assertEqual(result["qty"], 0.25)
        self.assertEqual(result["requested_base_qty"], 0.25)
        self.assertEqual(result["normalized_contract_amount"], 0.25)
        self.assertEqual(result["filled_contracts"], 0.25)
        self.assertEqual(result["filled_base_qty"], 0.25)
        self.assertEqual(result["post_trade_position_qty"], 0.25)
        self.assertEqual(args._live_client.ex.calls[0][2], "buy")
        self.assertEqual(args._live_client.ex.calls[0][5]["clientOrderId"], "client-open")

    def test_source_leverage_policy_resolves_copy_and_copy_div2(self):
        trade = open_trade()
        self.assertEqual(live.parse_source_leverage("6"), 6.0)
        self.assertIsNone(live.parse_source_leverage("0"))
        self.assertEqual(live.normalize_source_margin_mode("Cross"), "cross")
        ignore = live.effective_source_leverage(make_args(), trade)
        self.assertFalse(ignore["required"])
        copy = live.effective_source_leverage(make_args(source_leverage_mode="copy"), trade)
        self.assertEqual(copy["effective_leverage"], 6.0)
        div2 = live.effective_source_leverage(make_args(source_leverage_mode="copy_div2"), trade)
        self.assertEqual(div2["effective_leverage"], 3.0)
        forced_cross = live.effective_source_leverage(make_args(source_leverage_mode="copy_div2", source_margin_mode_override="cross"), {**trade, "source_margin_mode": "isolated"})
        self.assertEqual(forced_cross["effective_leverage"], 3.0)
        self.assertEqual(forced_cross["source_margin_mode"], "cross")
        fixed = live.effective_source_leverage(make_args(source_leverage_mode="fixed", fixed_source_leverage=2.0), trade)
        self.assertEqual(fixed["effective_leverage"], 2.0)
        fixed_capped = live.effective_source_leverage(
            make_args(source_leverage_mode="fixed", fixed_source_leverage=3.0, max_source_leverage=2.0),
            trade,
        )
        self.assertEqual(fixed_capped["effective_leverage"], 2.0)

    def test_lead_margin_balance_parser_handles_localized_numbers(self):
        cases = [
            ("$1,234.56", 1234.56),
            ("1 234,56 USDT", 1234.56),
            ("1,234.56 USDT", 1234.56),
        ]
        for raw, expected in cases:
            value, reason = live.paper.parse_lead_margin_balance_usdt(raw)
            self.assertEqual(reason, "parsed")
            self.assertAlmostEqual(value, expected)

        value, reason = live.paper.parse_lead_margin_balance_usdt("--")
        self.assertIsNone(value)
        self.assertEqual(reason, "placeholder")

    def test_lead_margin_balance_extracts_from_rendered_label_without_bs4(self):
        html = (
            '<div class="bn-flex justify-between py-[8px]">'
            '<div class="t-body3">Маржинальний баланс провідного трейдера</div>'
            '<div class="bn-flex t-subtitle2 items-center gap-[4px]">24,743.09 USDT</div>'
            "</div>"
        )
        meta = live.paper.extract_lead_margin_balance_by_label(html)
        self.assertEqual(meta["reason"], "ok")
        self.assertEqual(meta["extractor"], "label_regex")
        self.assertAlmostEqual(meta["lead_margin_balance_usdt"], 24743.09)

    def test_source_margin_allocation_metadata_uses_direct_or_derived_margin(self):
        derived = live.paper.source_margin_allocation_metadata({"notional_value": "500", "leverage": "5"}, "1000")
        self.assertAlmostEqual(derived["source_position_margin_usdt"], 100.0)
        self.assertEqual(derived["source_position_margin_source"], "notional_value_div_leverage")
        self.assertAlmostEqual(derived["source_margin_fraction"], 0.1)
        self.assertEqual(derived["source_margin_fraction_reason"], "ok")

        direct = live.paper.source_margin_allocation_metadata({"raw": {"positionMargin": "25"}, "notional_value": "500", "leverage": "5"}, "1000")
        self.assertAlmostEqual(direct["source_position_margin_usdt"], 25.0)
        self.assertEqual(direct["source_position_margin_source"], "raw.positionMargin")
        self.assertAlmostEqual(direct["source_margin_fraction"], 0.025)

        missing = live.paper.source_margin_allocation_metadata({"notional_value": "500", "leverage": "5"}, None)
        self.assertIsNone(missing["source_margin_fraction"])
        self.assertEqual(missing["source_margin_fraction_reason"], "lead_margin_balance_missing")

    def test_source_size_sync_increase_scales_current_notional_without_advancing_dca(self):
        args = make_args(source_size_sync_mode="ratio", source_size_sync_interval_sec=0.0)
        state = {"equity": 30.0, "open_trades": {}}
        trade = open_trade()
        trade["source_size_measure"] = 5.0
        trade["source_position_amount_abs"] = 5.0
        state["open_trades"][trade["key"]] = trade
        source = {
            trade["key"]: {
                "symbol": "HYPEUSDT",
                "side": "LONG",
                "position_amount": 7.5,
                "notional_value": 375.0,
            }
        }

        def fake_add_fill(_state, got_trade, **kwargs):
            got_trade["notional"] = float(got_trade["notional"]) + float(kwargs["notional"])
            got_trade["qty"] = float(got_trade["qty"]) + float(kwargs["notional"]) / float(kwargs["expected_price"])
            return {"type": "live_fill", "key": got_trade["key"], "fill": {"effective_order_notional": kwargs["notional"]}}

        with patch.object(live, "live_add_fill", side_effect=fake_add_fill) as add_fill:
            events = live.apply_source_size_sync(state, state["open_trades"], source, 50.0, NOW, args)

        self.assertEqual(add_fill.call_count, 1)
        self.assertAlmostEqual(add_fill.call_args.kwargs["notional"], 5.0)
        self.assertEqual(add_fill.call_args.kwargs["fill_type"], "source_size_increase")
        self.assertEqual(trade["next_level_idx"], 0)
        self.assertAlmostEqual(trade["notional"], 15.0)
        self.assertAlmostEqual(trade["source_size_measure"], 7.5)
        self.assertAlmostEqual(trade["target_notional"], 45.0)
        self.assertAlmostEqual(trade["base_notional"], 12.6)
        self.assertGreater(trade["add_notionals"][0], 1.0)
        self.assertAlmostEqual(trade["source_box_ratio"], 1.5)
        self.assertEqual(events[0]["type"], "source_size_observed")
        self.assertEqual(events[1]["type"], "source_box_resized")
        self.assertEqual(events[2]["type"], "live_fill")

    def test_callme_avgo_margin_fraction_sets_source_box_from_follower_margin_pool(self):
        args = make_args(
            initial_equity=247.5,
            initial_target_notional=247.5,
            max_gross_notional_usdt=247.5,
            source_leverage_mode="copy_div2",
            source_size_sync_mode="ratio",
            source_size_sync_interval_sec=0.0,
        )
        trade = open_trade()
        trade.update(
            {
                "key": "AVGOUSDT:LONG",
                "symbol": "AVGOUSDT",
                "source_leverage_raw": "20",
                "source_leverage": 20.0,
                "source_margin_mode": "Cross",
                "source_margin_fraction": 376.41 / 24204.49,
            }
        )
        snapshot = {
            "lead_margin_balance_usdt": 24204.49,
            "source_position_margin_usdt": 376.41,
            "source_margin_fraction": 376.41 / 24204.49,
            "source_margin_fraction_reason": "ok",
        }

        event = live.resize_trade_source_box(trade, snapshot, NOW, args, reason="test_callme_avgo_source_box")

        expected_margin_box = 247.5 * (376.41 / 24204.49)
        expected_notional_box = expected_margin_box * 10.0
        self.assertIsNotNone(event)
        self.assertAlmostEqual(trade["source_box_target_meta"]["source_box_margin_usdt"], expected_margin_box)
        self.assertAlmostEqual(trade["target_notional"], expected_notional_box)
        self.assertLess(trade["target_notional"], args.max_gross_notional_usdt)
        self.assertAlmostEqual(trade["base_notional"], expected_notional_box * 0.28)

    def test_v21_box_default_and_symbol_override_are_resolved_from_live_config(self):
        args = make_args()
        args._live_config = {
            "sizing": {
                "box_config_class": "V21StrictTrendStableBoxConfig",
                "base_order_policy": "callme_pooled_public_history_v21_same_max_plain_ignore",
                "dca_profile": "v21_same_max_dca0",
                "selected_dca_count": 0,
            },
            "callme_meta_symbols": {
                "BTCUSDT": {
                    "strategy_override": {
                        "override_fields": {
                            "sizing": {
                                "base_order_policy": "callme_symbol_public_history_v21_same_max_btcusdt_dca2_ignore",
                                "dca_profile": "v21_same_max_dca2",
                                "selected_dca_count": 2,
                            }
                        }
                    }
                }
            },
        }

        default_sizing, default_source = live.copy_signal_meta.strategy_sizing_for_symbol(args, "ETHUSDT")
        btc_sizing, btc_source = live.copy_signal_meta.strategy_sizing_for_symbol(args, "BTCUSDT")
        default_plan = live.copy_signal_meta.dca.build_plan_for_target(54.0, 100.0, side="SHORT", sizing=default_sizing)
        btc_plan = live.copy_signal_meta.dca.build_plan_for_target(54.0, 100.0, side="SHORT", sizing=btc_sizing)

        self.assertEqual(default_source, "default_symbol_config")
        self.assertEqual(default_plan["box_config_class"], "V21StrictTrendStableBoxConfig")
        self.assertEqual(default_plan["selected_dca_count"], 0)
        self.assertEqual(default_plan["base_notional"], 54.0)
        self.assertEqual(default_plan["add_notionals"], [])
        self.assertEqual(btc_source, "symbol_override_sparse")
        self.assertEqual(btc_plan["selected_dca_count"], 2)
        self.assertEqual(len(btc_plan["add_notionals"]), 2)
        self.assertGreater(btc_plan["levels"][0], 100.0)
        self.assertAlmostEqual(btc_plan["base_notional"] + sum(btc_plan["add_notionals"]), 54.0)

    def test_source_box_guard_args_expands_static_caps_to_active_box(self):
        args = make_args(max_gross_notional_usdt=30.0, max_one_side_notional_usdt=30.0)
        trade = open_trade()
        trade["source_box_current_target_notional"] = 45.0
        state = {"open_trades": {trade["key"]: trade}}

        guard_args, meta = live.source_box_guard_args(state, trade, args)

        self.assertTrue(meta["changed"])
        self.assertAlmostEqual(guard_args.max_gross_notional_usdt, 45.0 * live.SOURCE_BOX_GUARD_HEADROOM)
        self.assertAlmostEqual(guard_args.max_one_side_notional_usdt, 45.0 * live.SOURCE_BOX_GUARD_HEADROOM)
        self.assertEqual(args.max_gross_notional_usdt, 30.0)

    def test_source_size_sync_reduce_is_partial_and_keeps_trade_open(self):
        args = make_args(source_size_sync_mode="ratio", source_size_sync_interval_sec=0.0)
        state = {"equity": 30.0, "open_trades": {}}
        trade = open_trade()
        trade.update({"source_size_measure": 10.0, "source_position_amount_abs": 10.0, "fees_paid": 0.10})
        state["open_trades"][trade["key"]] = trade
        source = {
            trade["key"]: {
                "symbol": "HYPEUSDT",
                "side": "LONG",
                "position_amount": 5.0,
                "notional_value": 250.0,
            }
        }
        submitted = {
            "ok": True,
            "order": {"id": "close-1", "average": 55.0},
            "qty": 0.1,
            "fill_price": 55.0,
            "fill_dt": NOW.isoformat(),
            "ccxt_symbol": "HYPE/USDT:USDT",
            "exchange_order_id": "close-1",
            "exchange_position_after": {"qty": 0.1, "entry": 50.0},
        }

        with patch.object(live, "submit_close", return_value=submitted) as submit_close:
            events = live.apply_source_size_sync(state, state["open_trades"], source, 55.0, NOW, args)

        self.assertEqual(submit_close.call_count, 1)
        self.assertAlmostEqual(submit_close.call_args.args[3], 0.0909090909)
        self.assertAlmostEqual(trade["qty"], 0.1)
        self.assertAlmostEqual(trade["notional"], 5.0)
        self.assertAlmostEqual(trade["source_size_measure"], 5.0)
        self.assertGreater(state["equity"], 30.0)
        self.assertIn(trade["key"], state["open_trades"])
        self.assertEqual(events[0]["type"], "source_size_observed")
        self.assertEqual(events[1]["type"], "source_box_resized")
        self.assertEqual(events[2]["type"], "live_source_size_reduce")

    def test_callme_amd_partial_close_25pct_follows_source_size_and_margin_fraction(self):
        args = make_args(
            symbol="*",
            live_symbol="AMD/USDT:USDT",
            source_size_sync_mode="ratio",
            source_size_sync_interval_sec=0.0,
            live_exchange="gateio",
            position_mode="oneway",
        )
        state = {"equity": 253.8, "open_trades": {}}
        trade = open_trade()
        trade.update(
            {
                "key": "AMDUSDT:LONG",
                "lead_position_id": "callme-amd-live",
                "symbol": "AMDUSDT",
                "side": "LONG",
                "lead_entry_price": 508.26429,
                "avg_entry": 60.0,
                "qty": 2.0,
                "notional": 120.0,
                "fees_paid": 0.12,
                "source_size_measure": 60.12,
                "source_position_amount_abs": 60.12,
                "source_notional_value_abs": 31141.48,
                "lead_margin_balance_usdt": 24204.49,
                "source_position_margin_usdt": 15548.93,
                "source_position_margin_source": "page_margin",
                "source_margin_fraction": 15548.93 / 24204.49,
                "source_margin_fraction_reason": "ok",
            }
        )
        state["open_trades"][trade["key"]] = trade
        source_margin_after = 11661.70
        source = {
            trade["key"]: {
                "symbol": "AMDUSDT",
                "side": "LONG",
                "position_amount": 45.09,
                "notional_value": 23323.40,
                "leverage": "2",
                "lead_margin_balance_usdt": 24204.49,
                "source_position_margin_usdt": source_margin_after,
                "source_position_margin_source": "page_margin",
                "source_margin_fraction": source_margin_after / 24204.49,
                "source_margin_fraction_reason": "ok",
            }
        }
        submitted = {
            "ok": True,
            "order": {"id": "amd-partial-close-25pct", "average": 60.0},
            "qty": 0.5,
            "fill_price": 60.0,
            "fill_dt": NOW.isoformat(),
            "ccxt_symbol": "AMD/USDT:USDT",
            "exchange_order_id": "amd-partial-close-25pct",
            "exchange_position_after": {"qty": 1.5, "entry": 60.0},
        }

        with patch.object(live, "submit_close", return_value=submitted) as submit_close:
            events = live.apply_source_size_sync(state, state["open_trades"], source, 60.0, NOW, args)

        self.assertEqual(submit_close.call_count, 1)
        self.assertAlmostEqual(submit_close.call_args.args[3], 0.5)
        self.assertAlmostEqual(events[0]["ratio"], 0.75)
        self.assertAlmostEqual(events[0]["delta_notional"], -30.0)
        self.assertEqual(events[1]["type"], "source_box_resized")
        self.assertEqual(events[2]["type"], "live_source_size_reduce")
        self.assertAlmostEqual(events[2]["fill"]["requested_reduce_notional"], 30.0)
        self.assertAlmostEqual(trade["qty"], 1.5)
        self.assertAlmostEqual(trade["notional"], 90.0)
        self.assertEqual(trade["next_level_idx"], 0)
        self.assertIn(trade["key"], state["open_trades"])
        self.assertAlmostEqual(trade["source_size_measure"], 45.09)
        self.assertAlmostEqual(trade["source_position_margin_usdt"], source_margin_after)
        self.assertAlmostEqual(trade["source_margin_fraction"], source_margin_after / 24204.49)
        self.assertEqual(trade["source_margin_fraction_reason"], "ok")

    def test_ensure_symbol_leverage_bingx_hedge_is_side_specific_and_cached(self):
        args = make_args(live_exchange="bingx", position_mode="hedge")
        args._live_client = FakeClient()
        state = {}
        first = live.ensure_symbol_leverage(args, state, symbol="HYPEUSDT", side="LONG", margin_mode="Cross", leverage=3.0, now=NOW)
        second = live.ensure_symbol_leverage(args, state, symbol="HYPEUSDT", side="LONG", margin_mode="Cross", leverage=3.0, now=NOW)
        self.assertTrue(first["ok"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(args._live_client.ex.leverage_calls), 1)
        self.assertEqual(args._live_client.ex.leverage_calls[0][0], 3.0)
        self.assertEqual(args._live_client.ex.leverage_calls[0][2]["side"], "LONG")
        self.assertEqual(args._live_client.ex.leverage_calls[0][2]["marginMode"], "cross")

    def test_ensure_symbol_leverage_gateio_uses_settle_without_position_side(self):
        args = make_args(live_exchange="gateio", position_mode="oneway")
        args._live_client = FakeClient()
        result = live.ensure_symbol_leverage(args, {}, symbol="HYPEUSDT", side="LONG", margin_mode="isolated", leverage=2.0, now=NOW)
        self.assertTrue(result["ok"])
        params = args._live_client.ex.leverage_calls[0][2]
        self.assertEqual(params["settle"], "usdt")
        self.assertEqual(params["marginMode"], "isolated")
        self.assertNotIn("positionSide", params)

    def test_live_add_fill_blocks_when_required_leverage_setup_fails(self):
        args = make_args(source_leverage_mode="copy")
        args._live_client = FakeClient(FakeExchange(failures=["set leverage rejected"]))
        event = live.live_add_fill({}, open_trade(), now=NOW, expected_price=50.0, notional=10.0, fill_type="base_entry", reason="test", mark=50.0, args=args)
        self.assertEqual(event["type"], "live_entry_blocked")
        self.assertEqual(event["reason"], "leverage_setup_failed")
        self.assertEqual(args._live_client.ex.calls, [])

    def test_submit_open_retries_without_position_side_for_one_way_error(self):
        args = make_args()
        args._live_client = FakeClient(FakeExchange(orders=[{"id": "open-2", "filled": 0.2}], failures=["one-way mode rejects positionSide"]))
        with patch.object(live, "_normalize_order_qty", return_value=0.2), patch.object(
            live, "_fetch_order_fill", return_value=(50.0, NOW, {"id": "open-2", "average": 50.0})
        ), patch.object(live, "_fetch_exchange_position", return_value={"qty": 0.2, "entry": 50.0}):
            result = live.submit_open(args, "HYPEUSDT", "LONG", 50.0, 10.0, "client-open")
        self.assertTrue(result["ok"])
        self.assertEqual(len(args._live_client.ex.calls), 2)
        self.assertNotIn("positionSide", args._live_client.ex.calls[1][5])

    def test_submit_close_uses_reduce_only_and_ccxt_hedged_flag_for_hedge_close(self):
        args = make_args(position_mode="hedge")
        args._live_client = FakeClient(FakeExchange(orders=[{"id": "close-1", "filled": 0.2, "average": 52.0}]))
        with patch.object(live, "_fetch_exchange_position", side_effect=[{"qty": 0.2, "entry": 50.0}, None]), patch.object(
            live, "_normalize_order_qty", return_value=0.2
        ), patch.object(live, "_fetch_order_fill", return_value=(52.0, NOW, {"id": "close-1", "average": 52.0})):
            result = live.submit_close(args, "HYPEUSDT", "LONG", 0.5, "client-close")
        self.assertTrue(result["ok"])
        self.assertEqual(result["requested_base_qty"], 0.2)
        self.assertEqual(result["normalized_contract_amount"], 0.2)
        self.assertEqual(result["filled_contracts"], 0.2)
        self.assertEqual(result["filled_base_qty"], 0.2)
        self.assertEqual(result["post_trade_position_qty"], 0.0)
        self.assertEqual(args._live_client.ex.calls[0][2], "sell")
        self.assertTrue(args._live_client.ex.calls[0][5]["reduceOnly"])
        self.assertTrue(args._live_client.ex.calls[0][5]["hedged"])
        self.assertNotIn("positionSide", args._live_client.ex.calls[0][5])

    def test_submit_close_syncs_when_exchange_position_is_absent(self):
        args = make_args()
        args._live_client = FakeClient()
        with patch.object(live, "_fetch_exchange_position", return_value=None):
            result = live.submit_close(args, "HYPEUSDT", "LONG", 0.2, "client-close")
        self.assertTrue(result["synced_only"])
        self.assertEqual(result["reason"], "exchange_no_position_before_close")
        self.assertEqual(args._live_client.ex.calls, [])

    def test_dca_eval_due_separates_one_second_copy_poll_from_sixty_second_dca(self):
        args = make_args(interval_sec=1.0, dca_eval_interval_sec=60.0)
        due, meta = live.dca_eval_due({}, datetime(2026, 5, 25, 21, 1, 0, tzinfo=timezone.utc), args)
        self.assertTrue(due)
        state = {"last_dca_eval_bucket": meta["dca_eval_bucket"]}
        due_again, _ = live.dca_eval_due(state, datetime(2026, 5, 25, 21, 1, 1, tzinfo=timezone.utc), args)
        self.assertFalse(due_again)
        due_later, _ = live.dca_eval_due(state, datetime(2026, 5, 25, 21, 2, 0, tzinfo=timezone.utc), args)
        self.assertTrue(due_later)

    def test_load_inputs_live_uses_stale_public_endpoint_cache_and_skips_history_between_polls(self):
        args = make_args(history_poll_interval_sec=60.0)
        state = {
            "cached_positions": [lead_long()],
            "last_positions_poll_utc": "old",
            "cached_mark": 49.5,
            "last_mark_poll_utc": "old",
            "cached_history": [{"id": "hist"}],
            "last_history_poll_ts": NOW.timestamp(),
            "last_history_poll_utc": "old",
        }
        lead_page = {"lead_margin_balance_usdt": None, "reason": "lead_margin_balance_missing"}
        with patch.object(live.paper, "fetch_open_positions", side_effect=RuntimeError("positions down")), patch.object(
            live.paper, "fetch_mark", side_effect=RuntimeError("mark down")
        ), patch.object(live.paper, "fetch_lead_margin_balance", return_value=lead_page), patch.object(live.paper, "fetch_position_history") as fetch_history:
            positions, history, mark, meta = live.load_inputs_live(args, state, NOW)
        self.assertEqual(positions[0]["key"], lead_long()["key"])
        self.assertEqual(positions[0]["source_margin_fraction_reason"], "lead_margin_balance_missing")
        self.assertEqual(history, [{"id": "hist"}])
        self.assertEqual(mark, 49.5)
        self.assertFalse(fetch_history.called)
        self.assertEqual(meta["positions"]["cached_rows"], 1)
        self.assertEqual(meta["lead_page"], lead_page)
        self.assertTrue(meta["history"]["skipped"])
        self.assertEqual(meta["market"]["cached_mark"], 49.5)

    def test_sleep_until_next_poll_aligns_to_interval(self):
        with patch.object(live.time, "time", return_value=10.2), patch.object(live.time, "sleep") as sleep:
            live.sleep_until_next_poll(1.0)
        self.assertAlmostEqual(sleep.call_args[0][0], 0.8)

    def test_sync_trade_from_exchange_reconciles_qty_entry_and_notional(self):
        args = make_args()
        args._live_client = FakeClient()
        trade = open_trade()
        with patch.object(live, "_fetch_exchange_position", return_value={"qty": 0.3, "entry": 51.0}):
            meta = live.sync_trade_from_exchange(args, {}, trade)
        self.assertTrue(meta["synced"])
        self.assertEqual(trade["qty"], 0.3)
        self.assertEqual(trade["avg_entry"], 51.0)
        self.assertEqual(trade["notional"], 15.299999999999999)

    def test_multi_symbol_filter_accepts_all_symbols(self):
        rows = [lead_long(), {**lead_long(), "key": "AVGOUSDT:LONG", "symbol": "AVGOUSDT", "id": "lead-2"}]
        current, events = live.copy_signal_meta.filter_source_positions(rows, symbol="*", long_only=True)
        self.assertEqual(set(current), {"HYPEUSDT:LONG", "AVGOUSDT:LONG"})
        self.assertEqual(events, [])

    def test_multi_symbol_live_resolver_does_not_fallback_to_default_symbol(self):
        args = make_args(symbol="*", live_symbol="HYPE-USDT")
        args._live_config = {"_meta_strategy": "callme_meta_strategy_live"}
        args._live_client = StrictFakeClient({"HYPE-USDT": "HYPE/USDT:USDT"})
        preflight = live.live_open_order_preflight(args, "AVGOUSDT", expected_price=600.0, notional=10.0)
        self.assertFalse(preflight["ok"])
        self.assertEqual(preflight["reason"], "exchange_symbol_unsupported")
        self.assertEqual(preflight["exchange_universe_policy"]["matched_symbol"], "AVGOUSDT")

    def test_bingx_exchange_universe_marks_amd_unsupported(self):
        args = make_args(symbol="*", live_symbol="HYPE-USDT", live_exchange="bingx")
        args._live_client = FakeClient()
        preflight = live.live_open_order_preflight(args, "AMDUSDT", expected_price=180.0, notional=10.0)
        self.assertFalse(preflight["ok"])
        self.assertEqual(preflight["reason"], "exchange_symbol_unsupported")
        self.assertIn("AMDUSDT", preflight["exchange_universe_policy"]["unsupported_symbols"])

    def test_callme_meta_config_uses_exchange_allocation_override(self):
        args = make_args(live_exchange="gateio")
        cfg = {
            "schema": "callme_meta_strategy_config_v1",
            "name": "callme_meta_strategy_live",
            "lead": {"portfolio_id": "p1"},
            "allocation": {"default_exchange_margin_usdt": 54.0, "default_max_notional_usdt": 54.0},
            "default_symbol_config": {"safety": {}, "sizing": {}, "source_leverage": {}, "protection": {}},
            "exchanges": {
                "gateio": {
                    "enabled": True,
                    "exchange_profile": "gateio_current",
                    "position_mode": "oneway",
                    "allocation": {
                        "initial_equity_usdt": 253.8,
                        "initial_target_notional_usdt": 253.8,
                        "max_notional_usdt": 253.8,
                        "max_one_side_notional_usdt": 253.8,
                    },
                }
            },
            "symbols": {},
        }
        expanded = live.expand_callme_meta_live_config(args, cfg)
        self.assertEqual(expanded["allocation"]["initial_equity_usdt"], 253.8)
        self.assertEqual(expanded["allocation"]["max_notional_usdt"], 253.8)

    def test_sync_trade_from_exchange_updates_session_open_position(self):
        with tempfile.TemporaryDirectory() as td:
            session_db = str(Path(td) / "session.sqlite")
            live.ensure_session_dbs(td, session_db)
            args = make_args(out_dir=td, session_db=session_db)
            args._live_client = FakeClient()
            trade = open_trade()
            with patch.object(live, "_fetch_exchange_position", return_value={"qty": 0.3, "entry": 51.0}), patch.object(live.paper, "utc_now", return_value=NOW):
                meta = live.sync_trade_from_exchange(args, {}, trade)
            self.assertTrue(meta["synced"])
            con = sqlite3.connect(session_db)
            try:
                row = con.execute("SELECT qty, entry, status FROM open_positions WHERE status='OPEN'").fetchone()
            finally:
                con.close()
            self.assertEqual(row, (0.3, 51.0, "OPEN"))

    def test_sync_trade_from_exchange_applies_fixed_leverage_to_existing_open_position(self):
        args = make_args(source_leverage_mode="fixed", fixed_source_leverage=2.0, source_margin_mode_override="isolated", live_exchange="gateio")
        args._live_client = FakeClient()
        trade = open_trade()
        state = {}
        with patch.object(live, "_fetch_exchange_position", return_value={"qty": 0.3, "entry": 51.0}), patch.object(live.paper, "utc_now", return_value=NOW):
            meta = live.sync_trade_from_exchange(args, state, trade)
        self.assertTrue(meta["synced"])
        self.assertTrue(meta["leverage_setup"]["ok"])
        self.assertEqual(args._live_client.ex.leverage_calls[0][0], 2)
        self.assertEqual(args._live_client.ex.leverage_calls[0][2]["marginMode"], "isolated")
        self.assertEqual(state["last_leverage_setup"]["leverage"], 2.0)

    def test_live_add_fill_blocks_on_stop_file_without_order(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "STOP_NEW_ORDERS").write_text("", encoding="utf-8")
            args = make_args(control_dir=td)
            with patch.object(live, "submit_open") as submit_open:
                event = live.live_add_fill({}, open_trade(), now=NOW, expected_price=50.0, notional=10.0, fill_type="base", reason="test", mark=50.0, args=args)
        self.assertEqual(event["type"], "live_entry_blocked")
        self.assertEqual(event["reason"], "stop_new_orders_file")
        self.assertFalse(submit_open.called)

    def test_live_protection_blocks_entry_without_order(self):
        args = make_args()
        args._last_protection = {"block_new_entries": True, "reasons": [{"code": "floating_pnl_stop"}]}
        with patch.object(live, "submit_open") as submit_open:
            event = live.live_add_fill({}, open_trade(), now=NOW, expected_price=50.0, notional=10.0, fill_type="base", reason="test", mark=50.0, args=args)
        self.assertEqual(event["type"], "live_entry_blocked")
        self.assertEqual(event["reason"], "protection_block_new_entries")
        self.assertFalse(submit_open.called)

    def test_live_add_fill_records_successful_live_fill(self):
        args = make_args()
        state = {"open_trades": {}}
        trade = open_trade()
        submitted = {"ok": True, "order": {"id": "open-1", "average": 50.0, "filled": 0.2}, "qty": 0.2, "entry": 50.0, "ccxt_symbol": "HYPE/USDT:USDT", "exchange_order_id": "open-1"}
        with patch.object(live, "submit_open", return_value=submitted), patch.object(live, "record_session_order") as record, patch.object(
            live, "upsert_session_position"
        ) as upsert:
            event = live.live_add_fill(state, trade, now=NOW, expected_price=50.0, notional=10.0, fill_type="base", reason="test", mark=50.0, args=args)
        self.assertEqual(event["type"], "live_fill")
        self.assertEqual(trade["qty"], 0.4)
        self.assertEqual(state["paper_orders"][0]["client_order_id"], live.stable_client_order_id("entry", args.run_id, trade["key"], trade["lead_position_id"], trade["opened_at_utc"], "base", 0))
        self.assertIn("requested_base_qty", state["paper_orders"][0])
        self.assertIn("normalized_contract_amount", state["paper_orders"][0])
        self.assertIn("filled_base_qty", state["paper_orders"][0])
        self.assertTrue(record.called)
        self.assertTrue(upsert.called)

    def test_live_add_fill_arms_backoff_and_entry_cooldown_after_rejected_entry(self):
        args = make_args(order_post_throttle_sec=0.0)
        state = {"open_trades": {}}
        trade = open_trade()
        with patch.object(live, "submit_open", return_value={"ok": False, "error": 'bingx {"code":109429,"msg":"temporary restricted"}'}) as submit_open, patch.object(
            live, "record_session_order"
        ):
            first = live.live_add_fill(state, trade, now=NOW, expected_price=50.0, notional=10.0, fill_type="base", reason="test", mark=50.0, args=args)
            second = live.live_add_fill(state, trade, now=NOW, expected_price=50.0, notional=10.0, fill_type="base", reason="test", mark=50.0, args=args)
        self.assertEqual(first["type"], "live_entry_failed")
        self.assertIn(first["reason"], {"test"})
        self.assertEqual(second["type"], "live_entry_blocked")
        self.assertIn(second["reason"], {"order_error_backoff", "order_error_circuit_breaker"})
        self.assertEqual(submit_open.call_count, 1)
        self.assertIn("order_error_backoff", state)
        self.assertIn("entry_failures", state)
        self.assertEqual(state["order_attempts"]["hourly"][NOW.strftime("%Y%m%dT%H")], 1)
        self.assertIn("delay_sec", state["order_error_backoff"])
        self.assertIn("jitter_sec", state["order_error_backoff"])

    def test_live_add_fill_blocks_when_hourly_order_attempt_cap_reached(self):
        args = make_args(max_order_attempts_per_hour=1, order_post_throttle_sec=0.0)
        state = {"open_trades": {}}
        trade = open_trade()
        with patch.object(live, "submit_open", return_value={"ok": False, "error": "temporary restricted"}), patch.object(live, "record_session_order"):
            first = live.live_add_fill(state, trade, now=NOW, expected_price=50.0, notional=10.0, fill_type="base", reason="test", mark=50.0, args=args)
        state.pop("order_error_backoff", None)
        state.pop("entry_failures", None)
        with patch.object(live, "submit_open") as submit_open:
            second = live.live_add_fill(state, trade, now=NOW, expected_price=50.0, notional=10.0, fill_type="base2", reason="test", mark=50.0, args=args)
        self.assertEqual(first["type"], "live_entry_failed")
        self.assertEqual(second["type"], "live_entry_blocked")
        self.assertEqual(second["reason"], "max_order_attempts_per_hour")
        submit_open.assert_not_called()

    def test_live_add_fill_respects_post_order_throttle(self):
        args = make_args(order_post_throttle_sec=10.0)
        state = {"open_trades": {}}
        trade = open_trade()
        live.register_order_post_attempt(state, NOW, args, action="OPEN", symbol="HYPEUSDT", side="LONG")
        with patch.object(live, "submit_open") as submit_open:
            event = live.live_add_fill(state, trade, now=NOW, expected_price=50.0, notional=10.0, fill_type="base", reason="test", mark=50.0, args=args)
        self.assertEqual(event["type"], "live_entry_blocked")
        self.assertEqual(event["reason"], "order_post_throttle")
        submit_open.assert_not_called()

    def test_live_add_fill_records_incremental_order_qty_but_reconciles_cumulative_position_qty(self):
        with tempfile.TemporaryDirectory() as td:
            session_db = str(Path(td) / "session.sqlite")
            live.ensure_session_dbs(td, session_db)
            live.ensure_orders_db(session_db)
            args = make_args(out_dir=td, session_db=session_db)
            state = {"open_trades": {}}
            trade = open_trade()
            trade["qty"] = 0.1404
            trade["notional"] = 8.43102
            trade["avg_entry"] = 60.05
            submitted = {
                "ok": True,
                "order": {"id": "open-2", "average": 59.746, "filled": 0.0536, "fee": {"cost": "0.001601", "currency": "USDT"}},
                "order_qty": 0.0536,
                "qty": 0.194,
                "entry": 59.966,
                "fill_price": 59.746,
                "fill_dt": live.paper.iso(NOW),
                "ccxt_symbol": "HYPE/USDT:USDT",
                "exchange_order_id": "open-2",
                "exchange_position": {"qty": 0.194, "entry": 59.966},
            }
            with patch.object(live, "submit_open", return_value=submitted):
                event = live.live_add_fill(state, trade, now=NOW, expected_price=59.720325, notional=3.2, fill_type="dca_add_1", reason="test", mark=59.7, args=args)
            self.assertEqual(event["type"], "live_fill")
            self.assertEqual(trade["qty"], 0.194)
            self.assertEqual(trade["avg_entry"], 59.966)
            self.assertAlmostEqual(state["paper_orders"][0]["qty"], 0.0536)
            self.assertAlmostEqual(state["paper_orders"][0]["fee_usdt"], 0.001601)
            con = sqlite3.connect(session_db)
            try:
                row = con.execute("SELECT qty, entry, entry_slip_bp FROM open_positions WHERE status='OPEN'").fetchone()
                order_row = con.execute("SELECT qty, price FROM orders WHERE status='FILLED'").fetchone()
            finally:
                con.close()
            self.assertEqual(row[0], 0.194)
            self.assertEqual(row[1], 59.966)
            self.assertIsNotNone(row[2])
            self.assertEqual(order_row, (0.0536, 59.746))

    def test_live_close_trade_handles_filled_failed_and_synced_closes(self):
        args = make_args()
        trade = open_trade()
        with patch.object(live, "submit_close", return_value={"ok": True, "order": {"id": "close-1", "average": 52.0, "fee": {"cost": "0.01", "currency": "USDT"}}, "qty": 0.2, "fill_price": 52.0, "fill_dt": live.paper.iso(NOW), "exchange_order_id": "close-1"}), patch.object(
            live, "record_session_order"
        ), patch.object(live, "upsert_session_position"):
            closed, event = live.live_close_trade({}, trade, now=NOW, expected_exit=52.0, mark=52.0, reason="test_close", history_row=None, args=args)
        self.assertEqual(event["type"], "live_exit")
        self.assertFalse(closed["paper_only"])
        self.assertAlmostEqual(closed["paper_exit_price"], 52.0)
        self.assertAlmostEqual(closed["exit_fee"], 0.01)
        self.assertAlmostEqual(closed["paper_pnl_usdt"], 0.39)
        self.assertAlmostEqual(closed["exit_slip_bp"], 0.0)
        self.assertAlmostEqual(live.signed_slip_bp("LONG", 58.245, 58.245, is_close=True), 0.0)
        self.assertAlmostEqual(live.signed_slip_bp("LONG", 58.245, 58.22956, is_close=True), (58.245 - 58.22956) / 58.245 * 10000.0)
        with patch.object(live, "submit_close", return_value={"ok": False, "error": "rejected"}), patch.object(live, "record_session_order"), patch.object(
            live, "upsert_session_position"
        ):
            closed, event = live.live_close_trade({}, trade, now=NOW, expected_exit=52.0, mark=52.0, reason="test_close", history_row=None, args=args)
        self.assertIsNone(closed)
        self.assertEqual(event["type"], "live_exit_failed")
        with patch.object(live, "submit_close", return_value={"ok": True, "synced_only": True, "reason": "exchange_no_position_before_close"}), patch.object(
            live, "record_session_order"
        ), patch.object(live, "upsert_session_position"):
            closed, event = live.live_close_trade({}, trade, now=NOW, expected_exit=52.0, mark=52.0, reason="test_close", history_row=None, args=args)
        self.assertEqual(event["type"], "live_exit_synced")
        self.assertTrue(closed["live_exit_synced_only"])

    def test_stale_source_history_is_not_attached_to_closed_trade_or_slip(self):
        with tempfile.TemporaryDirectory() as td:
            session_db = str(Path(td) / "session.sqlite")
            live.ensure_session_dbs(td, session_db)
            live.ensure_orders_db(session_db)
            args = make_args(out_dir=td, session_db=session_db, run_id="run-history")
            trade = open_trade()
            trade["detected_at_ms"] = int(NOW.timestamp() * 1000)
            state = {"open_trades": {trade["key"]: trade}, "equity": 30.0}
            stale_history = [
                {
                    "id": trade["lead_position_id"],
                    "key": trade["key"],
                    "opened_ms": int((NOW.timestamp() - 7 * 3600) * 1000),
                    "opened_utc": "2026-05-25T14:00:00+00:00",
                    "closed_ms": int(NOW.timestamp() * 1000),
                    "closed_utc": live.paper.iso(NOW),
                    "avg_cost": 59.5773,
                    "avg_close_price": 60.352,
                }
            ]
            with patch.object(
                live,
                "submit_close",
                return_value={
                    "ok": True,
                    "order": {"id": "close-stale", "average": 58.245},
                    "qty": trade["qty"],
                    "fill_price": 58.245,
                    "fill_dt": live.paper.iso(NOW),
                    "exchange_order_id": "close-stale",
                },
            ):
                events = live.apply_live_snapshot(state, [], stale_history, 58.245, NOW, args, allow_dca=False)

            self.assertEqual(events[0]["type"], "live_exit")
            closed = state["closed_trades"][0]
            self.assertIsNone(closed["history_exit"])
            self.assertEqual(closed["exit_reason"], "lead_position_disappeared_mark_fallback")
            self.assertAlmostEqual(closed["exit_expected_price"], 58.245)
            self.assertAlmostEqual(closed["exit_slip_bp"], 0.0)
            con = sqlite3.connect(session_db)
            try:
                row = con.execute(
                    "SELECT source_history_valid, source_history_reject_reason, source_avg_close, exchange_fill_price, signal_price, exchange_vs_signal_bp FROM order_execution_comparisons WHERE action='CLOSE'"
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(row[0], 0)
            self.assertIn(row[1], {"missing_history", "no_valid_history_match"})
            self.assertIsNone(row[2])
            self.assertAlmostEqual(row[3], 58.245)
            self.assertAlmostEqual(row[4], 58.245)
            self.assertAlmostEqual(row[5], 0.0)

    def test_valid_source_history_attaches_and_records_execution_comparison(self):
        with tempfile.TemporaryDirectory() as td:
            session_db = str(Path(td) / "session.sqlite")
            live.ensure_session_dbs(td, session_db)
            live.ensure_orders_db(session_db)
            args = make_args(out_dir=td, session_db=session_db, run_id="run-history-valid")
            trade = open_trade()
            trade["detected_at_ms"] = int(NOW.timestamp() * 1000)
            state = {"open_trades": {trade["key"]: trade}, "equity": 30.0}
            valid_history = [
                {
                    "id": trade["lead_position_id"],
                    "key": trade["key"],
                    "opened_ms": int(NOW.timestamp() * 1000),
                    "opened_utc": live.paper.iso(NOW),
                    "closed_ms": int(NOW.timestamp() * 1000),
                    "closed_utc": live.paper.iso(NOW),
                    "avg_cost": 50.0,
                    "avg_close_price": 52.0,
                }
            ]
            with patch.object(
                live,
                "submit_close",
                return_value={
                    "ok": True,
                    "order": {"id": "close-valid", "average": 52.0},
                    "qty": trade["qty"],
                    "fill_price": 52.0,
                    "fill_dt": live.paper.iso(NOW),
                    "exchange_order_id": "close-valid",
                },
            ):
                live.apply_live_snapshot(state, [], valid_history, 51.5, NOW, args, allow_dca=False)

            closed = state["closed_trades"][0]
            self.assertEqual(closed["history_exit"]["avg_close_price"], 52.0)
            self.assertEqual(closed["exit_reason"], "position_history_closed")
            con = sqlite3.connect(session_db)
            try:
                row = con.execute(
                    "SELECT source_history_valid, source_avg_cost, source_avg_close, exchange_vs_signal_bp FROM order_execution_comparisons WHERE action='CLOSE'"
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(row[0], 1)
            self.assertAlmostEqual(row[1], 50.0)
            self.assertAlmostEqual(row[2], 52.0)
            self.assertAlmostEqual(row[3], 0.0)

    def test_live_close_trade_respects_order_error_backoff_before_submit(self):
        args = make_args()
        trade = open_trade()
        state = {
            "order_error_backoff": {
                "reason": "order_error_circuit_breaker",
                "until_ts": NOW.timestamp() + 60,
                "until_utc": live.paper.iso(NOW),
            }
        }
        with patch.object(live, "submit_close") as submit_close:
            closed, event = live.live_close_trade(state, trade, now=NOW, expected_exit=52.0, mark=52.0, reason="test_close", history_row=None, args=args)
        submit_close.assert_not_called()
        self.assertIsNone(closed)
        self.assertEqual(event["type"], "live_exit_blocked")
        self.assertEqual(event["reason"], "order_error_circuit_breaker")

    def test_apply_live_snapshot_respects_dca_schedule_and_reconciles_existing_position(self):
        args = make_args()
        state = {"open_trades": {"HYPEUSDT:LONG": open_trade()}, "equity": 30.0}
        with patch.object(live, "sync_trade_from_exchange", return_value={"synced": True, "qty": 0.2, "entry": 50.0}), patch.object(
            live, "live_add_fill"
        ) as add_fill:
            events = live.apply_live_snapshot(state, [lead_long()], [], 48.0, NOW, args, allow_dca=False)
        self.assertEqual(events[0]["type"], "exchange_position_synced")
        self.assertFalse(add_fill.called)
        with patch.object(live, "sync_trade_from_exchange", return_value={"synced": False}), patch.object(
            live, "live_add_fill", return_value={"type": "live_fill", "key": "HYPEUSDT:LONG"}
        ) as add_fill:
            live.apply_live_snapshot(state, [lead_long()], [], 48.0, NOW, args, allow_dca=True)
        self.assertTrue(add_fill.called)

    def test_apply_live_snapshot_closes_when_lead_position_disappears(self):
        args = make_args()
        state = {"open_trades": {"HYPEUSDT:LONG": open_trade()}, "equity": 30.0}
        with patch.object(live, "live_close_trade", return_value=({"paper_pnl_usdt": 1.25}, {"type": "live_exit", "key": "HYPEUSDT:LONG"})):
            events = live.apply_live_snapshot(state, [], [], 52.0, NOW, args, allow_dca=False)
        self.assertEqual(events[0]["type"], "live_exit")
        self.assertEqual(state["equity"], 31.25)
        self.assertEqual(state["open_trades"], {})

    def test_status_payload_marks_live_controls_and_scheduling(self):
        args = make_args()
        args._auth_probe = {"fetch_balance_ok": True}
        args._last_dca_eval_meta = {"due": True}
        payload = live.status_payload(live.paper.default_state(args), 50.0, NOW, [], {"mock": True}, args)
        self.assertFalse(payload["paper_only"])
        self.assertTrue(payload["live_order_code_present"])
        self.assertEqual(payload["live_exchange_profile"], "bingx_legacy")
        self.assertEqual(payload["copy_poll_interval_sec"], 1.0)
        self.assertEqual(payload["dca_eval_interval_sec"], 60.0)

    def test_poll_once_checks_kill_file_and_updates_dca_bucket(self):
        with tempfile.TemporaryDirectory() as td:
            args = make_args(out_dir=td)
            live.normalize_paths(args)
            Path(td, "KILL").write_text("", encoding="utf-8")
            with self.assertRaises(SystemExit):
                live.poll_once(args)
            Path(td, "KILL").unlink()
            with patch.object(live.paper, "utc_now", return_value=NOW), patch.object(
                live, "load_inputs_live", return_value=([lead_long()], [], 50.0, {"mock": True})
            ), patch.object(live, "apply_live_snapshot", return_value=[]):
                status = live.poll_once(args)
        self.assertEqual(status["dca_eval_meta"]["dca_eval_bucket"], int(NOW.timestamp() // 60))

    def test_poll_once_auto_stop_new_orders_on_floating_protection(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            args = make_args(
                out_dir=td,
                state_path=str(state_path),
                status_path=str(Path(td) / "RUN_STATUS.json"),
                telemetry_path=str(Path(td) / "telemetry.jsonl"),
                protection_floating_pnl_stop_usdt=1.0,
                protection_auto_stop_new_orders=True,
            )
            state = live.paper.default_state(args)
            trade = open_trade()
            trade["notional"] = 30.0
            state["open_trades"] = {"HYPEUSDT:LONG": trade}
            live.paper.write_json(state_path, state)
            with patch.object(live.paper, "utc_now", return_value=NOW), patch.object(
                live, "load_inputs_live", return_value=([lead_long()], [], 45.0, {"market": {"book_ok": True, "premium_ok": True}})
            ), patch.object(live, "apply_live_snapshot", return_value=[]):
                status = live.poll_once(args)
            self.assertTrue(Path(td, "STOP_NEW_ORDERS").exists())
        self.assertTrue(status["protection"]["block_new_entries"])
        self.assertEqual(status["protection"]["reasons"][0]["code"], "floating_pnl_stop")

    def test_live_config_scales_protection_percent_from_allocation_max_notional(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "live.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "exchange": "mexc",
                        "exchange_profile": "mexc_current",
                        "live_symbol": "HYPE/USDT:USDT",
                        "signal": {"portfolio_id": "p1", "copy_symbol": "HYPEUSDT"},
                        "allocation": {"initial_equity_usdt": 56.0, "max_notional_usdt": 90.0},
                        "protection": {
                            "account_loss_stop_pct_of_equity": 5.0,
                            "floating_pnl_stop_pct_of_equity": 5.0,
                            "emergency_account_loss_pct_of_equity": 10.0,
                            "stale_market_sec": 300,
                            "auto_stop_new_orders": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            args = live.build_arg_parser().parse_args(["--live-config", str(cfg_path)])
            live.apply_live_config(args)

        self.assertEqual(args.initial_equity, 56.0)
        self.assertEqual(args.max_gross_notional_usdt, 90.0)
        self.assertEqual(args.protection_account_loss_stop_usdt, 4.5)
        self.assertEqual(args.protection_floating_pnl_stop_usdt, 4.5)
        self.assertEqual(args.protection_emergency_account_loss_usdt, 9.0)
        self.assertEqual(args.protection_stale_market_sec, 300)
        self.assertTrue(args.protection_auto_stop_new_orders)

    def test_live_protection_loss_threshold_boundaries(self):
        args = make_args(
            protection_account_loss_stop_usdt=4.5,
            protection_floating_pnl_stop_usdt=4.5,
            protection_emergency_account_loss_usdt=9.0,
        )
        state = live.paper.default_state(args)

        with patch.object(live.paper, "daily_pnl_usdt", return_value=-4.49), patch.object(live, "open_unrealized_pnl_usdt", return_value=-4.49):
            protection = live.evaluate_live_protection(state, 50.0, NOW, {"market": {}}, args)
        self.assertFalse(protection["block_new_entries"])
        self.assertEqual(protection["reasons"], [])

        with patch.object(live.paper, "daily_pnl_usdt", return_value=-4.5), patch.object(live, "open_unrealized_pnl_usdt", return_value=-4.5):
            protection = live.evaluate_live_protection(state, 50.0, NOW, {"market": {}}, args)
        self.assertTrue(protection["block_new_entries"])
        self.assertFalse(protection["emergency"])
        self.assertEqual([r["code"] for r in protection["reasons"]], ["account_loss_stop", "floating_pnl_stop"])

        with patch.object(live.paper, "daily_pnl_usdt", return_value=-9.0), patch.object(live, "open_unrealized_pnl_usdt", return_value=-4.49):
            protection = live.evaluate_live_protection(state, 50.0, NOW, {"market": {}}, args)
        self.assertTrue(protection["emergency"])
        self.assertIn("emergency_account_loss", [r["code"] for r in protection["reasons"]])

    def test_live_protection_stale_market_boundary(self):
        args = make_args(protection_stale_market_sec=300.0)
        state = live.paper.default_state(args)
        state["last_mark_poll_utc"] = live.paper.iso(datetime.fromtimestamp(NOW.timestamp() - 299, tz=timezone.utc))
        state["last_positions_poll_utc"] = live.paper.iso(datetime.fromtimestamp(NOW.timestamp() - 299, tz=timezone.utc))
        fresh = live.evaluate_live_protection(state, 50.0, NOW, {"market": {}, "positions": {}}, args)
        self.assertFalse(fresh["block_new_entries"])

        fresh_mark_error = live.evaluate_live_protection(state, 50.0, NOW, {"market": {"error": "temporary"}, "positions": {}}, args)
        self.assertFalse(fresh_mark_error["block_new_entries"])

        state["last_mark_poll_utc"] = live.paper.iso(datetime.fromtimestamp(NOW.timestamp() - 301, tz=timezone.utc))
        stale = live.evaluate_live_protection(state, 50.0, NOW, {"market": {"error": "temporary"}, "positions": {}}, args)
        self.assertTrue(stale["block_new_entries"])
        self.assertEqual(stale["reasons"][0]["code"], "stale_mark")
        self.assertIn("mark_fetch_error", [r["code"] for r in stale["reasons"]])

        state["last_mark_poll_utc"] = live.paper.iso(NOW)
        state["last_positions_poll_utc"] = live.paper.iso(datetime.fromtimestamp(NOW.timestamp() - 301, tz=timezone.utc))
        stale_positions = live.evaluate_live_protection(state, 50.0, NOW, {"market": {}, "positions": {"error": "fetch failed"}}, args)
        self.assertTrue(stale_positions["block_new_entries"])
        self.assertEqual(stale_positions["reasons"][0]["code"], "positions_fetch_error_stale")

    def test_protection_side_effect_only_writes_stop_new_orders(self):
        with tempfile.TemporaryDirectory() as td:
            args = make_args(out_dir=td, protection_auto_stop_new_orders=True)
            protection = {"block_new_entries": True, "reasons": [{"code": "floating_pnl_stop"}]}
            with patch.object(live, "submit_open") as submit_open, patch.object(live, "submit_close") as submit_close:
                event = live.apply_live_protection_side_effects(args, protection, NOW)
            self.assertEqual(event["type"], "protection_stop_new_orders_created")
            self.assertTrue(Path(td, "STOP_NEW_ORDERS").exists())
            self.assertFalse(Path(td, "KILL").exists())
            self.assertFalse(submit_open.called)
            self.assertFalse(submit_close.called)

    def test_hot_stop_writes_snapshot_without_loading_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            args = make_args(out_dir=td, state_path=str(Path(td) / "state.json"), status_path=str(Path(td) / "RUN_STATUS.json"), telemetry_path=str(Path(td) / "telemetry.jsonl"))
            state = live.paper.default_state(args)
            state["open_trades"] = {"HYPEUSDT:LONG": open_trade()}
            live.paper.write_json(Path(args.state_path), state)
            Path(td, "HOT_STOP").write_text("", encoding="utf-8")

            with patch.object(live.paper, "utc_now", return_value=NOW), patch.object(live, "load_inputs_live") as load_inputs:
                with self.assertRaises(SystemExit):
                    live.poll_once(args)

            self.assertFalse(load_inputs.called)
            snapshot = live.paper.load_json(Path(td) / "HOT_RESTART_SNAPSHOT.json", {})
            self.assertEqual(snapshot["schema"], "hype_cap100_live_hot_restart_snapshot_v1")
            self.assertIn("HYPEUSDT:LONG", snapshot["state"]["open_trades"])
            status = live.paper.load_json(Path(args.status_path), {})
            self.assertTrue(status["hot_stop_requested"])
            self.assertEqual(status["hot_restart_snapshot_path"], str(Path(td) / "HOT_RESTART_SNAPSHOT.json"))

    def test_resume_snapshot_restores_state_and_requires_overwrite_for_existing_state(self):
        with tempfile.TemporaryDirectory() as td:
            source_state = live.paper.default_state(make_args())
            source_state["open_trades"] = {"HYPEUSDT:LONG": open_trade()}
            snapshot = {
                "schema": "hype_cap100_live_hot_restart_snapshot_v1",
                "utc": live.paper.iso(NOW),
                "state": source_state,
                "status": {"utc": live.paper.iso(NOW)},
            }
            snapshot_path = Path(td) / "snapshot.json"
            live.paper.write_json(snapshot_path, snapshot)
            state_path = Path(td) / "state.json"
            args = make_args(out_dir=td, state_path=str(state_path), resume_snapshot=str(snapshot_path))

            loaded = live.load_resume_snapshot(args)
            self.assertEqual(loaded["utc"], live.paper.iso(NOW))
            restored = live.paper.load_json(state_path, {})
            self.assertIn("HYPEUSDT:LONG", restored["open_trades"])
            self.assertEqual(restored["events"][-1]["type"], "resume_snapshot_loaded")

            args.resume_snapshot_overwrite = False
            with self.assertRaises(SystemExit):
                live.load_resume_snapshot(args)

    def test_parser_normalize_validate_and_reports_root_behavior(self):
        parser = live.build_arg_parser()
        args = parser.parse_args([])
        self.assertEqual(Path(args.out_dir), live.DEFAULT_OUT_DIR)
        if os.name == "nt":
            self.assertTrue(str(Path(args.out_dir).parent).endswith(r"obw_platform\_reports\_live"))
        else:
            self.assertEqual(Path(args.out_dir).parent, Path("/var/www/vps2.happyuser.info/top/top_1/obw_platform/_reports/_live"))
        self.assertEqual(args.live_exchange_profile, "gateio_current")
        with tempfile.TemporaryDirectory() as td:
            args.out_dir = td
            args.session_db = "session.sqlite"
            live.normalize_paths(args)
            live.validate_args(args)
            self.assertEqual(args.live_exchange, "gateio")
            self.assertEqual(args.live_symbol, "HYPE-USDT")
            self.assertEqual(args.position_mode, "oneway")
            self.assertEqual(args.env_file, live.DEFAULT_ENV_FILE)
            self.assertEqual(Path(args.session_db), Path(td) / "session.sqlite")
            self.assertEqual(Path(args.state_path), Path(td) / "state.json")

        legacy_args = parser.parse_args(["--live-exchange-profile", "bingx_legacy"])
        with tempfile.TemporaryDirectory() as td:
            legacy_args.out_dir = td
            live.normalize_paths(legacy_args)
            live.validate_args(legacy_args)
            self.assertEqual(legacy_args.live_exchange, "bingx")
            self.assertEqual(legacy_args.live_symbol, "HYPE-USDT")
            self.assertEqual(legacy_args.position_mode, "hedge")
            self.assertEqual(legacy_args.env_file, live.BINGX_LEGACY_ENV_FILE)
            self.assertEqual(legacy_args.interval_sec, 60.0)

    def test_bingx_live_config_applies_order_attempt_and_mark_throttle(self):
        cfg = Path("obw_platform/meta_strategies/telegram_signal_dca/configs/bingx_veronika_hype_live_54.json")
        args = live.build_arg_parser().parse_args(["--live-config", str(cfg)])
        with tempfile.TemporaryDirectory() as td:
            args.out_dir = td
            live.normalize_paths(args)
            live.validate_args(args)
        self.assertEqual(args.live_exchange, "bingx")
        self.assertGreaterEqual(args.interval_sec, 60.0)
        self.assertEqual(args.max_order_attempts_per_hour, 6)
        self.assertEqual(args.order_post_throttle_sec, 10)
        self.assertEqual(args.mark_poll_interval_sec, 60)

    def test_veronika_hype_configs_enable_source_box_ratio_with_liquid_base_scaled_box(self):
        configs = [
            "bingx_veronika_hype_live_54.json",
            "gateio_veronika_hype_live_310.json",
        ]
        for name in configs:
            cfg = Path("obw_platform/meta_strategies/telegram_signal_dca/configs") / name
            args = live.build_arg_parser().parse_args(["--live-config", str(cfg)])
            with tempfile.TemporaryDirectory() as td:
                args.out_dir = td
                live.normalize_paths(args)
                live.validate_args(args)
            sizing = args._live_config["sizing"]
            self.assertEqual(args.source_size_sync_mode, "ratio", name)
            self.assertEqual(args.source_size_sync_interval_sec, 60.0, name)
            self.assertEqual(args.source_size_sync_min_adjust_notional_usdt, 2.0, name)
            self.assertEqual(sizing["box_config_class"], "LiquidBaseScaledBoxConfig", name)
            self.assertEqual(sizing["dca_profile"], "liquid_base_scaled_dca8", name)
            self.assertEqual(sizing["selected_dca_count"], 8, name)

            plan = live.copy_signal_meta.dca.build_plan(
                args.initial_equity,
                args,
                entry_price=50.0,
                side="LONG",
                sizing=sizing,
            )
            self.assertEqual(plan["box_config_class"], "LiquidBaseScaledBoxConfig", name)
            self.assertEqual(len(plan["add_notionals"]), 8, name)

    def test_record_and_upsert_session_helpers_are_noops_without_session_db_and_call_db_with_session_db(self):
        args = make_args(session_db="")
        live.record_session_order(args, now=NOW, symbol="HYPE-USDT", side="LONG", type_="OPEN", price=50.0, qty=0.1, status="FILLED", reason="test")
        live.upsert_session_position(args, open_trade(), status="OPEN", now=NOW)
        args.session_db = "session.sqlite"
        with patch.object(live, "insert_order_row") as insert, patch.object(live, "db_upsert_open_position") as upsert:
            live.record_session_order(args, now=NOW, symbol="HYPE-USDT", side="LONG", type_="OPEN", price=50.0, qty=0.1, status="FILLED", reason="test")
            live.upsert_session_position(args, open_trade(), status="OPEN", now=NOW, exchange_order_id="ex-1")
        self.assertTrue(insert.called)
        self.assertTrue(upsert.called)

    def test_ui_artifacts_emit_live_equity_and_match_ready_csv_from_session_orders(self):
        with tempfile.TemporaryDirectory() as td:
            session_db = str(Path(td) / "session.sqlite")
            live.ensure_session_dbs(td, session_db)
            live.ensure_orders_db(session_db)
            args = make_args(out_dir=td, session_db=session_db, run_id="run-artifacts")
            live.record_session_order(
                args,
                now=NOW,
                symbol="HYPE-USDT",
                side="LONG",
                type_="OPEN",
                price=50.25,
                qty=0.2,
                status="FILLED",
                reason="artifact_test",
                exchange_order_id="ex-artifact-1",
                extra={"submitted": {"order": {"fee": {"cost": "0.005", "currency": "USDT"}}}},
            )
            other_args = make_args(out_dir=td, session_db=session_db, run_id="old-run")
            live.record_session_order(
                other_args,
                now=NOW,
                symbol="HYPE-USDT",
                side="LONG",
                type_="OPEN",
                price=49.0,
                qty=9.9,
                status="FILLED",
                reason="old_run",
                exchange_order_id="old-artifact",
            )
            live.record_session_order(
                args,
                now=NOW,
                symbol="HYPE-USDT",
                side="LONG",
                type_="CLOSE",
                price=51.0,
                qty=0.2,
                status="FILLED",
                reason="close_artifact",
                exchange_order_id="ex-close",
                extra={"closed": {"paper_pnl_usdt": -0.1097}, "submitted": {"order": {"fee": {"cost": "0.007", "currency": "USDT"}}}},
            )
            status = {
                "utc": live.paper.iso(NOW),
                "input_meta": {"market": {"mark": 50.75}},
                "guards": {
                    "daily_realized_plus_unrealized_pnl_usdt": 1.25,
                    "gross_open_notional": 10.05,
                },
            }
            artifacts = live.emit_ui_artifacts(args, status)

            self.assertTrue(Path(artifacts["live_equity_csv"]).exists())
            with open(artifacts["live_equity_csv"], newline="", encoding="utf-8-sig") as fh:
                equity_rows = list(csv.DictReader(fh))
            self.assertEqual(equity_rows[-1]["ts"], live.paper.iso(NOW))
            self.assertEqual(float(equity_rows[-1]["value"]), 1.25)

            self.assertTrue(Path(artifacts["match_ready_csv"]).exists())
            with open(artifacts["match_ready_csv"], newline="", encoding="utf-8-sig") as fh:
                match_rows = list(csv.DictReader(fh))
            self.assertEqual(len(match_rows), 2)
            self.assertTrue(match_rows[0]["Ордер №"].startswith("hypecap100-"))
            self.assertIn("HYPEUSDT", match_rows[0]["Ф’ючерси / Напрямок"])
            self.assertIn("Відкрити Long", match_rows[0]["Ф’ючерси / Напрямок"])
            self.assertEqual(match_rows[0]["Комісія"], "0.005 USDT")
            self.assertIn("Закрити Long", match_rows[1]["Ф’ючерси / Напрямок"])
            self.assertEqual(match_rows[1]["Закриті PnL / %"], "-0.1097 USDT")
            self.assertEqual(match_rows[1]["Комісія"], "0.007 USDT")

            self.assertTrue(Path(artifacts["live_candles_csv"]).exists())
            with open(artifacts["live_candles_csv"], newline="", encoding="utf-8-sig") as fh:
                candle_rows = list(csv.DictReader(fh))
            self.assertEqual(candle_rows[-1]["ts"], live.paper.iso(NOW))
            self.assertEqual(float(candle_rows[-1]["close"]), 50.75)
            self.assertEqual(candle_rows[-1]["symbol"], "HYPE-USDT")

            with open(artifacts["live_chart_events_csv"], newline="", encoding="utf-8-sig") as fh:
                chart_rows = list(csv.DictReader(fh))
            self.assertEqual(len(chart_rows), 2)
            self.assertNotEqual(chart_rows[0]["price"], "49")

            self.assertTrue(Path(artifacts["live_cache_npz"]).exists())
            with np.load(artifacts["live_cache_npz"], allow_pickle=False) as z:
                self.assertEqual([str(x) for x in z["symbols"].tolist()], ["HYPE-USDT"])
                self.assertEqual(z["offsets"].astype(int).tolist(), [0, 1])
                self.assertEqual(float(z["close"][-1]), 50.75)
                self.assertEqual(int(z["timestamp_s"][-1]), int(NOW.timestamp()))

            self.assertTrue(Path(artifacts["live_chart_events_csv"]).exists())
            with open(artifacts["live_chart_events_csv"], newline="", encoding="utf-8-sig") as fh:
                event_rows = list(csv.DictReader(fh))
            self.assertEqual(event_rows[0]["type"], "meta_open")
            self.assertEqual(event_rows[0]["side"], "LONG")
            self.assertTrue(event_rows[0]["order_id"].startswith("hypecap100-"))

            self.assertTrue(Path(artifacts["live_chart_events_jsonl"]).exists())
            with open(artifacts["live_chart_events_jsonl"], encoding="utf-8") as fh:
                jsonl_rows = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(jsonl_rows[0]["type"], "meta_open")

            self.assertTrue(Path(artifacts["live_strategy_params_json"]).exists())
            params = json.loads(Path(artifacts["live_strategy_params_json"]).read_text(encoding="utf-8"))
            self.assertEqual(params["candidate_index"], live.paper.CHAMPION_CANDIDATE_INDEX)
            self.assertEqual(params["active_params"], live.paper.CHAMPION_PARAMS)
            self.assertEqual(params["exchange"], "bingx")
            self.assertEqual(params["live_exchange_profile"], "bingx_legacy")

            con = sqlite3.connect(session_db)
            try:
                db_row = con.execute(
                    "SELECT equity_usdt, position_value_usdt, realized_pnl_cum FROM equity WHERE run_id=?",
                    ("run-artifacts",),
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(db_row, (31.25, 10.05, 1.25))

    def test_active_pointers_are_atomic_and_match_status_paths(self):
        with tempfile.TemporaryDirectory() as td:
            args = make_args(
                out_dir=td,
                state_path=str(Path(td) / "state.json"),
                status_path=str(Path(td) / "RUN_STATUS.json"),
                telemetry_path=str(Path(td) / "telemetry_current.jsonl"),
                session_db=str(Path(td) / "session.sqlite"),
                stdout_log_path=str(Path(td) / "stdout_current.log"),
            )
            live.write_active_pointers(args)
            sanity = live.active_pointer_sanity(args, {"telemetry_path": args.telemetry_path})
            self.assertTrue(sanity["ok"])
            self.assertEqual(Path(td, "ACTIVE_TELEMETRY_PATH.txt").read_text(encoding="utf-8").strip(), args.telemetry_path)
            self.assertEqual(Path(td, "ACTIVE_LOG_PATH.txt").read_text(encoding="utf-8").strip(), args.stdout_log_path)

            Path(td, "ACTIVE_TELEMETRY_PATH.txt").write_text("old.jsonl\n", encoding="utf-8")
            sanity = live.active_pointer_sanity(args, {"telemetry_path": args.telemetry_path})
            self.assertFalse(sanity["ok"])
            self.assertIn("telemetry_path", sanity["mismatches"])


if __name__ == "__main__":
    unittest.main()
