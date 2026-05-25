from __future__ import annotations

import yaml

from obw_platform.meta_strategies.v21_external_signal_wrapper import (
    V21ExternalSignalLong,
    V21ExternalSignalShort,
    build_v21_external_signal_cfg,
)


def _base_cfg():
    with open("obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _row(ts: str = "2026-05-19T00:00:00Z", close: float = 100.0):
    return {
        "datetime_utc": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "atr_ratio": 0.0,
        "gain_24h_before": 0.0,
        "dp6h": 0.0,
        "vol_surge_mult": 0.0,
    }


def test_v21_external_signal_gates_entry_by_side_and_symbol():
    cfg = build_v21_external_signal_cfg(
        _base_cfg(),
        delegated_capital_usdt=100.0,
        base_order_pct_eq=5.0,
        signals=[
            {
                "symbol": "BTC/USDT:USDT",
                "side": "LONG",
                "start_utc": "2026-05-19T00:00:00Z",
                "expires_utc": "2026-05-20T00:00:00Z",
                "source": "unit",
                "signal_id": "sig1",
            }
        ],
    )
    long_leg = V21ExternalSignalLong(cfg)
    short_leg = V21ExternalSignalShort(cfg)

    assert short_leg.entry_signal(True, "BTC/USDT:USDT", _row(), ctx=None) is None
    assert long_leg.entry_signal(True, "ETH/USDT:USDT", _row(), ctx=None) is None

    sig = long_leg.entry_signal(True, "BTC/USDT:USDT", _row(), ctx=None)
    assert sig is not None
    assert sig.side == "LONG"
    assert sig.qty > 0
    assert "external_signal:unit:sig1" in sig.reason


def test_v21_external_signal_applies_delegated_sizing():
    cfg = build_v21_external_signal_cfg(
        _base_cfg(),
        delegated_capital_usdt=200.0,
        base_order_pct_eq=5.0,
        signals=[{"symbol": "BTC/USDT:USDT", "side": "LONG"}],
    )
    leg = V21ExternalSignalLong(cfg)
    assert leg.delegate.equity_for_sizing == 200.0
    assert leg.delegate.base_order_pct_eq == 5.0
