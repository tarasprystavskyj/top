import argparse
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from obw_platform.meta_strategies.telegram_signal_dca import live_session_telegram_monitor as monitor


NOW = datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc)


def make_args(live_dir, state_path=None, env_file=None, **overrides):
    base = dict(
        live_dir=str(live_dir),
        env_file=str(env_file or Path(live_dir) / ".env"),
        state_path=str(state_path or Path(live_dir) / "_monitor" / "state.json"),
        interval_sec=3600.0,
        loop=False,
        dry_run=False,
        init_baseline=False,
        send_startup=False,
        expected_live_exchange="gateio",
        process_pattern="hype_cap100_.*live.*canary\\.py",
        max_status_age_sec=600.0,
        order_scan_limit=250,
        rejected_recent_threshold=3,
        repeated_attempt_threshold=3,
        notional_mismatch_tolerance=0.25,
        telegram_send_gap_sec=0.0,
        daily_reexec_hour_kyiv=2,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def write_status(live_dir, **overrides):
    status = {
        "utc": monitor.iso(NOW),
        "live_exchange": "gateio",
        "session_db": str(Path(live_dir) / "session.sqlite"),
        "control": {"stop_new_orders": False, "hot_stop": False, "kill": False},
        "order_error_backoff": {"consecutive": 0, "until_ts": 0, "until_utc": ""},
        "entry_failures": {},
        "guards": {"gross_open_notional": 0.0, "one_side_open_notional": 0.0},
        "open_paper_trades": [],
    }
    status.update(overrides)
    Path(live_dir, "RUN_STATUS.json").write_text(json.dumps(status), encoding="utf-8")
    return status


def make_db(live_dir, orders=None, positions=None):
    db = Path(live_dir) / "session.sqlite"
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            """CREATE TABLE orders (
                order_id TEXT, ts_utc TEXT, bar_time_utc TEXT, mode TEXT, symbol TEXT,
                side TEXT, type TEXT, price REAL, qty REAL, status TEXT, reason TEXT,
                run_id TEXT, extra TEXT
            )"""
        )
        con.execute(
            """CREATE TABLE open_positions (
                bot_id TEXT, symbol TEXT, side TEXT, qty REAL, entry REAL,
                status TEXT, exchange TEXT, entry_fill REAL, exit_fill REAL
            )"""
        )
        for order in orders or []:
            con.execute(
                """INSERT INTO orders
                   (order_id, ts_utc, mode, symbol, side, type, price, qty, status, reason, extra)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order.get("order_id"),
                    order.get("ts_utc"),
                    order.get("mode", ""),
                    order.get("symbol", "HYPEUSDT"),
                    order.get("side", "BUY"),
                    order.get("type", "market"),
                    order.get("price", 50.0),
                    order.get("qty", 0.1),
                    order.get("status"),
                    order.get("reason", ""),
                    order.get("extra", ""),
                ),
            )
        for pos in positions or []:
            con.execute(
                """INSERT INTO open_positions
                   (bot_id, symbol, side, qty, entry, status, exchange, entry_fill, exit_fill)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pos.get("bot_id", "bot"),
                    pos.get("symbol", "HYPEUSDT"),
                    pos.get("side", "LONG"),
                    pos.get("qty", 0.0),
                    pos.get("entry", 0.0),
                    pos.get("status", "OPEN"),
                    pos.get("exchange", "gateio"),
                    pos.get("entry_fill", 0.0),
                    pos.get("exit_fill", 0.0),
                ),
            )
        con.commit()
    finally:
        con.close()
    return db


