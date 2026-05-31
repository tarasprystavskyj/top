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
fake_live_runner_dual._normalize_order_qty = lambda _client, _symbol, qty, is_close=False: qty
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
        entry_failure_cooldown_sec=3600.0,
        hot_restart_snapshot_path="",
        resume_snapshot="",
        resume_snapshot_overwrite=False,
        stdout_log_path="",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class FakeExchange:
    def __init__(self, orders=None, failures=None, balance=None):
        self.orders = list(orders or [{"id": "ex-1", "average": 50.0, "filled": 0.2}])
        self.failures = list(failures or [])
        self.calls = []
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
        with patch.object(live.paper, "fetch_open_positions", side_effect=RuntimeError("positions down")), patch.object(
            live.paper, "fetch_mark", side_effect=RuntimeError("mark down")
        ), patch.object(live.paper, "fetch_position_history") as fetch_history:
            positions, history, mark, meta = live.load_inputs_live(args, state, NOW)
        self.assertEqual(positions, [lead_long()])
        self.assertEqual(history, [{"id": "hist"}])
        self.assertEqual(mark, 49.5)
        self.assertFalse(fetch_history.called)
        self.assertEqual(meta["positions"]["cached_rows"], 1)
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
            meta = live.sync_trade_from_exchange(args, trade)
        self.assertTrue(meta["synced"])
        self.assertEqual(trade["qty"], 0.3)
        self.assertEqual(trade["avg_entry"], 51.0)
        self.assertEqual(trade["notional"], 15.299999999999999)

    def test_sync_trade_from_exchange_updates_session_open_position(self):
        with tempfile.TemporaryDirectory() as td:
            session_db = str(Path(td) / "session.sqlite")
            live.ensure_session_dbs(td, session_db)
            args = make_args(out_dir=td, session_db=session_db)
            args._live_client = FakeClient()
            trade = open_trade()
            with patch.object(live, "_fetch_exchange_position", return_value={"qty": 0.3, "entry": 51.0}), patch.object(live.paper, "utc_now", return_value=NOW):
                meta = live.sync_trade_from_exchange(args, trade)
            self.assertTrue(meta["synced"])
            con = sqlite3.connect(session_db)
            try:
                row = con.execute("SELECT qty, entry, status FROM open_positions WHERE status='OPEN'").fetchone()
            finally:
                con.close()
            self.assertEqual(row, (0.3, 51.0, "OPEN"))

    def test_live_add_fill_blocks_on_stop_file_without_order(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "STOP_NEW_ORDERS").write_text("", encoding="utf-8")
            args = make_args(control_dir=td)
            with patch.object(live, "submit_open") as submit_open:
                event = live.live_add_fill({}, open_trade(), now=NOW, expected_price=50.0, notional=10.0, fill_type="base", reason="test", mark=50.0, args=args)
        self.assertEqual(event["type"], "live_entry_blocked")
        self.assertEqual(event["reason"], "stop_new_orders_file")
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
        args = make_args()
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
