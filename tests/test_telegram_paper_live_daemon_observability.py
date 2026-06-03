import datetime as dt
import sys
import types
import unittest

if "telethon" not in sys.modules:
    telethon_stub = types.ModuleType("telethon")
    telethon_stub.TelegramClient = object
    telethon_stub.events = types.SimpleNamespace(NewMessage=lambda *args, **kwargs: None)
    sys.modules["telethon"] = telethon_stub

from obw_platform.telegram_signal_tools.telegram_signal_paper_live_daemon import (
    TelegramPollState,
    format_heartbeat_line,
    safe_error_text,
)


class TelegramPaperLiveDaemonObservabilityTest(unittest.TestCase):
    def test_heartbeat_line_reports_idle_and_counters(self):
        state = TelegramPollState()
        state.started_at = "2026-05-19T10:00:00+00:00"
        state.last_message_at = "2026-05-19T10:04:00+00:00"
        state.last_signal_at = "2026-05-19T10:03:30+00:00"
        state.last_monitor_at = "2026-05-19T10:04:50+00:00"
        state.messages_seen = 7
        state.signals_seen = 2
        state.channel_exits_seen = 1
        state.duplicates_seen = 3

        line = format_heartbeat_line(
            state,
            True,
            dt.datetime(2026, 5, 19, 10, 5, 0, tzinfo=dt.timezone.utc),
        )

        self.assertIn("[paper-live HEARTBEAT] status=alive connected=yes", line)
        self.assertIn("uptime_sec=300.0", line)
        self.assertIn("telegram_idle_sec=60.0", line)
        self.assertIn("monitor_idle_sec=10.0", line)
        self.assertIn("messages=7", line)
        self.assertIn("signals=2", line)
        self.assertIn("channel_exits=1", line)
        self.assertIn("duplicates=3", line)

    def test_heartbeat_line_marks_disconnected_as_degraded(self):
        state = TelegramPollState()
        state.started_at = "2026-05-19T10:00:00+00:00"

        line = format_heartbeat_line(
            state,
            False,
            dt.datetime(2026, 5, 19, 10, 1, 0, tzinfo=dt.timezone.utc),
        )

        self.assertIn("status=degraded", line)
        self.assertIn("connected=no", line)
        self.assertIn("telegram_idle_sec=none", line)

    def test_safe_error_text_is_single_line_and_bounded(self):
        text = safe_error_text(RuntimeError("first line\nsecond line " + ("x" * 400)))

        self.assertNotIn("\n", text)
        self.assertLessEqual(len(text), 240)
        self.assertTrue(text.startswith("RuntimeError: first line second line"))


if __name__ == "__main__":
    unittest.main()