class LiveSessionTelegramMonitorTest(unittest.TestCase):
    def test_env_aliases_and_send_telegram_use_mocked_requests(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text("TG_TOKEN='fake-token'\nTG_CHAT='fake-chat'\n", encoding="utf-8")
            token, chat = monitor.telegram_credentials(env)
            self.assertEqual(token, "fake-token")
            self.assertEqual(chat, "fake-chat")

        response = SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 1}})
        with patch.object(monitor.requests, "post", return_value=response) as post:
            payload = monitor.send_telegram("fake-token", "fake-chat", "hello", timeout_sec=1)
        self.assertTrue(payload["ok"])
        self.assertEqual(post.call_count, 1)
        self.assertNotIn("fake-token", str(post.call_args.kwargs))

    def test_filled_order_alert_is_sent_once_then_deduped(self):
        with tempfile.TemporaryDirectory() as td:
            live_dir = Path(td)
            write_status(live_dir)
            make_db(
                live_dir,
                orders=[
                    {
                        "order_id": "fill-1",
                        "ts_utc": monitor.iso(NOW),
                        "mode": "open",
                        "status": "FILLED",
                        "reason": "ok",
                    }
                ],
            )
            env = live_dir / ".env"
            env.write_text("TELEGRAM_TOKEN=fake-token\nTELEGRAM_CHAT=fake-chat\n", encoding="utf-8")
            args = make_args(live_dir, env_file=env)
            proc = SimpleNamespace(stdout=f"123 python hype_cap100_bingx_live_canary.py --out-dir {live_dir}\n")
            response = SimpleNamespace(status_code=200, json=lambda: {"ok": True})
            with patch.object(monitor, "utc_now", return_value=NOW), patch.object(monitor.subprocess, "run", return_value=proc), patch.object(monitor.requests, "post", return_value=response) as post:
                first = monitor.run_check(args)
                second = monitor.run_check(args)
            self.assertEqual(first["sent"], 1)
            self.assertEqual(second["sent"], 0)
            self.assertEqual(post.call_count, 1)
            self.assertIn("[HYPE live order FILLED]", first["alerts"][0])

    def test_init_baseline_marks_existing_orders_without_order_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            live_dir = Path(td)
            write_status(live_dir)
            make_db(live_dir, orders=[{"order_id": "old-fill", "ts_utc": monitor.iso(NOW), "status": "FILLED"}])
            args = make_args(live_dir, dry_run=True, init_baseline=True)
            proc = SimpleNamespace(stdout=f"123 python hype_cap100_bingx_live_canary.py --out-dir {live_dir}\n")
            with patch.object(monitor, "utc_now", return_value=NOW), patch.object(monitor.subprocess, "run", return_value=proc):
                result = monitor.run_check(args)
            self.assertEqual(result["alerts"], [])
            state = monitor.load_monitor_state(Path(args.state_path))
            self.assertIn("old-fill", state["seen_order_ids"])
            self.assertTrue(state["baseline_initialized"])

    def test_collect_alerts_detects_suspicious_state_clusters_and_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            live_dir = Path(td)
            stale = monitor.iso(NOW - timedelta(hours=2))
            write_status(
                live_dir,
                utc=stale,
                live_exchange="bingx",
                control={"stop_new_orders": True, "hot_stop": True, "kill": False},
                order_error_backoff={"consecutive": 2, "until_ts": NOW.timestamp() + 120, "until_utc": monitor.iso(NOW + timedelta(minutes=2))},
                entry_failures={"HYPEUSDT:LONG": {"last_error": "entry rejected"}},
                guards={"gross_open_notional": 5.0, "one_side_open_notional": 5.0},
                open_paper_trades=[{"notional": 2.0}],
            )
            make_db(
                live_dir,
                orders=[
                    {"order_id": "rej-1", "ts_utc": monitor.iso(NOW), "mode": "close", "status": "REJECTED", "reason": "busy"},
                    {"order_id": "rej-2", "ts_utc": monitor.iso(NOW - timedelta(seconds=1)), "mode": "close", "status": "REJECTED", "reason": "busy"},
                    {"order_id": "rej-3", "ts_utc": monitor.iso(NOW - timedelta(seconds=2)), "mode": "close", "status": "REJECTED", "reason": "busy"},
                    {"order_id": "can-1", "ts_utc": monitor.iso(NOW - timedelta(seconds=2)), "mode": "close", "status": "CANCELED", "reason": "busy"},
                ],
                positions=[{"qty": 1.0, "entry": 10.0, "entry_fill": 10.0, "status": "OPEN"}],
            )
            args = make_args(live_dir, dry_run=True)
            with patch.object(monitor.subprocess, "run", return_value=SimpleNamespace(stdout="")):
                alerts, meta = monitor.collect_alerts(args, monitor.load_monitor_state(Path(args.state_path)), NOW)
            text = "\n".join(alerts)
            self.assertIn("stale RUN_STATUS", text)
            self.assertIn("live_exchange=bingx expected=gateio", text)
            self.assertIn("control flag active", text)
            self.assertIn("order_error_backoff active", text)
            self.assertIn("entry_failure HYPEUSDT:LONG", text)
            self.assertIn("live process missing", text)
            self.assertIn("notional mismatch", text)
            self.assertIn("position/notional mismatch", text)
            self.assertIn("rejected order cluster", text)
            self.assertIn("repeated close attempts", text)
            self.assertEqual(meta["orders_scanned"], 4)

    def test_daily_reexec_updates_state_before_exec(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            args = make_args(td, state_path=state_path, daily_reexec_hour_kyiv=2)
            state = monitor.load_monitor_state(state_path)
            with patch.object(monitor, "utc_now", return_value=datetime(2026, 5, 26, 23, 1, tzinfo=timezone.utc)), patch.object(monitor.os, "execv", side_effect=RuntimeError("exec called")):
                with self.assertRaises(RuntimeError):
                    monitor.maybe_daily_reexec(args, state)
            saved = monitor.load_monitor_state(state_path)
            self.assertEqual(saved["last_daily_reexec_date_kyiv"], "2026-05-27")


if __name__ == "__main__":
    unittest.main()
