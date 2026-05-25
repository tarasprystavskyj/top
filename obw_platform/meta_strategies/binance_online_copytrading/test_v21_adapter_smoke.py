from __future__ import annotations

from datetime import datetime, timezone

from obw_platform.meta_strategies.binance_online_copytrading.binance_online_copytrading import (
    DEFAULT_V21_CONFIG,
    V21OneLegRuntime,
    load_v21_runtime_cfg,
    open_v21_paper,
)


def test_v21_runtime_overrides_active_side_capital():
    cfg = load_v21_runtime_cfg(DEFAULT_V21_CONFIG, "LONG", 123.0)

    assert cfg["strategy_params_long"]["equityForSizingUSDT"] == 123.0
    assert cfg["strategy_params_long"]["baseOrderPctEq"] == 5.0
    assert cfg["strategy_class_long"].endswith("CryptomineLongPackAdaptiveEven")


def test_open_v21_paper_creates_one_real_leg_without_network():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    runtime = V21OneLegRuntime(DEFAULT_V21_CONFIG, 100.0)

    trade, reason = open_v21_paper(
        runtime=runtime,
        strategy_name="lead_smoke",
        lead_name="Smoke Trader",
        portfolio_id="p1",
        mode="contrarian_on_close",
        signal_id="s1",
        symbol="BTCUSDT",
        side="SHORT",
        mark=100000.0,
        mark_source="smoke",
        now=now,
        slippage_bp=0.0,
        ttl_hours=72.0,
        raw_signal={"id": "s1"},
    )

    assert reason is None
    assert trade is not None
    assert trade["lead_trader_name"] == "Smoke Trader"
    assert trade["side"] == "SHORT"
    assert trade["v21"]["class_path"].endswith("CryptomineShortPackAdaptiveEven")
    assert trade["v21"]["delegated_capital_usdt"] == 100.0
    assert trade["v21"]["base_order_pct_eq"] == 5.0
    assert trade["qty"] > 0
    assert trade["notional_usdt"] > 0
