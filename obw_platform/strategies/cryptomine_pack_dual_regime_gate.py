# -*- coding: utf-8 -*-
"""Dual pack strategy with binding short regime gate.

This module intentionally changes only strategy decisions. It does not alter
backtest execution, slippage, fees, or liquidation/margin accounting.
"""

from __future__ import annotations

from typing import Any, Dict

from strategies.cryptomine_pack_dual_full import (
    CryptomineLongPackAdaptiveEven,
    CryptomineShortPackAdaptiveEven,
    ExitSig,
)


class CryptomineLongPackRegimeGate(CryptomineLongPackAdaptiveEven):
    """Long leg is kept as the established V21 pack implementation."""


class CryptomineShortPackRegimeGate(CryptomineShortPackAdaptiveEven):
    """Short leg with a hard no-new-short/deleverage gate.

    The base V21 strategy already computes a side-aware HTF trend strength:
    for SHORT, low strength means the MA slope is hostile/upward. Existing
    `entryTrendStrengthMin` blocks only new cycles. This subclass also blocks
    DCA and can close/deleverage an existing short bag when the hostile regime
    persists.
    """

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        sp = cfg.get("strategy_params_short", {}) or {}
        self.short_gate_enabled = bool(sp.get("shortGateEnabled", True))
        self.short_gate_entry_strength_min = float(
            sp.get("shortGateEntryStrengthMin", sp.get("entryTrendStrengthMin", 0.0) or 0.0)
        )
        self.short_gate_dca_strength_min = float(sp.get("shortGateDcaStrengthMin", 0.0) or 0.0)
        self.short_gate_close_strength_min = float(sp.get("shortGateCloseStrengthMin", 0.0) or 0.0)
        self.short_gate_close_unreal_pct = float(sp.get("shortGateCloseUnrealPct", 0.0) or 0.0)
        self.short_gate_close_min_fills = int(sp.get("shortGateCloseMinFills", 1) or 1)
        self.short_gate_force_close = bool(sp.get("shortGateForceClose", False))

    def _short_unreal_pct(self, st, close: float) -> float:
        avg = float(st.avg_price or 0.0)
        if avg <= 0.0:
            return 0.0
        return (avg - float(close or 0.0)) / avg * 100.0

    def _gate_update_strength(self, st, t, close: float) -> float:
        try:
            self._update_trend(st, t, float(close or 0.0))
        except Exception:
            pass
        return float(getattr(self, "_last_trend_strength", 100.0) or 0.0)

    def entry_signal(self, is_opening, sym, row, ctx=None):
        if self.short_gate_enabled:
            t = row.get("datetime_utc")
            st = self._get_state(sym)
            _, _, close = self._trigger_prices(row)
            strength = self._gate_update_strength(st, t, close)
            threshold = max(float(self.entry_trend_strength_min or 0.0), self.short_gate_entry_strength_min)
            if threshold > 0.0 and strength < threshold:
                return None
        return super().entry_signal(is_opening, sym, row, ctx=ctx)

    def manage_position(self, sym, row, pos, ctx=None):
        if not self.short_gate_enabled:
            return super().manage_position(sym, row, pos, ctx=ctx)

        t = row.get("datetime_utc")
        st = self._get_state(sym)
        hi, lo, close = self._trigger_prices(row)
        strength = self._gate_update_strength(st, t, close)

        if st.lots and self.short_gate_close_strength_min > 0.0 and strength < self.short_gate_close_strength_min:
            unreal_pct = self._short_unreal_pct(st, close)
            enough_fills = int(st.num_fills or 0) >= self.short_gate_close_min_fills
            loss_bad_enough = (
                self.short_gate_close_unreal_pct <= 0.0
                or unreal_pct <= -abs(self.short_gate_close_unreal_pct)
            )
            if enough_fills and loss_bad_enough and self._can_place_order(t):
                st.reset_pending = True
                st.pos_value_usdt = 0.0
                st.pos_size = 0.0
                st.avg_price = None
                st.num_fills = 0
                st.last_fill_price = None
                st.next_level_price = None
                st.lots = []
                st.trailing_active = False
                st.trailing_ref = None
                st.cycle_start_ts = None
                st.last_fill_ts = None
                self._register_order()
                return ExitSig(
                    action="EXIT",
                    exit_price=close,
                    reason=(
                        f"Short regime gate close strength={strength:.2f} "
                        f"unr={unreal_pct:.2f}%"
                    ),
                )

        old_dca_block = self.dca_block_counter_gain_abs
        try:
            if self.short_gate_dca_strength_min > 0.0 and strength < self.short_gate_dca_strength_min:
                # Use a huge counter-gain threshold effect by making the base DCA risk
                # guard active through the existing path when row gain data exists; for
                # OHLCV-only data, fall back to temporarily disabling fresh DCA by setting
                # max_fills_per_bar to zero for this manage call.
                old_max_fills = self.max_fills_per_bar
                self.max_fills_per_bar = 0
                try:
                    return super().manage_position(sym, row, pos, ctx=ctx)
                finally:
                    self.max_fills_per_bar = old_max_fills
            return super().manage_position(sym, row, pos, ctx=ctx)
        finally:
            self.dca_block_counter_gain_abs = old_dca_block
