#!/usr/bin/env python3
"""Deterministic HYPE DCA backtester verification.

Research-only. Uses synthetic local candles and hand-computed expectations.
"""
from __future__ import annotations

import math
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
import sys

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_hype_dca_parameter_search import Candidate, simulate_position, summarize  # noqa: E402


@dataclass(frozen=True)
class SyntheticPosition:
    id: str
    symbol: str
    side: str
    opened: datetime
    closed: datetime
    entry: float
    exit: float


def ts_minute(n: int) -> int:
    return 1_700_000_000_000 + n * 60_000


def assert_close(name: str, got: float, expected: float, tol: float = 1e-9) -> None:
    if not math.isclose(got, expected, rel_tol=tol, abs_tol=tol):
        raise AssertionError(f"{name}: got {got:.12f}, expected {expected:.12f}")


def arrays() -> Dict[str, np.ndarray]:
    return {
        "t": np.array([ts_minute(i) for i in range(5)], dtype=np.int64),
        "high": np.array([101.0, 100.0, 99.0, 101.0, 103.0], dtype=float),
        "low": np.array([90.0, 98.5, 96.8, 95.0, 101.0], dtype=float),
        "close": np.array([90.0, 98.8, 96.9, 101.0, 102.0], dtype=float),
    }


def main() -> None:
    initial_equity = 500.0
    pos = SyntheticPosition(
        id="synthetic-long",
        symbol="HYPEUSDT",
        side="LONG",
        opened=datetime.fromtimestamp(ts_minute(0) / 1000, tz=timezone.utc),
        closed=datetime.fromtimestamp(ts_minute(4) / 1000, tz=timezone.utc),
        entry=100.0,
        exit=102.0,
    )
    candidate = Candidate(
        name="synthetic",
        target_notional=100.0,
        base_frac=0.5,
        steps_pct=(1.0, 2.0),
        add_weights=(1.0, 1.0),
        fee=0.0,
        slippage=0.0,
    )

    strict = simulate_position(pos, candidate, arrays(), fill_mode="close_beyond_skip_boundary")
    if strict is None:
        raise AssertionError("strict simulation returned None")
    level1 = 99.0
    level2 = 97.02
    qty = 50.0 / 100.0 + 25.0 / level1 + 25.0 / level2
    avg_entry = 100.0 / qty
    expected_pnl = (102.0 / avg_entry - 1.0) * 100.0
    expected_min_mtm = (95.0 / avg_entry - 1.0) * 100.0

    if int(strict["dca_fills"]) != 2:
        raise AssertionError(f"strict dca_fills: got {strict['dca_fills']}, expected 2")
    assert_close("avg_entry", float(strict["avg_entry"]), avg_entry)
    assert_close("pnl", float(strict["pnl"]), expected_pnl)
    assert_close("min_mtm", float(strict["min_mtm"]), expected_min_mtm)

    summary = summarize([strict], initial_equity)
    assert_close("net_pct", float(summary["net_pct"]), 100.0 * expected_pnl / initial_equity)
    assert_close(
        "min_trade_mtm_pct_equity",
        float(summary["min_trade_mtm_pct_equity"]),
        100.0 * expected_min_mtm / initial_equity,
    )

    touch = simulate_position(pos, candidate, arrays(), fill_mode="touch")
    if touch is None:
        raise AssertionError("touch simulation returned None")
    touch_fills = json.loads(str(touch["fills_json"]))
    strict_fills = json.loads(str(strict["fills_json"]))
    if int(touch_fills[0]["t"]) != ts_minute(0):
        raise AssertionError("touch mode did not fill on the open boundary fixture")
    if int(strict_fills[0]["t"]) != ts_minute(1):
        raise AssertionError("strict mode did not skip the open boundary fixture")

    protected_candidate = replace(
        candidate,
        name="synthetic-protected",
        protection_account_loss_stop_pct_of_margin=1.0,
        protection_floating_pnl_stop_pct_of_margin=1.0,
        protection_emergency_account_loss_pct_of_margin=2.0,
        protection_confirm_bars=1,
    )
    protected = simulate_position(
        pos,
        protected_candidate,
        arrays(),
        fill_mode="close_beyond_skip_boundary",
        protection_reference_usd=100.0,
    )
    if protected is None:
        raise AssertionError("protected simulation returned None")
    if int(protected["dca_fills"]) != 1:
        raise AssertionError(f"protected dca_fills: got {protected['dca_fills']}, expected 1")
    if str(protected["protection_triggered"]) != "True":
        raise AssertionError("protection did not trigger before the second DCA fill")
    if int(protected["protection_blocked_dca_fills"]) != 1:
        raise AssertionError(
            f"blocked dca fills: got {protected['protection_blocked_dca_fills']}, expected 1"
        )
    protected_fills = json.loads(str(protected["fills_json"]))
    if len(protected_fills) != 1 or int(protected_fills[0]["t"]) != ts_minute(1):
        raise AssertionError("protection should leave the first strict DCA fill intact and block the second")

    print("OK synthetic HYPE DCA formulas/fills/MTM match hand-computed expectations")


if __name__ == "__main__":
    main()
