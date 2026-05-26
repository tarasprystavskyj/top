import unittest
from datetime import datetime, timezone

from obw_platform.meta_strategies.binance_online_copytrading import binance_online_copytrading as mod


class FixedMarkProvider:
    def mark(self, _session, _symbol, fallback=None):
        return float(fallback or 101.0), "test_mark"


class BinanceOnlineCopytradingTest(unittest.TestCase):
    def test_parse_iso_accepts_z_and_offset_without_fromisoformat(self):
        parsed_z = mod.parse_iso("2026-05-26T05:00:00Z")
        parsed_offset = mod.parse_iso("2026-05-26T08:00:00+03:00")

        self.assertEqual(parsed_z, datetime(2026, 5, 26, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(parsed_offset, parsed_z)

    def test_follow_open_close_keeps_history_exit_context(self):
        now = datetime(2026, 5, 26, 5, 0, tzinfo=timezone.utc)
        lead = {"name": "lead", "portfolio_id": "p1"}
        trade = mod.open_paper(
            strategy_name="lead",
            portfolio_id="p1",
            mode="follow_open",
            signal_id="s1",
            symbol="HYPEUSDT",
            side="LONG",
            mark=100.0,
            mark_source="entry_mark",
            now=now,
            notional=10.0,
            slippage_bp=0.0,
            ttl_hours=1.0,
            raw_signal={"id": "s1"},
        )
        state = {"open_positions": {trade["key"]: trade}, "closed_trades": [], "events": []}
        hist = {"symbol": "HYPEUSDT", "side": "LONG", "avg_close_price": 102.0, "id": "hist-1"}

        events = mod.apply_follow_open(
            state,
            lead=lead,
            open_positions=[],
            history=[hist],
            mark_provider=FixedMarkProvider(),
            session=None,
            now=now,
            notional=10.0,
            slippage_bp=0.0,
            ttl_hours=1.0,
        )

        self.assertEqual(events[0]["type"], "paper_exit")
        self.assertEqual(state["closed_trades"][0]["exit_reason"], "lead_position_no_longer_open_market_snapshot")
        self.assertEqual(state["closed_trades"][0]["history_exit"], hist)
        self.assertEqual(state["open_positions"], {})


if __name__ == "__main__":
    unittest.main()
