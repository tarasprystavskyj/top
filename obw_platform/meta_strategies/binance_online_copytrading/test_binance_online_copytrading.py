import unittest
from datetime import datetime, timezone

from obw_platform.meta_strategies.binance_online_copytrading import binance_online_copytrading as mod


class FixedMarkProvider:
    def mark(self, _session, _symbol, fallback=None):
        return float(fallback or 101.0), "test_mark"

    def context(self, _session, symbol, fallback=None):
        mark, source = self.mark(_session, symbol, fallback=fallback)
        return mark, source, {"symbol": symbol, "source": source}


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
            lead_name="Callme",
            portfolio_id="p1",
            mode="follow_open",
            signal_id="s1",
            symbol="HYPEUSDT",
            side="LONG",
            mark=100.0,
            mark_source="entry_mark",
            mark_context={"symbol": "HYPEUSDT", "source": "entry_mark"},
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
            v21_runtime=None,
            now=now,
            slippage_bp=0.0,
            ttl_hours=1.0,
        )

        self.assertEqual(events[0]["type"], "paper_exit")
        self.assertEqual(state["closed_trades"][0]["exit_reason"], "lead_position_no_longer_open_market_snapshot")
        self.assertEqual(state["closed_trades"][0]["history_exit"], hist)
        self.assertEqual(state["open_positions"], {})

    def test_callme_meta_follow_open_allocates_proportionally_and_enters_existing(self):
        now = datetime(2026, 6, 4, 8, 0, tzinfo=timezone.utc)
        lead = {"name": "lead", "portfolio_id": "4512404768792222208", "lead_trader_name": "Callme"}
        meta_config = {
            "_config_path": "callme_meta_strategy_live.json",
            "schema": "callme_meta_strategy_config_v1",
            "allocation": {"default_max_notional_usdt": 54.0},
            "default_symbol_config": {"sizing": {"base_order_pct_eq": 5.0}},
            "symbols": {
                "AMDUSDT": {"exchange_symbols": {"htx": {"available": True, "live_symbol": "AMD/USDT:USDT"}}},
                "AVGOUSDT": {"exchange_symbols": {"htx": {"available": True, "live_symbol": "AVGO/USDT:USDT"}}},
            },
        }
        state = mod.default_state()

        events = mod.apply_follow_open(
            state,
            cfg={"live_orders_enabled": False},
            lead=lead,
            open_positions=[
                {"id": "amd-open", "symbol": "AMDUSDT", "side": "LONG", "entry_price": 100.0, "notional_value": 30.0},
                {"id": "avgo-open", "symbol": "AVGOUSDT", "side": "LONG", "entry_price": 200.0, "notional_value": 70.0},
            ],
            history=[],
            mark_provider=FixedMarkProvider(),
            session=None,
            v21_runtime=None,
            now=now,
            slippage_bp=0.0,
            ttl_hours=72.0,
            paper_exchange="htx",
            meta_config=meta_config,
            delegated_capital=90.0,
            enter_existing_positions=True,
        )

        decisions = [event for event in events if event["type"] == "source_position_evaluated"]
        self.assertEqual([event["decision"] for event in decisions], ["would_enter", "would_enter"])
        self.assertAlmostEqual(decisions[0]["allocation_weight"], 0.3)
        self.assertAlmostEqual(decisions[0]["target_notional"], 16.2)
        self.assertAlmostEqual(decisions[1]["allocation_weight"], 0.7)
        self.assertAlmostEqual(decisions[1]["target_notional"], 37.8)
        self.assertEqual(len(state["open_positions"]), 2)
        self.assertTrue(all(not trade.get("v21") for trade in state["open_positions"].values()))
        self.assertTrue(all(trade.get("callme_meta") for trade in state["open_positions"].values()))

    def test_callme_meta_follow_open_skips_unavailable_symbol_structured(self):
        now = datetime(2026, 6, 4, 8, 0, tzinfo=timezone.utc)
        lead = {"name": "lead", "portfolio_id": "4512404768792222208", "lead_trader_name": "Callme"}
        meta_config = {
            "_config_path": "callme_meta_strategy_live.json",
            "schema": "callme_meta_strategy_config_v1",
            "allocation": {"default_max_notional_usdt": 54.0},
            "default_symbol_config": {"sizing": {"base_order_pct_eq": 5.0}},
            "symbols": {
                "MSTRUSDT": {"exchange_symbols": {"htx": {"available": False, "reason": "HTX swap market unavailable"}}},
                "AMDUSDT": {"exchange_symbols": {"htx": {"available": True, "live_symbol": "AMD/USDT:USDT"}}},
            },
        }
        state = mod.default_state()

        events = mod.apply_follow_open(
            state,
            cfg={"live_orders_enabled": False},
            lead=lead,
            open_positions=[
                {"id": "mstr-open", "symbol": "MSTRUSDT", "side": "SHORT", "entry_price": 150.0, "notional_value": 70.0},
                {"id": "amd-open", "symbol": "AMDUSDT", "side": "LONG", "entry_price": 100.0, "notional_value": 30.0},
            ],
            history=[],
            mark_provider=FixedMarkProvider(),
            session=None,
            v21_runtime=None,
            now=now,
            slippage_bp=0.0,
            ttl_hours=72.0,
            paper_exchange="htx",
            meta_config=meta_config,
            delegated_capital=90.0,
            enter_existing_positions=True,
        )

        decisions = [event for event in events if event["type"] == "source_position_evaluated"]
        self.assertEqual(decisions[0]["decision"], "would_skip")
        self.assertEqual(decisions[0]["eligibility"]["reason"], "exchange_symbol_unavailable")
        self.assertEqual(decisions[0]["target_notional"], 0.0)
        self.assertEqual(decisions[1]["decision"], "would_enter")
        self.assertAlmostEqual(decisions[1]["target_notional"], 16.2)
        self.assertEqual(len(state["open_positions"]), 1)

    def test_callme_meta_follow_open_seeds_existing_when_disabled(self):
        now = datetime(2026, 6, 4, 8, 0, tzinfo=timezone.utc)
        lead = {"name": "lead", "portfolio_id": "4512404768792222208", "lead_trader_name": "Callme"}
        meta_config = {
            "_config_path": "callme_meta_strategy_live.json",
            "schema": "callme_meta_strategy_config_v1",
            "allocation": {"default_max_notional_usdt": 54.0},
            "default_symbol_config": {},
            "symbols": {
                "AMDUSDT": {"exchange_symbols": {"htx": {"available": True, "live_symbol": "AMD/USDT:USDT"}}},
                "AVGOUSDT": {"exchange_symbols": {"htx": {"available": True, "live_symbol": "AVGO/USDT:USDT"}}},
            },
        }
        state = mod.default_state()

        events = mod.apply_follow_open(
            state,
            cfg={"live_orders_enabled": False},
            lead=lead,
            open_positions=[
                {"id": "amd-open", "symbol": "AMDUSDT", "side": "LONG", "entry_price": 100.0, "notional_value": 30.0},
                {"id": "avgo-open", "symbol": "AVGOUSDT", "side": "LONG", "entry_price": 200.0, "notional_value": 70.0},
            ],
            history=[],
            mark_provider=FixedMarkProvider(),
            session=None,
            v21_runtime=None,
            now=now,
            slippage_bp=0.0,
            ttl_hours=72.0,
            paper_exchange="htx",
            meta_config=meta_config,
            delegated_capital=90.0,
            enter_existing_positions=False,
        )

        decisions = [event for event in events if event["type"] == "source_position_evaluated"]
        self.assertEqual([event["decision"] for event in decisions], ["would_skip", "would_skip"])
        self.assertEqual(len(state["open_positions"]), 0)
        self.assertEqual(set(state["seen_open_position_ids"]["lead"]), {"amd-open", "avgo-open"})

    def test_callme_allocation_splits_budget_by_source_notional(self):
        rows = [
            {"symbol": "AMDUSDT", "notional_value": 30.0},
            {"symbol": "AVGOUSDT", "notional_value": 70.0},
        ]

        self.assertAlmostEqual(mod.allocation_weight(rows[0], rows), 0.3)
        self.assertAlmostEqual(mod.allocation_weight(rows[1], rows), 0.7)
        self.assertAlmostEqual(mod.callme_budget_notional({"paper_notional_usdt": 90.0}, {}, {}, 0.0), 90.0)

if __name__ == "__main__":
    unittest.main()
