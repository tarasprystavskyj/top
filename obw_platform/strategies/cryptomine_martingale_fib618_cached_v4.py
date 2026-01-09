# -*- coding: utf-8 -*-
"""
cryptomine_martingale_fib618_cached_v4.py

v4 (updated): makes caching actually work end-to-end + speeds up fallback.

What was slow:
- When DB cached columns (pmin_roll_*, atr_htf_pct_*) are NOT present in the row dict,
  the strategy falls back to runtime rolling-min:
      min(buffer_of_43200)  # O(43200) each bar
  This dominates runtime.

What this update adds (WITHOUT changing trading logic):
1) required_db_columns(cfg): so the backtester can SELECT the cached columns.
2) O(1) amortized rolling-min fallback (monotonic deque) if cache is missing/corrupt.
3) A tiny guard so double-calls in the same timestamp don't advance the deque index.

Place this file to:
  obw_platform/strategies/cryptomine_martingale_fib618_cached_v4.py
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional

from strategies.cryptomine_martingale_fib618 import CryptomineMartingaleFib618V2


class CryptomineMartingaleFib618V2Cached(CryptomineMartingaleFib618V2):
    @classmethod
    def required_db_columns(cls, cfg: Dict[str, Any]) -> List[str]:
        """
        Tell the backtester which DB columns are needed for the cached fast-path.

        We mirror strategy defaults, because cfg may omit these fields.
        """
        sp = cfg.get("strategy_params", {}) or {}

        pmin_lb = int(sp.get("pminLookbackBars", 43200))
        htf_bars = int(sp.get("atrHtfBars", 60))
        atr_len = int(sp.get("atrLen", 14))

        cols = ["open", "high", "low", "close"]
        if pmin_lb > 0:
            cols.append(f"pmin_roll_{pmin_lb}")
        if htf_bars > 0 and atr_len > 0:
            cols.append(f"atr_htf_pct_{htf_bars}_{atr_len}")
            cols.append(f"is_htf_close_{htf_bars}")

        # Optional liquidity/diagnostics (safe if present)
        cols += ["qv_24h", "quote_volume", "dp6h", "dp12h", "atr_ratio"]

        # unique preserve order
        out: List[str] = []
        seen = set()
        for c in cols:
            if c not in seen:
                out.append(c)
                seen.add(c)
        return out

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)

        # Cached DB keys
        self.pmin_key = f"pmin_roll_{self.pmin_lookback_bars}"
        self.atr_key = f"atr_htf_pct_{self.htf_bars}_{self.atr_len}"
        self.htf_flag_key = f"is_htf_close_{self.htf_bars}"

        # Monotonic rolling-min fallback state (only used if cache missing)
        self._pmin_dq: Dict[str, deque] = {}
        self._pmin_idx: Dict[str, int] = {}
        self._pmin_last_ts: Dict[str, Any] = {}

    # -------------------- fast fallback: rolling-min (O(1) amortized) --------------------

    def _update_pmin_flat_fast(self, sym: str, low: float, ts: Optional[Any] = None) -> None:
        """
        Same output as the slow path (rolling min), but without min(buf) each bar.
        """
        if self.pmin_lookback_bars <= 0:
            # Running min since start
            prev = self._pmin.get(sym)
            self._pmin[sym] = float(low) if prev is None else (float(low) if low < prev else prev)
            return

        # Don't advance bar index if called twice for same timestamp
        if ts is not None and self._pmin_last_ts.get(sym) == ts:
            i = self._pmin_idx.get(sym, 0)
        else:
            self._pmin_last_ts[sym] = ts
            i = self._pmin_idx.get(sym, -1) + 1
            self._pmin_idx[sym] = i

        dq = self._pmin_dq.get(sym)
        if dq is None:
            dq = deque()
            self._pmin_dq[sym] = dq

        low_f = float(low)

        # Maintain monotonic increasing deque by value
        while dq and dq[-1][1] >= low_f:
            dq.pop()
        dq.append((i, low_f))

        # Expire old indices
        expire = i - int(self.pmin_lookback_bars) + 1
        while dq and dq[0][0] < expire:
            dq.popleft()

        self._pmin[sym] = dq[0][1] if dq else low_f

    def entry_signal(self, is_opening: bool, sym: str, row: Dict[str, Any], ctx=None):
        low = float(row.get("low") if row.get("low") is not None else row.get("close"))
        ts = row.get("datetime_utc") or row.get("t")

        # pmin cache
        pmin_used = False
        pmin_val = row.get(self.pmin_key)
        if pmin_val is not None:
            try:
                self._pmin[sym] = float(pmin_val)
                pmin_used = True
            except Exception:
                pmin_used = False
        if not pmin_used:
            self._update_pmin_flat_fast(sym, low, ts=ts)

        # ATR cache (only update on HTF close if flag exists; else accept per-row)
        atr_used = False
        atr_val = row.get(self.atr_key)
        if atr_val is not None:
            try:
                is_htf = row.get(self.htf_flag_key)
                if is_htf is None or int(is_htf) == 1:
                    self._atr_pct[sym] = float(atr_val)
                atr_used = True
            except Exception:
                atr_used = False
        if not atr_used:
            self._update_htf_atr(sym, row)

        return super().entry_signal(is_opening, sym, row, ctx=ctx)

    def manage_position(self, sym: str, row: Dict[str, Any], pos, ctx=None):
        low = float(row.get("low") if row.get("low") is not None else row.get("close"))
        ts = row.get("datetime_utc") or row.get("t")

        # pmin cache
        pmin_used = False
        pmin_val = row.get(self.pmin_key)
        if pmin_val is not None:
            try:
                self._pmin[sym] = float(pmin_val)
                pmin_used = True
            except Exception:
                pmin_used = False
        if not pmin_used:
            self._update_pmin_flat_fast(sym, low, ts=ts)

        # ATR cache
        atr_used = False
        atr_val = row.get(self.atr_key)
        if atr_val is not None:
            try:
                is_htf = row.get(self.htf_flag_key)
                if is_htf is None or int(is_htf) == 1:
                    self._atr_pct[sym] = float(atr_val)
                atr_used = True
            except Exception:
                atr_used = False
        if not atr_used:
            self._update_htf_atr(sym, row)

        return super().manage_position(sym, row, pos, ctx=ctx)
