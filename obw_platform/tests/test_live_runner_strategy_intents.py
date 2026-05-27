from __future__ import annotations

import unittest
import tempfile
import sqlite3
import importlib
from pathlib import Path
from types import SimpleNamespace

from obw_platform.runners import live_runner_dual as live_runner
from obw_platform.meta_strategies.telegram_signal_dca.hype_grounded_compound_paper_live import (
    ADD_WEIGHTS,
    INITIAL_EQUITY,
    INITIAL_TARGET_NOTIONAL,
    dca_levels,
    fill_plan,
    should_fill_level,
)
from obw_platform.runners.strategy_intents import (
    INTENT_ADD,
    INTENT_CLOSE,
    INTENT_HOLD,
    INTENT_OPEN,
    INTENT_PARTIAL_CLOSE,
    StrategyRejectionBackoff,
    entry_intent_from_signal,
    manage_intent_from_result,
    notify_strategy_filled,
    notify_strategy_rejected,
    strategy_can_submit,
)
from obw_platform.runners.legacy_strategy_adapter import (
    LegacyStrategyPolicy,
    call_manage_position,
)


class StrategyIntentTests(unittest.TestCase):
    def test_strategy_entry_signal_is_the_only_open_source(self):
        hold = entry_intent_from_signal(
            symbol="HYPE/USDT:USDT",
            side="LONG",
            signal=None,
            requested_price=40.0,
            notional_long=100.0,
            notional_short=50.0,
        )
        self.assertEqual(hold.kind, INTENT_HOLD)

        sig = SimpleNamespace(qty=None, tp=42.0, sl=38.0, order_type="limit", limit_price=39.5, sizing_policy="delegate_notional")
        intent = entry_intent_from_signal(
            symbol="HYPE/USDT:USDT",
            side="LONG",
            signal=sig,
            requested_price=40.0,
            notional_long=100.0,
            notional_short=50.0,
        )
        self.assertEqual(intent.kind, INTENT_OPEN)
        self.assertAlmostEqual(intent.qty, 2.5)
        self.assertEqual(intent.order_type, "limit")
        self.assertEqual(intent.limit_price, 39.5)
        self.assertEqual(intent.tp_price, 42.0)
        self.assertEqual(intent.sl_price, 38.0)

    def test_manage_result_decides_close_partial_and_dca(self):
        close = manage_intent_from_result(
            symbol="HYPE/USDT:USDT",
            side="LONG",
            result=SimpleNamespace(action="TP", reason="full_tp"),
            qty_before=3.0,
            entry_before=40.0,
        )
        self.assertEqual(close.kind, INTENT_CLOSE)
        self.assertEqual(close.qty, 3.0)
        self.assertEqual(close.reason, "full_tp")

        partial = manage_intent_from_result(
            symbol="HYPE/USDT:USDT",
            side="LONG",
            result=SimpleNamespace(action="TP_PARTIAL", qty_frac=0.25),
            qty_before=4.0,
            entry_before=40.0,
        )
        self.assertEqual(partial.kind, INTENT_PARTIAL_CLOSE)
        self.assertEqual(partial.qty, 1.0)

        dca = manage_intent_from_result(
            symbol="HYPE/USDT:USDT",
            side="LONG",
            result=SimpleNamespace(action="DCA", delta_qty=0.75, limit_price=39.0),
            qty_before=2.0,
            entry_before=40.0,
            default_order_type="limit",
        )
        self.assertEqual(dca.kind, INTENT_ADD)
        self.assertEqual(dca.qty, 0.75)
        self.assertEqual(dca.limit_price, 39.0)

    def test_legacy_state_delta_dca_is_explicitly_labeled_strategy_state(self):
        dca = manage_intent_from_result(
            symbol="HYPE/USDT:USDT",
            side="LONG",
            result=None,
            qty_before=2.0,
            entry_before=40.0,
            qty_after=2.5,
            entry_after=39.8,
            default_order_type="market",
        )
        self.assertEqual(dca.kind, INTENT_ADD)
        self.assertEqual(dca.source, "manage_position_state_delta")
        self.assertAlmostEqual(dca.qty, 0.5)

    def test_strategy_owned_backoff_blocks_retry_storms_and_resets_on_fill(self):
        strat = SimpleNamespace(execution_backoff=StrategyRejectionBackoff(max_failures=1, cooldown_sec=3600.0))
        self.assertTrue(strategy_can_submit(strat, "HYPE/USDT:USDT", "open"))

        notify_strategy_rejected(strat, "HYPE/USDT:USDT", "open", {"error": "exchange_reject"})

        self.assertFalse(strategy_can_submit(strat, "HYPE/USDT:USDT", "open"))
        state = strat.execution_backoff.export_state()
        self.assertEqual(state["failures"], 1)
        self.assertEqual(state["last_event"], "open")
        self.assertEqual(state["last_details"], {"error": "exchange_reject"})

        notify_strategy_filled(strat, "HYPE/USDT:USDT")
        self.assertTrue(strategy_can_submit(strat, "HYPE/USDT:USDT", "open"))

    def test_close_fill_does_not_reset_entry_backoff(self):
        strat = SimpleNamespace(execution_backoff=StrategyRejectionBackoff(max_failures=1, cooldown_sec=3600.0))
        notify_strategy_rejected(strat, "HYPE/USDT:USDT", "open", {"error": "exchange_reject"})
        self.assertFalse(strategy_can_submit(strat, "HYPE/USDT:USDT", "open"))

        notify_strategy_filled(strat, "HYPE/USDT:USDT", event="close")

        self.assertFalse(strategy_can_submit(strat, "HYPE/USDT:USDT", "open"))
        notify_strategy_filled(strat, "HYPE/USDT:USDT", event="dca")
        self.assertTrue(strategy_can_submit(strat, "HYPE/USDT:USDT", "open"))

    def test_legacy_adapter_does_not_mask_strategy_body_typeerror(self):
        class Strategy:
            def __init__(self):
                self.calls = 0

            def manage_position(self, sym, row, pos, ctx=None):
                self.calls += 1
                raise TypeError("strategy body bug")

        strat = Strategy()
        with self.assertRaisesRegex(TypeError, "strategy body bug"):
            call_manage_position(strat, "HYPE/USDT:USDT", {}, SimpleNamespace(qty=1.0, entry=40.0), {}, LegacyStrategyPolicy(enabled=True))
        self.assertEqual(strat.calls, 1)

    def test_hype_paper_dca_levels_map_to_strategy_add_intent(self):
        args = SimpleNamespace(initial_equity=INITIAL_EQUITY, initial_target_notional=INITIAL_TARGET_NOTIONAL)
        plan = fill_plan(INITIAL_EQUITY, args, "LONG", 40.0)

        self.assertEqual(tuple(plan.levels), dca_levels("LONG", 40.0))
        self.assertAlmostEqual(sum(plan.add_notionals), plan.target_notional - plan.base_notional)
        self.assertEqual(len(plan.add_notionals), len(ADD_WEIGHTS))
        self.assertTrue(should_fill_level("LONG", plan.levels[0] - 0.001, plan.levels[0]))

        qty_before = plan.base_notional / 40.0
        add_qty = plan.add_notionals[0] / plan.levels[0]
        intent = manage_intent_from_result(
            symbol="HYPEUSDT",
            side="LONG",
            result=None,
            qty_before=qty_before,
            entry_before=40.0,
            qty_after=qty_before + add_qty,
            entry_after=39.95,
            default_order_type="market",
        )
        self.assertEqual(intent.kind, INTENT_ADD)
        self.assertAlmostEqual(intent.qty, add_qty)
        self.assertEqual(intent.source, "manage_position_state_delta")


class _PatchAttr:
    def __init__(self, obj, name, value):
        self.obj = obj
        self.name = name
        self.value = value
        self.old = None

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self.value

    def __exit__(self, exc_type, exc, tb):
        setattr(self.obj, self.name, self.old)
        return False


class _RunnerStrategy:
    SIDE = "LONG"

    def __init__(self, *, entry_signal=None, manage_result=None, mutate_manage=None, can_submit=True, dca_order_type="market"):
        self._entry_signal = entry_signal
        self._manage_result = manage_result
        self._mutate_manage = mutate_manage
        self.can_submit = can_submit
        self.dca_order_type = dca_order_type

    def entry_signal(self, is_opening, sym, row, ctx=None):
        return self._entry_signal

    def manage_position(self, sym, row, pos, ctx=None):
        if self._mutate_manage:
            self._mutate_manage(pos)
        return self._manage_result

    def can_submit_order(self, sym, event="", details=None):
        return self.can_submit

    def get_execution_policy(self, sym, event=""):
        if str(event or "").lower() == "dca":
            return {"order_type": self.dca_order_type}
        return {}

    def export_state_snapshot(self, sym):
        return {"snapshot": True}

    def restore_state_snapshot(self, sym, snapshot):
        self.restored = snapshot


class LiveRunnerIntentRoutingTests(unittest.TestCase):
    def setUp(self):
        self.row = {
            "datetime_utc": "2026-05-27T00:00:00+00:00",
            "open": 40.0,
            "high": 40.5,
            "low": 39.5,
            "close": 40.0,
            "volume": 1.0,
        }

    def test_attempt_entry_does_not_execute_without_strategy_signal(self):
        calls = []
        strategy = _RunnerStrategy(entry_signal=None)
        with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
            live_runner,
            "_execute_open_with_rollback",
            lambda *args, **kwargs: calls.append(kwargs) or (True, {}),
        ):
            ok = live_runner._attempt_entry(
                object(),
                "HYPE/USDT:USDT",
                "LONG",
                strategy,
                self.row,
                {},
                "/tmp/no-write",
                "hedge",
                "",
                "bot",
                "run",
                notional_long=100.0,
                notional_short=100.0,
            )
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_attempt_entry_executes_only_strategy_open_intent(self):
        calls = []
        strategy = _RunnerStrategy(entry_signal=SimpleNamespace(qty=2.0, order_type="market", tp=42.0, sl=38.0))
        with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
            live_runner,
            "_execute_open_with_rollback",
            lambda *args, **kwargs: calls.append(kwargs) or (True, {}),
        ):
            ok = live_runner._attempt_entry(
                object(),
                "HYPE/USDT:USDT",
                "LONG",
                strategy,
                self.row,
                {},
                "/tmp/no-write",
                "hedge",
                "",
                "bot",
                "run",
                notional_long=100.0,
                notional_short=100.0,
            )
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["qty"], 2.0)
        self.assertEqual(calls[0]["tp_price"], 42.0)
        self.assertEqual(calls[0]["sl_price"], 38.0)

    def test_manage_hold_does_not_execute_close_or_dca(self):
        open_calls = []
        close_calls = []
        strategy = _RunnerStrategy(manage_result=None)
        rec = {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0}
        with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
            live_runner,
            "_execute_open_with_rollback",
            lambda *args, **kwargs: open_calls.append(kwargs) or (True, {}),
        ), _PatchAttr(
            live_runner,
            "_execute_reduce_with_rollback",
            lambda *args, **kwargs: close_calls.append(kwargs) or (True, {}),
        ):
            ok = live_runner._maybe_apply_manage_result(
                object(),
                live_runner.pos_key("HYPE/USDT:USDT", "LONG"),
                rec,
                self.row,
                strategy,
                {live_runner.pos_key("HYPE/USDT:USDT", "LONG"): rec},
                "/tmp/no-write",
                "hedge",
                "",
                "bot",
                "run",
            )
        self.assertFalse(ok)
        self.assertEqual(open_calls, [])
        self.assertEqual(close_calls, [])

    def test_manage_dca_state_delta_executes_add_intent(self):
        open_calls = []

        def mutate(pos):
            pos.qty += 0.5
            pos.entry = 39.9

        strategy = _RunnerStrategy(manage_result=None, mutate_manage=mutate)
        rec = {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0}
        positions = {live_runner.pos_key("HYPE/USDT:USDT", "LONG"): rec}
        with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
            live_runner,
            "_execute_open_with_rollback",
            lambda *args, **kwargs: open_calls.append(kwargs) or (True, {}),
        ), _PatchAttr(
            live_runner,
            "db_upsert_open_position",
            lambda *args, **kwargs: None,
        ):
            ok = live_runner._maybe_apply_manage_result(
                object(),
                live_runner.pos_key("HYPE/USDT:USDT", "LONG"),
                rec,
                self.row,
                strategy,
                positions,
                "/tmp/no-write",
                "hedge",
                "",
                "bot",
                "run",
            )
        self.assertTrue(ok)
        self.assertEqual(len(open_calls), 1)
        self.assertAlmostEqual(open_calls[0]["qty"], 0.5)
        self.assertEqual(open_calls[0]["strategy_event"], "dca")

    def test_strategy_backoff_blocks_runner_execution(self):
        calls = []
        strategy = _RunnerStrategy(entry_signal=SimpleNamespace(qty=2.0), can_submit=False)
        with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
            live_runner,
            "_execute_open_with_rollback",
            lambda *args, **kwargs: calls.append(kwargs) or (True, {}),
        ), _PatchAttr(live_runner, "_record_order", lambda *args, **kwargs: None), _PatchAttr(
            live_runner,
            "_emit_runtime_debug",
            lambda *args, **kwargs: None,
        ):
            ok = live_runner._attempt_entry(
                object(),
                "HYPE/USDT:USDT",
                "LONG",
                strategy,
                self.row,
                {},
                "/tmp/no-write",
                "hedge",
                "",
                "bot",
                "run",
                notional_long=100.0,
                notional_short=100.0,
            )
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_missing_entry_qty_requires_explicit_sizing_delegation(self):
        intent = entry_intent_from_signal(
            symbol="HYPE/USDT:USDT",
            side="LONG",
            signal=SimpleNamespace(order_type="market"),
            requested_price=40.0,
            notional_long=100.0,
            notional_short=100.0,
        )
        self.assertEqual(intent.kind, INTENT_HOLD)
        self.assertEqual(intent.reason, "missing_explicit_qty")

    def test_legacy_size_field_maps_to_qty_only_in_adapter_mode(self):
        strict = live_runner.entry_intent_from_signal(
            symbol="HYPE/USDT:USDT",
            side="LONG",
            signal=SimpleNamespace(size=2.0, order_type="market"),
            requested_price=40.0,
            notional_long=100.0,
            notional_short=100.0,
        )
        self.assertEqual(strict.kind, INTENT_HOLD)

        calls = []
        strategy = _RunnerStrategy(entry_signal=SimpleNamespace(size=2.0, order_type="market"))
        with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
            live_runner,
            "_execute_open_with_rollback",
            lambda *args, **kwargs: calls.append(kwargs) or (True, {}),
        ):
            ok = live_runner._attempt_entry(
                object(), "HYPE/USDT:USDT", "LONG", strategy, self.row, {},
                "/tmp/no-write", "hedge", "", "bot", "run",
                notional_long=100.0, notional_short=100.0,
                cfg={"legacy_strategy_adapter": {"enabled": True}},
            )
        self.assertTrue(ok)
        self.assertEqual(calls[0]["qty"], 2.0)

    def test_legacy_no_qty_signal_delegates_notional_only_when_flag_enabled(self):
        def run_with_cfg(cfg):
            calls = []
            strategy = _RunnerStrategy(entry_signal=SimpleNamespace(order_type="market"))
            with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
                live_runner,
                "_execute_open_with_rollback",
                lambda *args, **kwargs: calls.append(kwargs) or (True, {}),
            ):
                ok = live_runner._attempt_entry(
                    object(), "HYPE/USDT:USDT", "LONG", strategy, self.row, {},
                    "/tmp/no-write", "hedge", "", "bot", "run",
                    notional_long=80.0, notional_short=100.0, cfg=cfg,
                )
            return ok, calls

        ok_disabled, calls_disabled = run_with_cfg({"legacy_strategy_adapter": {"enabled": True}})
        ok_enabled, calls_enabled = run_with_cfg({"legacy_strategy_adapter": {"enabled": True, "legacy_runner_notional_sizing": True}})

        self.assertFalse(ok_disabled)
        self.assertEqual(calls_disabled, [])
        self.assertTrue(ok_enabled)
        self.assertAlmostEqual(calls_enabled[0]["qty"], 2.0)

    def test_attempt_entry_restores_state_on_contract_rejected_hold(self):
        class MutatingEntryStrategy(_RunnerStrategy):
            def __init__(self):
                super().__init__(entry_signal=SimpleNamespace(order_type="market"))
                self.state = {"value": "before"}
                self.restored = None
            def entry_signal(self, is_opening, sym, row, ctx=None):
                self.state = {"value": "mutated"}
                return self._entry_signal
            def export_state_snapshot(self, sym):
                return {"value": "before"}
            def restore_state_snapshot(self, sym, snapshot):
                self.restored = snapshot
                self.state = dict(snapshot or {})

        strategy = MutatingEntryStrategy()
        with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback):
            ok = live_runner._attempt_entry(
                object(), "HYPE/USDT:USDT", "LONG", strategy, self.row, {},
                "/tmp/no-write", "hedge", "", "bot", "run",
                notional_long=100.0, notional_short=100.0,
            )
        self.assertFalse(ok)
        self.assertEqual(strategy.restored, {"value": "before"})
        self.assertEqual(strategy.state, {"value": "before"})

    def test_manage_result_restores_state_on_contract_rejected_hold(self):
        key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
        rec = {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0}
        strategy = _RunnerStrategy(manage_result=SimpleNamespace(action="DCA", delta_qty=0.5), dca_order_type="")
        ok = live_runner._maybe_apply_manage_result(
            object(), key, rec, self.row, strategy, {key: dict(rec)},
            "/tmp/no-write", "hedge", "", "bot", "run",
        )
        self.assertFalse(ok)
        self.assertEqual(strategy.restored, {"snapshot": True})

    def test_legacy_manage_position_is_live_signature_supported_when_adapter_enabled(self):
        class OldManageStrategy(_RunnerStrategy):
            def __init__(self):
                super().__init__()
                self.called = None
            def manage_position(self, is_live, sym, row, pos, ctx=None):
                self.called = (is_live, sym)
                return SimpleNamespace(action="SL")

        key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
        rec = {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0}
        close_calls = []
        strategy = OldManageStrategy()
        with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
            live_runner,
            "_execute_reduce_with_rollback",
            lambda *args, **kwargs: close_calls.append(kwargs) or (True, {}),
        ):
            ok = live_runner._maybe_apply_manage_result(
                object(), key, rec, self.row, strategy, {key: dict(rec)},
                "/tmp/no-write", "hedge", "", "bot", "run",
                cfg={"legacy_strategy_adapter": {"enabled": True}},
            )
        self.assertTrue(ok)
        self.assertEqual(strategy.called, (True, "HYPE/USDT:USDT"))
        self.assertEqual(len(close_calls), 1)

    def test_legacy_adapter_does_not_mask_modern_manage_typeerror(self):
        class BuggyModernStrategy(_RunnerStrategy):
            def manage_position(self, sym, row, pos, ctx=None):
                raise TypeError("bug inside strategy")

        key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
        rec = {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0}
        with self.assertRaisesRegex(TypeError, "bug inside strategy"):
            live_runner._maybe_apply_manage_result(
                object(), key, rec, self.row, BuggyModernStrategy(), {key: dict(rec)},
                "/tmp/no-write", "hedge", "", "bot", "run",
                cfg={"legacy_strategy_adapter": {"enabled": True}},
            )

    def test_legacy_dca_default_order_type_applies_only_as_legacy_policy(self):
        key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
        rec = {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0}

        strict_strategy = _RunnerStrategy(manage_result=SimpleNamespace(action="DCA", delta_qty=0.5), dca_order_type="")
        ok_strict = live_runner._maybe_apply_manage_result(
            object(), key, rec, self.row, strict_strategy, {key: dict(rec)},
            "/tmp/no-write", "hedge", "", "bot", "run",
        )
        self.assertFalse(ok_strict)

        calls = []
        legacy_strategy = _RunnerStrategy(manage_result=SimpleNamespace(action="DCA", delta_qty=0.5), dca_order_type="")
        with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
            live_runner,
            "_execute_open_with_rollback",
            lambda *args, **kwargs: calls.append(kwargs) or (True, {}),
        ), _PatchAttr(live_runner, "db_upsert_open_position", lambda *args, **kwargs: None):
            ok_legacy = live_runner._maybe_apply_manage_result(
                object(), key, dict(rec), self.row, legacy_strategy, {key: dict(rec)},
                "/tmp/no-write", "hedge", "", "bot", "run",
                cfg={"legacy_strategy_adapter": {"enabled": True, "legacy_dca_order_type": "market"}},
            )
        self.assertTrue(ok_legacy)
        self.assertEqual(calls[0]["order_type"], "market")

    def test_limit_reject_does_not_market_fallback_without_strategy_permission(self):
        fallback_calls = []
        old_flags = (
            live_runner.ENTRY_LIMIT_FALLBACK_TO_MARKET,
            live_runner.ENTRY_LIMIT_FALLBACK_ON_REJECT,
        )
        live_runner.ENTRY_LIMIT_FALLBACK_TO_MARKET = True
        live_runner.ENTRY_LIMIT_FALLBACK_ON_REJECT = True
        try:
            with _PatchAttr(live_runner, "safe_fetch_order_book", lambda *args, **kwargs: None), _PatchAttr(
                live_runner,
                "record_pretrade_snapshot",
                lambda *args, **kwargs: {},
            ), _PatchAttr(
                live_runner,
                "place_open_qty_limit",
                lambda *args, **kwargs: {"ok": False, "error": "post_only_reject"},
            ), _PatchAttr(
                live_runner,
                "_place_market_fallback_open",
                lambda *args, **kwargs: fallback_calls.append(kwargs) or (True, {}),
            ), _PatchAttr(live_runner, "_record_order", lambda *args, **kwargs: None), _PatchAttr(
                live_runner,
                "_write_exec_metrics",
                lambda *args, **kwargs: None,
            ), _PatchAttr(live_runner, "_emit_runtime_debug", lambda *args, **kwargs: None):
                ok, res = live_runner._execute_open_with_rollback(
                    object(),
                    _RunnerStrategy(),
                    sym="HYPE/USDT:USDT",
                    side="LONG",
                    requested_px=40.0,
                    qty=1.0,
                    bar_close=live_runner._dt.datetime.fromisoformat(self.row["datetime_utc"]),
                    position_mode="hedge",
                    snapshot={},
                    session_db_path="",
                    run_id="run",
                    bot_id="bot",
                    results_dir="/tmp/no-write",
                    positions={},
                    order_type="limit",
                    limit_price=39.5,
                    allow_market_fallback=False,
                    fallback_on_reject=False,
                )
            self.assertFalse(ok)
            self.assertEqual(res["error"], "post_only_reject")
            self.assertEqual(fallback_calls, [])
        finally:
            live_runner.ENTRY_LIMIT_FALLBACK_TO_MARKET, live_runner.ENTRY_LIMIT_FALLBACK_ON_REJECT = old_flags

    def test_limit_reject_market_fallback_requires_strategy_permission(self):
        fallback_calls = []
        old_flags = (
            live_runner.ENTRY_LIMIT_FALLBACK_TO_MARKET,
            live_runner.ENTRY_LIMIT_FALLBACK_ON_REJECT,
        )
        live_runner.ENTRY_LIMIT_FALLBACK_TO_MARKET = True
        live_runner.ENTRY_LIMIT_FALLBACK_ON_REJECT = True
        try:
            with _PatchAttr(live_runner, "safe_fetch_order_book", lambda *args, **kwargs: None), _PatchAttr(
                live_runner,
                "record_pretrade_snapshot",
                lambda *args, **kwargs: {},
            ), _PatchAttr(
                live_runner,
                "place_open_qty_limit",
                lambda *args, **kwargs: {"ok": False, "error": "post_only_reject"},
            ), _PatchAttr(
                live_runner,
                "_place_market_fallback_open",
                lambda *args, **kwargs: fallback_calls.append(kwargs) or (True, {}),
            ), _PatchAttr(live_runner, "_record_order", lambda *args, **kwargs: None):
                ok, _ = live_runner._execute_open_with_rollback(
                    object(),
                    _RunnerStrategy(),
                    sym="HYPE/USDT:USDT",
                    side="LONG",
                    requested_px=40.0,
                    qty=1.0,
                    bar_close=live_runner._dt.datetime.fromisoformat(self.row["datetime_utc"]),
                    position_mode="hedge",
                    snapshot={},
                    session_db_path="",
                    run_id="run",
                    bot_id="bot",
                    results_dir="/tmp/no-write",
                    positions={},
                    order_type="limit",
                    limit_price=39.5,
                    allow_market_fallback=True,
                    fallback_on_reject=True,
                )
            self.assertTrue(ok)
            self.assertEqual(len(fallback_calls), 1)
            self.assertTrue(fallback_calls[0]["allow_market_fallback"])
        finally:
            live_runner.ENTRY_LIMIT_FALLBACK_TO_MARKET, live_runner.ENTRY_LIMIT_FALLBACK_ON_REJECT = old_flags

    def test_direct_market_fallback_guard_blocks_without_intent_permission(self):
        market_calls = []
        with _PatchAttr(live_runner, "_stop_new_orders_active", lambda results_dir: (False, "")), _PatchAttr(
            live_runner,
            "place_open_qty",
            lambda *args, **kwargs: market_calls.append(args) or {"ok": True},
        ), _PatchAttr(live_runner, "_record_order", lambda *args, **kwargs: None), _PatchAttr(
            live_runner,
            "_emit_runtime_debug",
            lambda *args, **kwargs: None,
        ):
            ok, res = live_runner._place_market_fallback_open(
                object(),
                _RunnerStrategy(),
                sym="HYPE/USDT:USDT",
                side="LONG",
                requested_px=40.0,
                qty=1.0,
                bar_close=live_runner._dt.datetime.fromisoformat(self.row["datetime_utc"]),
                position_mode="hedge",
                session_db_path="",
                run_id="run",
                bot_id="bot",
                results_dir="/tmp/no-write",
                positions={},
                allow_market_fallback=False,
            )
        self.assertFalse(ok)
        self.assertEqual(res["error"], "market_fallback_not_strategy_allowed")
        self.assertEqual(market_calls, [])

    def test_dca_uses_intent_order_type_not_runner_config(self):
        limit_calls = []
        strategy = _RunnerStrategy(
            manage_result=SimpleNamespace(action="DCA", delta_qty=0.5, order_type="limit", limit_price=39.0)
        )
        rec = {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0}
        positions = {live_runner.pos_key("HYPE/USDT:USDT", "LONG"): rec}
        with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
            live_runner,
            "place_open_qty_limit",
            lambda *args, **kwargs: limit_calls.append(args) or {"ok": True, "order": {"id": "L1", "status": "open"}},
        ), _PatchAttr(
            live_runner,
            "_sync_pending_entry_orders",
            lambda *args, **kwargs: None,
        ), _PatchAttr(live_runner, "_record_order", lambda *args, **kwargs: None):
            ok = live_runner._maybe_apply_manage_result(
                object(),
                live_runner.pos_key("HYPE/USDT:USDT", "LONG"),
                rec,
                self.row,
                strategy,
                positions,
                "/tmp/no-write",
                "hedge",
                "",
                "bot",
                "run",
                cfg={"dca_open_order_type": "market"},
            )
        self.assertFalse(ok)
        self.assertEqual(len(limit_calls), 1)
        self.assertEqual(limit_calls[0][4], 39.0)

    def test_limit_timeout_does_not_fallback_without_timeout_permission(self):
        fallback_calls = []
        old_flags = (
            live_runner.ENTRY_LIMIT_FALLBACK_TO_MARKET,
            live_runner.ENTRY_LIMIT_FALLBACK_ON_TIMEOUT,
        )
        live_runner.ENTRY_LIMIT_FALLBACK_TO_MARKET = True
        live_runner.ENTRY_LIMIT_FALLBACK_ON_TIMEOUT = True
        bar_dt = live_runner._dt.datetime.fromisoformat(self.row["datetime_utc"])
        try:
            with _PatchAttr(live_runner, "safe_fetch_order_book", lambda *args, **kwargs: None), _PatchAttr(
                live_runner,
                "record_pretrade_snapshot",
                lambda *args, **kwargs: {},
            ), _PatchAttr(
                live_runner,
                "place_open_qty_limit",
                lambda *args, **kwargs: {"ok": True, "order": {"id": "L1"}},
            ), _PatchAttr(live_runner, "_fetch_order_fill", lambda *args, **kwargs: (None, None, {"status": "open"})), _PatchAttr(
                live_runner,
                "_cancel_order_and_fetch_final",
                lambda *args, **kwargs: ("canceled", None, None, {"status": "canceled"}),
            ), _PatchAttr(
                live_runner,
                "_place_market_fallback_open",
                lambda *args, **kwargs: fallback_calls.append(kwargs) or (True, {}),
            ), _PatchAttr(live_runner, "_record_order", lambda *args, **kwargs: None), _PatchAttr(
                live_runner,
                "_write_exec_metrics",
                lambda *args, **kwargs: None,
            ), _PatchAttr(live_runner, "_emit_runtime_debug", lambda *args, **kwargs: None):
                ok, res = live_runner._execute_open_with_rollback(
                    object(), _RunnerStrategy(), sym="HYPE/USDT:USDT", side="LONG",
                    requested_px=40.0, qty=1.0, bar_close=bar_dt, position_mode="hedge",
                    snapshot={}, session_db_path="", run_id="run", bot_id="bot",
                    results_dir="/tmp/no-write", positions={}, order_type="limit",
                    limit_price=39.5, allow_market_fallback=True, fallback_on_timeout=False,
                )
            self.assertFalse(ok)
            self.assertEqual(res["cancel_state"], "canceled")
            self.assertEqual(fallback_calls, [])
        finally:
            live_runner.ENTRY_LIMIT_FALLBACK_TO_MARKET, live_runner.ENTRY_LIMIT_FALLBACK_ON_TIMEOUT = old_flags

    def test_limit_filled_during_cancel_finalizes_without_fallback(self):
        fallback_calls = []
        finalize_calls = []
        bar_dt = live_runner._dt.datetime.fromisoformat(self.row["datetime_utc"])
        with _PatchAttr(live_runner, "safe_fetch_order_book", lambda *args, **kwargs: None), _PatchAttr(
            live_runner,
            "record_pretrade_snapshot",
            lambda *args, **kwargs: {},
        ), _PatchAttr(
            live_runner,
            "record_fill_observation",
            lambda *args, **kwargs: None,
        ), _PatchAttr(
            live_runner,
            "_online_update_slippage_model",
            lambda *args, **kwargs: None,
        ), _PatchAttr(
            live_runner,
            "place_open_qty_limit",
            lambda *args, **kwargs: {"ok": True, "order": {"id": "L1"}},
        ), _PatchAttr(live_runner, "_fetch_order_fill", lambda *args, **kwargs: (None, None, {"status": "open"})), _PatchAttr(
            live_runner,
            "_cancel_order_and_fetch_final",
            lambda *args, **kwargs: ("filled", 39.75, bar_dt, {"status": "closed", "filled": 1.0}),
        ), _PatchAttr(
            live_runner,
            "_place_market_fallback_open",
            lambda *args, **kwargs: fallback_calls.append(kwargs) or (True, {}),
        ), _PatchAttr(
            live_runner,
            "_finalize_open_success",
            lambda *args, **kwargs: finalize_calls.append(kwargs) or (True, {}),
        ), _PatchAttr(live_runner, "_emit_runtime_debug", lambda *args, **kwargs: None), _PatchAttr(
            live_runner,
            "_write_exec_metrics",
            lambda *args, **kwargs: None,
        ):
            ok, _ = live_runner._execute_open_with_rollback(
                object(), _RunnerStrategy(), sym="HYPE/USDT:USDT", side="LONG",
                requested_px=40.0, qty=1.0, bar_close=bar_dt, position_mode="hedge",
                snapshot={}, session_db_path="", run_id="run", bot_id="bot",
                results_dir="/tmp/no-write", positions={}, order_type="limit",
                limit_price=39.5, allow_market_fallback=True, fallback_on_timeout=True,
            )
        self.assertTrue(ok)
        self.assertEqual(fallback_calls, [])
        self.assertEqual(len(finalize_calls), 1)
        self.assertEqual(finalize_calls[0]["fill_px"], 39.75)

    def test_sl_exit_and_partial_route_to_reduce_executor(self):
        close_calls = []
        for action in ("SL", "EXIT", "TP_PARTIAL"):
            strategy = _RunnerStrategy(
                manage_result=SimpleNamespace(action=action, qty_frac=0.5 if action == "TP_PARTIAL" else 1.0)
            )
            rec = {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0}
            with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
                live_runner,
                "_execute_reduce_with_rollback",
                lambda *args, **kwargs: close_calls.append(kwargs) or (True, {}),
            ):
                ok = live_runner._maybe_apply_manage_result(
                    object(), live_runner.pos_key("HYPE/USDT:USDT", "LONG"), rec, self.row,
                    strategy, {live_runner.pos_key("HYPE/USDT:USDT", "LONG"): rec},
                    "/tmp/no-write", "hedge", "", "bot", "run",
                )
            self.assertTrue(ok)
        self.assertEqual([c["partial"] for c in close_calls], [False, False, True])
        self.assertEqual(close_calls[-1]["qty"], 1.0)

    def test_exchange_no_position_on_close_syncs_local_only(self):
        sync_calls = []
        rec = {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0, "order_id": "local1"}
        with _PatchAttr(live_runner, "_fetch_exchange_position", lambda *args, **kwargs: None), _PatchAttr(
            live_runner,
            "_sync_close_local_only",
            lambda *args, **kwargs: sync_calls.append(args) or None,
        ):
            ok, res = live_runner._execute_reduce_with_rollback(
                object(), _RunnerStrategy(), sym="HYPE/USDT:USDT", side="LONG", qty=2.0,
                requested_px=40.0, bar_close=live_runner._dt.datetime.fromisoformat(self.row["datetime_utc"]),
                position_mode="hedge", snapshot={}, session_db_path="", run_id="run",
                close_reason="SL", rec=rec, positions={live_runner.pos_key("HYPE/USDT:USDT", "LONG"): rec},
                results_dir="/tmp/no-write", bot_id="bot", event_type="close", partial=False,
            )
        self.assertTrue(ok)
        self.assertTrue(res["synced_only"])
        self.assertEqual(len(sync_calls), 1)

    def test_stop_and_kill_block_new_entry_orders(self):
        for guard in ("STOP_NEW_ORDERS", "KILL"):
            with tempfile.TemporaryDirectory() as td:
                Path(td, guard).write_text("operator", encoding="utf-8")
                calls = []
                strategy = _RunnerStrategy(entry_signal=SimpleNamespace(qty=1.0))
                with _PatchAttr(live_runner, "_execute_open_with_rollback", lambda *args, **kwargs: calls.append(kwargs) or (True, {})), _PatchAttr(
                    live_runner,
                    "_emit_runtime_debug",
                    lambda *args, **kwargs: None,
                ):
                    ok = live_runner._attempt_entry(
                        object(), "HYPE/USDT:USDT", "LONG", strategy, self.row, {},
                        td, "hedge", "", "bot", "run", notional_long=100.0, notional_short=100.0,
                    )
                self.assertFalse(ok)
                self.assertEqual(calls, [])

    def test_reduce_only_retry_strips_rejected_param(self):
        class FakeExchange:
            id = "binance"
            def __init__(self):
                self.calls = []
            def market(self, sym):
                return {"precision": {"amount": 3}, "limits": {"amount": {"min": 0.001}}}
            def create_order(self, sym, order_type, side, amount, price, params):
                self.calls.append(dict(params))
                if len(self.calls) == 1:
                    raise Exception("reduceOnly rejected")
                return {"id": "C1", "status": "closed"}
        class FakeFetcher:
            def __init__(self):
                self.ex = FakeExchange()
            def resolve_symbol(self, sym):
                return sym
        fetcher = FakeFetcher()
        res = live_runner.place_reduce_only(fetcher, "HYPE/USDT:USDT", "LONG", 1.0, "oneway")
        self.assertTrue(res["ok"])
        self.assertEqual(fetcher.ex.calls[0], {"reduceOnly": True})
        self.assertEqual(fetcher.ex.calls[1], {})

    def test_slippage_revert_places_reduce_and_does_not_open_position(self):
        old = live_runner.MAX_ENTRY_SLIP_BP
        live_runner.MAX_ENTRY_SLIP_BP = 1.0
        positions = {}
        reduce_calls = []
        try:
            with _PatchAttr(live_runner, "place_reduce_only", lambda *args, **kwargs: reduce_calls.append(args) or {"ok": True}), _PatchAttr(
                live_runner,
                "_record_order",
                lambda *args, **kwargs: None,
            ), _PatchAttr(live_runner, "_write_exec_metrics", lambda *args, **kwargs: None):
                ok, res = live_runner._finalize_open_success(
                    object(), _RunnerStrategy(), {}, sym="HYPE/USDT:USDT", side="LONG",
                    requested_px=40.0, qty_requested=1.0, ex_order_id="O1",
                    bar_close=live_runner._dt.datetime.fromisoformat(self.row["datetime_utc"]),
                    fill_px=41.0, fill_dt=None, session_db_path="", bot_id="bot",
                    results_dir="/tmp/no-write", positions=positions, run_id="run",
                    position_mode="hedge",
                )
            self.assertFalse(ok)
            self.assertEqual(res["error"], "max_entry_slip")
            self.assertEqual(positions, {})
            self.assertEqual(len(reduce_calls), 1)
        finally:
            live_runner.MAX_ENTRY_SLIP_BP = old

    def test_no_order_post_after_backoff_at_direct_submit_boundary(self):
        calls = []
        strategy = _RunnerStrategy(can_submit=False)
        with _PatchAttr(live_runner, "safe_fetch_order_book", lambda *args, **kwargs: None), _PatchAttr(
            live_runner,
            "record_pretrade_snapshot",
            lambda *args, **kwargs: {},
        ), _PatchAttr(
            live_runner,
            "place_open_qty",
            lambda *args, **kwargs: calls.append(args) or {"ok": True},
        ):
            ok, res = live_runner._execute_open_with_rollback(
                object(), strategy, sym="HYPE/USDT:USDT", side="LONG",
                requested_px=40.0, qty=1.0,
                bar_close=live_runner._dt.datetime.fromisoformat(self.row["datetime_utc"]),
                position_mode="hedge", snapshot={}, session_db_path="", run_id="run",
                bot_id="bot", results_dir="/tmp/no-write", positions={}, order_type="market",
            )
        self.assertFalse(ok)
        self.assertEqual(res["error"], "strategy_execution_backoff")
        self.assertEqual(calls, [])

    def test_limit_timeout_cancel_not_confirmed_never_fallbacks(self):
        fallback_calls = []
        bar_dt = live_runner._dt.datetime.fromisoformat(self.row["datetime_utc"])
        with _PatchAttr(live_runner, "safe_fetch_order_book", lambda *args, **kwargs: None), _PatchAttr(
            live_runner,
            "record_pretrade_snapshot",
            lambda *args, **kwargs: {},
        ), _PatchAttr(
            live_runner,
            "place_open_qty_limit",
            lambda *args, **kwargs: {"ok": True, "order": {"id": "L1"}},
        ), _PatchAttr(live_runner, "_fetch_order_fill", lambda *args, **kwargs: (None, None, {"status": "open"})), _PatchAttr(
            live_runner,
            "_cancel_order_and_fetch_final",
            lambda *args, **kwargs: ("unknown", None, None, {"status": "open"}),
        ), _PatchAttr(
            live_runner,
            "_place_market_fallback_open",
            lambda *args, **kwargs: fallback_calls.append(kwargs) or (True, {}),
        ), _PatchAttr(live_runner, "_record_order", lambda *args, **kwargs: None), _PatchAttr(
            live_runner,
            "_write_exec_metrics",
            lambda *args, **kwargs: None,
        ), _PatchAttr(live_runner, "_emit_runtime_debug", lambda *args, **kwargs: None):
            ok, res = live_runner._execute_open_with_rollback(
                object(), _RunnerStrategy(), sym="HYPE/USDT:USDT", side="LONG",
                requested_px=40.0, qty=1.0, bar_close=bar_dt, position_mode="hedge",
                snapshot={}, session_db_path="", run_id="run", bot_id="bot",
                results_dir="/tmp/no-write", positions={}, order_type="limit",
                limit_price=39.5, allow_market_fallback=True, fallback_on_timeout=True,
            )
        self.assertFalse(ok)
        self.assertEqual(res["cancel_state"], "unknown")
        self.assertEqual(fallback_calls, [])

    def test_limit_timeout_unconfirmed_cancel_tracks_first_entry_pending(self):
        bar_dt = live_runner._dt.datetime.fromisoformat(self.row["datetime_utc"])
        pending = {}
        fallback_calls = []
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "session.sqlite")
            live_runner.ensure_orders_db(db_path)
            with _PatchAttr(live_runner, "safe_fetch_order_book", lambda *args, **kwargs: None), _PatchAttr(
                live_runner,
                "record_pretrade_snapshot",
                lambda *args, **kwargs: {},
            ), _PatchAttr(live_runner, "place_open_qty_limit", lambda *args, **kwargs: {"ok": True, "order": {"id": "L1", "status": "open"}}), _PatchAttr(
                live_runner,
                "_fetch_order_fill",
                lambda *args, **kwargs: (None, None, {"id": "L1", "status": "open", "filled": 0.0}),
            ), _PatchAttr(
                live_runner,
                "_cancel_order_and_fetch_final",
                lambda *args, **kwargs: ("unknown", None, None, {"id": "L1", "status": "open", "filled": 0.0}),
            ), _PatchAttr(
                live_runner,
                "_place_market_fallback_open",
                lambda *args, **kwargs: fallback_calls.append(kwargs) or (True, {}),
            ), _PatchAttr(live_runner, "_strategy_rejected", lambda *args, **kwargs: None), _PatchAttr(
                live_runner,
                "_write_exec_metrics",
                lambda *args, **kwargs: None,
            ), _PatchAttr(live_runner, "_emit_runtime_debug", lambda *args, **kwargs: None):
                ok, res = live_runner._execute_open_with_rollback(
                    object(), _RunnerStrategy(), sym="HYPE/USDT:USDT", side="LONG",
                    requested_px=40.0, qty=1.0, bar_close=bar_dt, position_mode="hedge",
                    snapshot={"pos_size": 0.0}, session_db_path=db_path, run_id="run", bot_id="bot",
                    results_dir=td, positions={}, order_type="limit", limit_price=39.5,
                    strategy_event="first", allow_market_fallback=True, fallback_on_timeout=True,
                    pending_entries=pending,
                )
            key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
            self.assertFalse(ok)
            self.assertEqual(res["cancel_state"], "unknown")
            self.assertEqual(fallback_calls, [])
            self.assertEqual(pending[key]["exchange_order_id"], "L1")
            self.assertEqual(pending[key]["intent"]["strategy_event"], "first")
            self.assertEqual(live_runner._load_pending_entries(td)[key]["exchange_order_id"], "L1")
            self.assertEqual(live_runner._load_pending_entries_from_db(db_path)[key]["exchange_order_id"], "L1")

    def test_pending_limit_fill_sync_after_restart_like_reconstructed_state(self):
        class FakeExchange:
            def fetch_order(self, order_id, symbol):
                return {"id": order_id, "status": "closed", "filled": 0.5, "average": 39.0}
        class FakeFetcher:
            ex = FakeExchange()
            def resolve_symbol(self, sym):
                return sym
        with tempfile.TemporaryDirectory() as td:
            key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
            pending = {
                key: {
                    "symbol": "HYPE/USDT:USDT",
                    "side": "LONG",
                    "exchange_order_id": "L1",
                    "created_bar_iso": self.row["datetime_utc"],
                    "limit_price": 39.0,
                    "delta_qty": 0.5,
                    "applied_filled_qty": 0.0,
                    "status": "open",
                    "strategy_snapshot": {"pos_size": 2.0, "avg_price": 40.0},
                }
            }
            live_runner._save_pending_entries(td, pending, run_id="run")
            loaded = live_runner._load_pending_entries(td)
            positions = {key: {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0}}
            with _PatchAttr(live_runner, "save_positions", lambda *args, **kwargs: None), _PatchAttr(
                live_runner,
                "db_upsert_open_position",
                lambda *args, **kwargs: None,
            ), _PatchAttr(live_runner, "_record_order", lambda *args, **kwargs: None), _PatchAttr(
                live_runner,
                "_strategy_sync_after_fill",
                lambda *args, **kwargs: None,
            ):
                live_runner._sync_pending_entry_orders(
                    FakeFetcher(), loaded, positions, td, "", "bot", _RunnerStrategy(), _RunnerStrategy(),
                    self.row["datetime_utc"], run_id="run",
                )
            self.assertEqual(loaded, {})
            self.assertAlmostEqual(positions[key]["qty"], 2.5)
            self.assertAlmostEqual(positions[key]["entry"], ((2.0 * 40.0) + (0.5 * 39.0)) / 2.5)

    def test_pending_entry_db_lifecycle_create_update_fill_cancel_remove_restart_load(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "session.sqlite")
            key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
            pending = {
                key: {
                    "symbol": "HYPE/USDT:USDT",
                    "side": "LONG",
                    "exchange_order_id": "L1",
                    "created_bar_iso": self.row["datetime_utc"],
                    "limit_price": 39.0,
                    "delta_qty": 0.5,
                    "applied_filled_qty": 0.0,
                    "status": "open",
                    "strategy_snapshot": {"pos_size": 2.0, "avg_price": 40.0},
                    "intent": {"kind": "ADD", "order_type": "limit", "qty": 0.5},
                }
            }
            live_runner._save_pending_entries(td, pending, run_id="run", session_db_path=db_path)

            loaded = live_runner._load_pending_entries_from_db(db_path)
            self.assertEqual(loaded[key]["exchange_order_id"], "L1")
            self.assertEqual(loaded[key]["strategy_snapshot"], {"pos_size": 2.0, "avg_price": 40.0})
            self.assertEqual(loaded[key]["intent"]["order_type"], "limit")

            loaded[key]["applied_filled_qty"] = 0.25
            loaded[key]["status"] = "cancel_requested"
            live_runner._db_upsert_pending_entry(db_path, key, loaded[key], run_id="run")
            updated = live_runner._load_pending_entries_from_db(db_path)
            self.assertEqual(updated[key]["status"], "cancel_requested")
            self.assertAlmostEqual(updated[key]["applied_filled_qty"], 0.25)

            live_runner._db_remove_pending_entry(db_path, key, status="canceled", reason="test_cancel")
            removed = live_runner._load_pending_entries_from_db(db_path)
            self.assertEqual(removed, {})
            con = sqlite3.connect(db_path)
            try:
                row = con.execute("SELECT status, remove_reason, removed_ts_utc FROM pending_entry_orders WHERE key=?", (key,)).fetchone()
            finally:
                con.close()
            self.assertEqual(row[0], "canceled")
            self.assertEqual(row[1], "test_cancel")
            self.assertTrue(row[2])

    def test_pending_entry_persistence_preserves_strategy_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
            snapshot = {"pos_size": 2.0, "avg_price": 40.0, "lots": [[1.0, 40.0]]}
            pending = {
                key: {
                    "symbol": "HYPE/USDT:USDT",
                    "side": "LONG",
                    "exchange_order_id": "L1",
                    "created_bar_iso": self.row["datetime_utc"],
                    "limit_price": 39.0,
                    "delta_qty": 0.5,
                    "applied_filled_qty": 0.0,
                    "status": "open",
                    "strategy_snapshot": snapshot,
                }
            }
            live_runner._save_pending_entries(td, pending, run_id="run")
            loaded = live_runner._load_pending_entries(td)
            self.assertEqual(loaded[key]["strategy_snapshot"], snapshot)
            self.assertTrue(loaded[key]["snapshot_recoverable"])

    def test_pending_entry_persistence_does_not_truncate_large_strategy_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "session.sqlite")
            key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
            snapshot = {
                "lots": [{"qty": i + 1, "price": 40.0 + i} for i in range(120)],
                "wide": {f"k{i}": i for i in range(120)},
            }
            pending = {
                key: {
                    "symbol": "HYPE/USDT:USDT",
                    "side": "LONG",
                    "exchange_order_id": "L1",
                    "created_bar_iso": self.row["datetime_utc"],
                    "limit_price": 39.0,
                    "delta_qty": 0.5,
                    "applied_filled_qty": 0.0,
                    "status": "open",
                    "strategy_snapshot": snapshot,
                }
            }
            live_runner._save_pending_entries(td, pending, run_id="run", session_db_path=db_path)
            self.assertEqual(live_runner._load_pending_entries(td)[key]["strategy_snapshot"], snapshot)
            self.assertEqual(live_runner._load_pending_entries_from_db(db_path)[key]["strategy_snapshot"], snapshot)

    def test_pending_entry_hybrid_load_merges_db_and_json(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "session.sqlite")
            key_db = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
            key_json = live_runner.pos_key("ENA/USDT:USDT", "LONG")
            db_pending = {
                key_db: {
                    "symbol": "HYPE/USDT:USDT", "side": "LONG", "exchange_order_id": "DB1",
                    "created_bar_iso": self.row["datetime_utc"], "limit_price": 39.0,
                    "delta_qty": 0.5, "applied_filled_qty": 0.0, "status": "open",
                    "strategy_snapshot": {"db": True},
                }
            }
            json_pending = {
                key_json: {
                    "symbol": "ENA/USDT:USDT", "side": "LONG", "exchange_order_id": "JSON1",
                    "created_bar_iso": self.row["datetime_utc"], "limit_price": 95.0,
                    "delta_qty": 1.0, "applied_filled_qty": 0.0, "status": "open",
                    "strategy_snapshot": {"json": True},
                }
            }
            live_runner._save_pending_entries(td, json_pending, run_id="run")
            live_runner._db_upsert_pending_entry(db_path, key_db, db_pending[key_db], run_id="run")
            loaded = live_runner._load_pending_entries_hybrid(td, db_path)
            self.assertEqual(loaded[key_db]["exchange_order_id"], "DB1")
            self.assertEqual(loaded[key_json]["exchange_order_id"], "JSON1")

    def test_pending_entry_hybrid_load_preserves_same_key_json_db_collision(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "session.sqlite")
            key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
            live_runner._save_pending_entries(td, {
                key: {
                    "symbol": "HYPE/USDT:USDT", "side": "LONG", "exchange_order_id": "JSON1",
                    "created_bar_iso": self.row["datetime_utc"], "limit_price": 39.0,
                    "delta_qty": 1.0, "applied_filled_qty": 0.0, "status": "open",
                    "strategy_snapshot": {"source": "json"}, "intent": {"kind": "OPEN"},
                }
            }, run_id="run")
            live_runner._db_upsert_pending_entry(db_path, key, {
                "symbol": "HYPE/USDT:USDT", "side": "LONG", "exchange_order_id": "DB1",
                "created_bar_iso": self.row["datetime_utc"], "limit_price": 38.0,
                "delta_qty": 1.0, "applied_filled_qty": 0.0, "status": "open",
                "strategy_snapshot": {"source": "db"}, "intent": {"kind": "ADD"},
            }, run_id="run")

            loaded = live_runner._load_pending_entries_hybrid(td, db_path)
            order_ids = {v["exchange_order_id"] for v in loaded.values()}
            self.assertEqual(order_ids, {"JSON1", "DB1"})
            self.assertEqual(loaded[key]["exchange_order_id"], "DB1")
            alias_keys = [k for k in loaded if k != key]
            self.assertEqual(len(alias_keys), 1)
            self.assertEqual(live_runner._pending_position_key(alias_keys[0], loaded[alias_keys[0]]), key)

    def test_stale_pending_no_fill_after_restart_restores_persisted_snapshot(self):
        class FakeExchange:
            def __init__(self):
                self.fetch_calls = 0
                self.cancel_calls = []
            def fetch_order(self, order_id, symbol):
                self.fetch_calls += 1
                if self.fetch_calls == 1:
                    return {"id": order_id, "status": "open", "filled": 0.0}
                return {"id": order_id, "status": "canceled", "filled": 0.0}
            def cancel_order(self, order_id, symbol):
                self.cancel_calls.append((order_id, symbol))
        class FakeFetcher:
            def __init__(self):
                self.ex = FakeExchange()
            def resolve_symbol(self, sym):
                return sym
        with tempfile.TemporaryDirectory() as td:
            key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
            snapshot = {"pos_size": 2.0, "avg_price": 40.0}
            pending = {
                key: {
                    "symbol": "HYPE/USDT:USDT",
                    "side": "LONG",
                    "exchange_order_id": "L1",
                    "created_bar_iso": "2026-05-27T00:00:00+00:00",
                    "limit_price": 39.0,
                    "delta_qty": 0.5,
                    "applied_filled_qty": 0.0,
                    "status": "open",
                    "strategy_snapshot": snapshot,
                    "restart_loaded": True,
                    "snapshot_recoverable": True,
                }
            }
            strategy = _RunnerStrategy()
            with _PatchAttr(live_runner, "_strategy_rejected", lambda *args, **kwargs: None):
                live_runner._sync_pending_entry_orders(
                    FakeFetcher(), pending, {key: {"qty": 2.0, "entry": 40.0}}, td, "", "bot",
                    strategy, strategy, "2026-05-27T00:01:00+00:00", run_id="run",
                )
            self.assertEqual(pending, {})
            self.assertEqual(strategy.restored, snapshot)

    def test_stale_pending_no_fill_after_restart_blocks_without_snapshot(self):
        class FakeExchange:
            def __init__(self):
                self.fetch_calls = 0
            def fetch_order(self, order_id, symbol):
                self.fetch_calls += 1
                if self.fetch_calls == 1:
                    return {"id": order_id, "status": "open", "filled": 0.0}
                return {"id": order_id, "status": "canceled", "filled": 0.0}
            def cancel_order(self, order_id, symbol):
                return {"id": order_id}
        class FakeFetcher:
            def __init__(self):
                self.ex = FakeExchange()
            def resolve_symbol(self, sym):
                return sym
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "session.sqlite")
            key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
            pending = {
                key: {
                    "symbol": "HYPE/USDT:USDT",
                    "side": "LONG",
                    "exchange_order_id": "L1",
                    "created_bar_iso": "2026-05-27T00:00:00+00:00",
                    "limit_price": 39.0,
                    "delta_qty": 0.5,
                    "applied_filled_qty": 0.0,
                    "status": "open",
                    "strategy_snapshot": None,
                    "restart_loaded": True,
                    "snapshot_recoverable": False,
                }
            }
            strategy = _RunnerStrategy()
            live_runner._sync_pending_entry_orders(
                FakeFetcher(), pending, {key: {"qty": 2.0, "entry": 40.0}}, td, db_path, "bot",
                strategy, strategy, "2026-05-27T00:01:00+00:00", run_id="run",
            )
            self.assertIn(key, pending)
            self.assertEqual(pending[key]["status"], "recovery_blocked_no_snapshot")
            self.assertFalse(hasattr(strategy, "restored"))
            loaded = live_runner._load_pending_entries_from_db(db_path)
            self.assertEqual(loaded[key]["status"], "recovery_blocked_no_snapshot")

    def test_stale_pending_limit_fetches_and_applies_fill_before_cancel_pop(self):
        class FakeExchange:
            def __init__(self):
                self.cancel_calls = []
                self.fetch_calls = 0
            def fetch_order(self, order_id, symbol):
                self.fetch_calls += 1
                return {"id": order_id, "status": "closed", "filled": 0.5, "average": 39.0}
            def cancel_order(self, order_id, symbol):
                self.cancel_calls.append((order_id, symbol))
        class FakeFetcher:
            def __init__(self):
                self.ex = FakeExchange()
            def resolve_symbol(self, sym):
                return sym
        with tempfile.TemporaryDirectory() as td:
            key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
            pending = {
                key: {
                    "symbol": "HYPE/USDT:USDT",
                    "side": "LONG",
                    "exchange_order_id": "L1",
                    "created_bar_iso": "2026-05-27T00:00:00+00:00",
                    "limit_price": 39.0,
                    "delta_qty": 0.5,
                    "applied_filled_qty": 0.0,
                    "status": "open",
                }
            }
            positions = {key: {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0}}
            fetcher = FakeFetcher()
            with _PatchAttr(live_runner, "save_positions", lambda *args, **kwargs: None), _PatchAttr(
                live_runner,
                "db_upsert_open_position",
                lambda *args, **kwargs: None,
            ), _PatchAttr(live_runner, "_record_order", lambda *args, **kwargs: None), _PatchAttr(
                live_runner,
                "_strategy_sync_after_fill",
                lambda *args, **kwargs: None,
            ):
                live_runner._sync_pending_entry_orders(
                    fetcher, pending, positions, td, "", "bot", _RunnerStrategy(), _RunnerStrategy(),
                    "2026-05-27T00:01:00+00:00", run_id="run",
                )
            self.assertEqual(fetcher.ex.cancel_calls, [])
            self.assertEqual(pending, {})
            self.assertAlmostEqual(positions[key]["qty"], 2.5)

    def test_restart_loaded_pending_fill_restores_snapshot_before_strategy_sync(self):
        class FakeExchange:
            def fetch_order(self, order_id, symbol):
                return {"id": order_id, "status": "closed", "filled": 0.5, "average": 39.0}
        class FakeFetcher:
            ex = FakeExchange()
            def resolve_symbol(self, sym):
                return sym
        class Strategy(_RunnerStrategy):
            def __init__(self):
                super().__init__()
                self.events = []
            def restore_state_snapshot(self, sym, snapshot):
                self.events.append(("restore", snapshot))
                self.restored = snapshot
            def sync_after_external_fill(self, sym, qty, entry, fill_price=None, delta_qty=None, event=''):
                self.events.append(("sync", self.restored, qty, entry, delta_qty, event))
        with tempfile.TemporaryDirectory() as td:
            key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
            snapshot = {"pos_size": 2.0, "avg_price": 40.0}
            pending = {
                key: {
                    "symbol": "HYPE/USDT:USDT", "side": "LONG", "exchange_order_id": "L1",
                    "created_bar_iso": self.row["datetime_utc"], "limit_price": 39.0,
                    "delta_qty": 0.5, "applied_filled_qty": 0.0, "status": "open",
                    "strategy_snapshot": snapshot, "restart_loaded": True, "snapshot_recoverable": True,
                }
            }
            strategy = Strategy()
            positions = {key: {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0}}
            with _PatchAttr(live_runner, "save_positions", lambda *args, **kwargs: None), _PatchAttr(
                live_runner, "db_upsert_open_position", lambda *args, **kwargs: None,
            ), _PatchAttr(live_runner, "_record_order", lambda *args, **kwargs: None):
                live_runner._sync_pending_entry_orders(
                    FakeFetcher(), pending, positions, td, "", "bot", strategy, strategy,
                    self.row["datetime_utc"], run_id="run",
                )
            self.assertEqual(strategy.events[0], ("restore", snapshot))
            self.assertEqual(strategy.events[1][0], "sync")
            self.assertEqual(strategy.events[1][1], snapshot)

    def test_filled_pending_without_position_blocks_instead_of_popping_for_dca(self):
        class FakeExchange:
            def fetch_order(self, order_id, symbol):
                return {"id": order_id, "status": "closed", "filled": 0.5, "average": 39.0}
        class FakeFetcher:
            ex = FakeExchange()
            def resolve_symbol(self, sym):
                return sym
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "session.sqlite")
            key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
            pending = {
                key: {
                    "symbol": "HYPE/USDT:USDT", "side": "LONG", "exchange_order_id": "L1",
                    "created_bar_iso": self.row["datetime_utc"], "limit_price": 39.0,
                    "delta_qty": 0.5, "applied_filled_qty": 0.0, "status": "open",
                    "strategy_snapshot": {"pos_size": 2.0}, "restart_loaded": True,
                    "snapshot_recoverable": True, "intent": {"kind": "ADD"},
                }
            }
            live_runner._sync_pending_entry_orders(
                FakeFetcher(), pending, {}, td, db_path, "bot", _RunnerStrategy(), _RunnerStrategy(),
                self.row["datetime_utc"], run_id="run",
            )
            self.assertIn(key, pending)
            self.assertEqual(pending[key]["status"], "recovery_blocked_missing_position_after_fill")
            self.assertIn(key, live_runner._load_pending_entries_from_db(db_path))

    def test_filled_first_entry_pending_without_position_repairs_local_position(self):
        class FakeExchange:
            def fetch_order(self, order_id, symbol):
                return {"id": order_id, "status": "closed", "filled": 1.0, "average": 39.5}
        class FakeFetcher:
            ex = FakeExchange()
            def resolve_symbol(self, sym):
                return sym
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "session.sqlite")
            live_runner.ensure_orders_db(db_path)
            key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
            pending = {
                key: {
                    "symbol": "HYPE/USDT:USDT", "side": "LONG", "exchange_order_id": "L1",
                    "created_bar_iso": self.row["datetime_utc"], "limit_price": 39.5,
                    "delta_qty": 1.0, "applied_filled_qty": 0.0, "status": "open",
                    "strategy_snapshot": {"pos_size": 0.0}, "restart_loaded": True,
                    "snapshot_recoverable": True,
                    "intent": {"kind": "OPEN", "strategy_event": "first", "tp_price": 42.0, "sl_price": 37.0},
                }
            }
            positions = {}
            with _PatchAttr(live_runner, "save_positions", lambda *args, **kwargs: None):
                live_runner._sync_pending_entry_orders(
                    FakeFetcher(), pending, positions, td, db_path, "bot", _RunnerStrategy(), _RunnerStrategy(),
                    self.row["datetime_utc"], run_id="run",
                )
            self.assertEqual(pending, {})
            self.assertAlmostEqual(positions[key]["qty"], 1.0)
            self.assertAlmostEqual(positions[key]["entry"], 39.5)
            self.assertAlmostEqual(positions[key]["tp_price"], 42.0)
            self.assertAlmostEqual(positions[key]["sl_price"], 37.0)

    def test_restart_guard_blocks_untracked_open_strategy_orders(self):
        class FakeExchange:
            def __init__(self, orders):
                self.orders = orders
            def fetch_open_orders(self):
                return list(self.orders)
        class FakeFetcher:
            def __init__(self, orders):
                self.ex = FakeExchange(orders)
        pending = {live_runner.pos_key("HYPE/USDT:USDT", "LONG"): {"exchange_order_id": "L1", "symbol": "HYPE/USDT:USDT", "side": "LONG"}}
        tracked = live_runner._validate_pending_entries_restart_guard(
            FakeFetcher([{"id": "L1", "symbol": "HYPE/USDT:USDT", "status": "open", "type": "limit"}]),
            pending,
            {},
            results_dir="/tmp/no-write",
            session_db_path="",
            run_id="run",
        )
        blocked = live_runner._validate_pending_entries_restart_guard(
            FakeFetcher([{"id": "L2", "symbol": "HYPE/USDT:USDT", "status": "open", "type": "limit"}]),
            pending,
            {},
            results_dir="/tmp/no-write",
            session_db_path="",
            run_id="run",
        )
        self.assertTrue(tracked["ok"])
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["untracked_open_orders"][0]["id"], "L2")

    def test_restart_guard_exchange_wide_blocks_other_symbol_untracked_order(self):
        class FakeExchange:
            def fetch_open_orders(self):
                return [
                    {"id": "L1", "symbol": "HYPE/USDT:USDT", "status": "open", "type": "limit"},
                    {"id": "X1", "symbol": "ENA/USDT:USDT", "status": "open", "type": "limit"},
                ]
        class FakeFetcher:
            ex = FakeExchange()
        pending = {live_runner.pos_key("HYPE/USDT:USDT", "LONG"): {"exchange_order_id": "L1", "symbol": "HYPE/USDT:USDT", "side": "LONG"}}
        positions = {live_runner.pos_key("HYPE/USDT:USDT", "LONG"): {"qty": 1.0, "entry": 40.0}}
        blocked = live_runner._validate_pending_entries_restart_guard(
            FakeFetcher(), pending, positions, results_dir="/tmp/no-write", session_db_path="", run_id="run"
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["untracked_open_orders"][0]["id"], "X1")

    def test_restart_guard_fetches_symbol_scope_when_exchange_wide_omits_target_symbol(self):
        class FakeExchange:
            def __init__(self):
                self.calls = []
            def fetch_open_orders(self, symbol=None):
                self.calls.append(symbol)
                if symbol is None:
                    return [{"id": "L1", "symbol": "HYPE/USDT:USDT", "status": "open", "type": "limit"}]
                if symbol == "ENA/USDT:USDT":
                    return [{"id": "X1", "symbol": "ENA/USDT:USDT", "status": "open", "type": "limit"}]
                return []
        class FakeFetcher:
            def __init__(self):
                self.ex = FakeExchange()
            def resolve_symbol(self, sym):
                return sym
        pending = {live_runner.pos_key("HYPE/USDT:USDT", "LONG"): {"exchange_order_id": "L1", "symbol": "HYPE/USDT:USDT", "side": "LONG"}}
        positions = {live_runner.pos_key("ENA/USDT:USDT", "LONG"): {"qty": 1.0, "entry": 95.0}}
        fetcher = FakeFetcher()
        blocked = live_runner._validate_pending_entries_restart_guard(
            fetcher, pending, positions, results_dir="/tmp/no-write", session_db_path="", run_id="run"
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["untracked_open_orders"][0]["id"], "X1")
        self.assertIn("ENA/USDT:USDT", fetcher.ex.calls)

    def test_restart_guard_symbol_required_fetch_open_orders_fallback(self):
        class FakeExchange:
            def __init__(self):
                self.calls = []
            def fetch_open_orders(self, symbol=None):
                self.calls.append(symbol)
                if symbol is None:
                    raise TypeError("symbol required")
                return [{"id": "L1", "symbol": symbol, "status": "open", "type": "limit"}]
        class FakeFetcher:
            def __init__(self):
                self.ex = FakeExchange()
            def resolve_symbol(self, sym):
                return sym
        key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
        pending = {key: {"exchange_order_id": "L1", "symbol": "HYPE/USDT:USDT", "side": "LONG"}}
        fetcher = FakeFetcher()
        res = live_runner._validate_pending_entries_restart_guard(
            fetcher, pending, {}, results_dir="/tmp/no-write", session_db_path="", run_id="run"
        )
        self.assertTrue(res["ok"])
        self.assertEqual(fetcher.ex.calls, [None, "HYPE/USDT:USDT"])

    def test_restart_guard_scans_supplied_universe_when_no_local_state(self):
        class FakeExchange:
            def __init__(self):
                self.calls = []

            def fetch_open_orders(self, symbol=None):
                self.calls.append(symbol)
                if symbol is None:
                    return []
                if symbol == "ENA/USDT:USDT":
                    return [{"id": "ORPHAN", "symbol": symbol, "status": "open", "type": "limit"}]
                return []

        class FakeFetcher:
            def __init__(self):
                self.ex = FakeExchange()

            def resolve_symbol(self, sym):
                return sym

        fetcher = FakeFetcher()
        res = live_runner._validate_pending_entries_restart_guard(
            fetcher, {}, {}, results_dir="/tmp/no-write", session_db_path="", run_id="run",
            symbols=["HYPE/USDT:USDT", "ENA/USDT:USDT"],
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["untracked_open_orders"][0]["id"], "ORPHAN")
        self.assertIn("ENA/USDT:USDT", fetcher.ex.calls)

    def test_restart_guard_ignores_reduce_only_and_close_orders(self):
        class FakeExchange:
            def fetch_open_orders(self):
                return [
                    {"id": "R1", "symbol": "HYPE/USDT:USDT", "status": "open", "reduceOnly": True},
                    {"id": "R2", "symbol": "ENA/USDT:USDT", "status": "open", "info": {"closePosition": "true"}},
                    {"id": "R3", "symbol": "BTC/USDT:USDT", "status": "open", "params": {"reduceOnly": "1"}},
                ]
        class FakeFetcher:
            ex = FakeExchange()
        res = live_runner._validate_pending_entries_restart_guard(
            FakeFetcher(), {}, {}, results_dir="/tmp/no-write", session_db_path="", run_id="run"
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["open_orders"], [])

    def test_restart_guard_preserves_side_position_side_and_id_aliases(self):
        class FakeExchange:
            def fetch_open_orders(self):
                return [{"orderId": "OID1", "symbol": "ENA/USDT:USDT", "status": "open", "type": "limit", "side": "sell", "info": {"positionSide": "SHORT"}}]
        class FakeFetcher:
            ex = FakeExchange()
        res = live_runner._validate_pending_entries_restart_guard(
            FakeFetcher(), {}, {}, results_dir="/tmp/no-write", session_db_path="", run_id="run"
        )
        self.assertFalse(res["ok"])
        od = res["untracked_open_orders"][0]
        self.assertEqual(od["id"], "OID1")
        self.assertEqual(od["side"], "sell")
        self.assertEqual(od["positionSide"], "SHORT")

    def test_restart_guard_reads_raw_info_reduce_only_strings(self):
        class FakeExchange:
            def fetch_open_orders(self):
                return [{"clientOrderId": "CID1", "symbol": "ENA/USDT:USDT", "status": "open", "type": "limit", "info": {"reduceOnly": "true"}}]
        class FakeFetcher:
            ex = FakeExchange()
        res = live_runner._validate_pending_entries_restart_guard(
            FakeFetcher(), {}, {}, results_dir="/tmp/no-write", session_db_path="", run_id="run"
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["open_orders"], [])

    def test_stop_kill_blocks_dca_but_close_still_routes(self):
        for guard in ("STOP_NEW_ORDERS", "KILL"):
            with tempfile.TemporaryDirectory() as td:
                Path(td, guard).write_text("operator", encoding="utf-8")
                key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
                rec = {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0}
                dca_calls = []
                close_calls = []
                with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
                    live_runner,
                    "_execute_open_with_rollback",
                    lambda *args, **kwargs: dca_calls.append(kwargs) or (True, {}),
                ), _PatchAttr(live_runner, "_record_order", lambda *args, **kwargs: None), _PatchAttr(
                    live_runner,
                    "_emit_runtime_debug",
                    lambda *args, **kwargs: None,
                ):
                    ok_dca = live_runner._maybe_apply_manage_result(
                        object(), key, rec, self.row,
                        _RunnerStrategy(manage_result=SimpleNamespace(action="DCA", delta_qty=0.5, order_type="market")),
                        {key: dict(rec)}, td, "hedge", "", "bot", "run",
                    )
                with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
                    live_runner,
                    "_execute_reduce_with_rollback",
                    lambda *args, **kwargs: close_calls.append(kwargs) or (True, {}),
                ):
                    ok_close = live_runner._maybe_apply_manage_result(
                        object(), key, rec, self.row,
                        _RunnerStrategy(manage_result=SimpleNamespace(action="SL")),
                        {key: dict(rec)}, td, "hedge", "", "bot", "run",
                    )
                self.assertFalse(ok_dca)
                self.assertEqual(dca_calls, [])
                self.assertTrue(ok_close)
                self.assertEqual(len(close_calls), 1)

    def test_entry_backoff_blocks_open_and_dca_but_not_close(self):
        key = live_runner.pos_key("HYPE/USDT:USDT", "LONG")
        rec = {"symbol": "HYPE/USDT:USDT", "side": "LONG", "qty": 2.0, "entry": 40.0}
        blocked_strategy = _RunnerStrategy(entry_signal=SimpleNamespace(qty=1.0), can_submit=False)
        open_calls = []
        dca_calls = []
        close_calls = []
        with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
            live_runner,
            "_execute_open_with_rollback",
            lambda *args, **kwargs: open_calls.append(kwargs) or (True, {}),
        ), _PatchAttr(live_runner, "_record_order", lambda *args, **kwargs: None), _PatchAttr(
            live_runner,
            "_emit_runtime_debug",
            lambda *args, **kwargs: None,
        ):
            ok_open = live_runner._attempt_entry(
                object(), "HYPE/USDT:USDT", "LONG", blocked_strategy, self.row, {},
                "/tmp/no-write", "hedge", "", "bot", "run", notional_long=100.0, notional_short=100.0,
            )
        with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
            live_runner,
            "_execute_open_with_rollback",
            lambda *args, **kwargs: dca_calls.append(kwargs) or (True, {}),
        ), _PatchAttr(live_runner, "_record_order", lambda *args, **kwargs: None), _PatchAttr(
            live_runner,
            "_emit_runtime_debug",
            lambda *args, **kwargs: None,
        ):
            ok_dca = live_runner._maybe_apply_manage_result(
                object(), key, rec, self.row,
                _RunnerStrategy(manage_result=SimpleNamespace(action="DCA", delta_qty=0.5, order_type="market"), can_submit=False),
                {key: dict(rec)}, "/tmp/no-write", "hedge", "", "bot", "run",
            )
        with _PatchAttr(live_runner, "_choose_requested_price", lambda fetcher, sym, fallback: fallback), _PatchAttr(
            live_runner,
            "_execute_reduce_with_rollback",
            lambda *args, **kwargs: close_calls.append(kwargs) or (True, {}),
        ):
            ok_close = live_runner._maybe_apply_manage_result(
                object(), key, rec, self.row,
                _RunnerStrategy(manage_result=SimpleNamespace(action="SL"), can_submit=False),
                {key: dict(rec)}, "/tmp/no-write", "hedge", "", "bot", "run",
            )
        self.assertFalse(ok_open)
        self.assertFalse(ok_dca)
        self.assertTrue(ok_close)
        self.assertEqual(open_calls, [])
        self.assertEqual(dca_calls, [])
        self.assertEqual(len(close_calls), 1)

    def test_order_strategy_state_ledger_and_slippage_telemetry_consistency(self):
        old_db = live_runner.LIVE_SESSION_DB_PATH
        old_run = live_runner.LIVE_RUN_ID
        old_results = live_runner.LIVE_RESULTS_DIR
        old_fee = live_runner.LIVE_FEE_RATE
        old_cum = live_runner.LIVE_REALIZED_PNL_CUM
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "session.sqlite")
            live_runner.LIVE_SESSION_DB_PATH = db_path
            live_runner.LIVE_RUN_ID = "run"
            live_runner.LIVE_RESULTS_DIR = td
            live_runner.LIVE_FEE_RATE = 0.0
            live_runner.LIVE_REALIZED_PNL_CUM = 0.0
            try:
                live_runner.ensure_orders_db(db_path)
                live_runner.ensure_strategy_state_events_db(db_path)
                live_runner.ensure_live_pnl_ledger_db(db_path)
                live_runner.ensure_microstructure_tables(db_path)
                bar_dt = live_runner._dt.datetime.fromisoformat(self.row["datetime_utc"])
                live_runner._record_order(db_path, bar_time=bar_dt, symbol="HYPE/USDT:USDT", side="LONG", type_="OPEN", price=40.0, qty=1.0, status="FILLED", reason="open", run_id="run", exchange_order_id="O1")
                live_runner._record_strategy_state_transition(strat=_RunnerStrategy(), sym="HYPE/USDT:USDT", event="open", qty=1.0, entry=40.0, fill_price=40.0, delta_qty=1.0, bar_time=bar_dt)
                live_runner._record_live_pnl_close(session_db_path=db_path, results_dir=td, run_id="run", bar_time=bar_dt, symbol="HYPE/USDT:USDT", side="LONG", qty=1.0, entry_price=40.0, fill_price=41.0, strategy_event="close", exchange_order_id="C1", order_id="O1")
                live_runner.record_fill_observation(db_path, run_id="run", bar_time_utc=bar_dt.isoformat(), symbol="HYPE/USDT:USDT", strategy_side="LONG", order_action="OPEN", qty=1.0, requested_price=40.0, fill_price=40.1, pre_snapshot={})
                con = sqlite3.connect(db_path)
                try:
                    orders_n = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
                    state_n = con.execute("SELECT COUNT(*) FROM strategy_state_events").fetchone()[0]
                    ledger_n = con.execute("SELECT COUNT(*) FROM live_pnl_ledger").fetchone()[0]
                    slip_n = con.execute("SELECT COUNT(*) FROM slippage_observations").fetchone()[0]
                finally:
                    con.close()
                self.assertEqual((orders_n, state_n, ledger_n, slip_n), (1, 1, 1, 1))
            finally:
                live_runner.LIVE_SESSION_DB_PATH = old_db
                live_runner.LIVE_RUN_ID = old_run
                live_runner.LIVE_RESULTS_DIR = old_results
                live_runner.LIVE_FEE_RATE = old_fee
                live_runner.LIVE_REALIZED_PNL_CUM = old_cum

    def test_live_candidate_strategy_classes_expose_execution_hooks(self):
        config_paths = [
            "obw_platform/configs/V21_maxxing_bingx_live_candidate_1m_1y.yaml",
            "obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml",
            "obw_platform/configs/final_best_ena_1y_pack_04-14_bingx_live_v18_warmup_minOrderUSDT2_limit_maker_STRICT_LIFO_BE.yaml",
            "obw_platform/configs/STRICT_LIFO_BE_static9p38_FAST_V2.yaml",
        ]
        class_paths = set()
        for rel in config_paths:
            for line in Path(rel).read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(("strategy_class_long:", "strategy_class_short:")):
                    class_paths.add(line.split(":", 1)[1].strip())
        missing = []
        for class_path in sorted(class_paths):
            module_path, class_name = class_path.rsplit(".", 1)
            if module_path.startswith("strategies."):
                module_path = "obw_platform." + module_path
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            inst = cls({"strategy_params_long": {}, "strategy_params_short": {}, "portfolio": {}})
            for hook in ("get_execution_policy", "get_execution_backoff", "can_submit_order", "can_submit_close_order", "set_broker_min_order_usdt"):
                if not callable(getattr(inst, hook, None)):
                    missing.append(f"{class_path}.{hook}")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
