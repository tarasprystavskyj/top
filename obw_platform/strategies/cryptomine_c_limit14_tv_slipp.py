# -*- coding: utf-8 -*-
"""
cryptomine_c_limit14_tv_port.py

Python strategy port that more closely matches the TradingView PineScript
"C - limit 14" behavior:

- DCA fills are triggered by intrabar LOW crossing the next buy level, and fill
  at the level price (not at close).
- Multiple DCA fills per bar are possible (bounded by `fills` and `max_steps`).
- Multiple sub-sells per bar are possible (bounded by `max_sub_sells_per_bar`).
  Since the OBW backtester consumes at most one exit signal per bar, we aggregate
  same-bar partial sells into a single TP_PARTIAL with correct notional/qty and
  adjust the effective entry for realized PnL attribution.
- Optional per-bar signal throttling (max_signals_window over window_minutes),
  like Pine's "maxSignalsWindow" protection.

This file is designed to be drop-in compatible with existing YAML keys used in
your tuner runs: tp, subtp, cb, lin-drop, fills, d1..d5, m2..m5.

Notes / limitations:
- Pine can close and re-open within the same bar. Here we restart on the NEXT
  bar after a full exit to fit the backtester loop model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple, List
from collections import deque
from math import isfinite
from datetime import datetime

# The OBW platform typically provides these base types.
try:
    from strategies.base_strategy import BaseStrategy
    from strategies.signals import EntrySignal, ExitSignal
except Exception:  # pragma: no cover
    BaseStrategy = object  # type: ignore
    EntrySignal = object  # type: ignore
    ExitSignal = object  # type: ignore


def _safe_float(x, default: float) -> float:
    try:
        v = float(x)
        if not isfinite(v):
            return default
        return v
    except Exception:
        return default


def _parse_pct(x, default_pct: float) -> float:
    # accepts either 0.2 (meaning 0.2%) or "0.2"
    return _safe_float(x, default_pct)


def _bar_time_from_row(row: Dict) -> Optional[int]:
    for k in ("t", "ts", "time", "timestamp", "open_time", "open_ts", "datetime", "date"):
        if k in row:
            v = row[k]
            try:
                if isinstance(v, (int, float)):
                    return int(v)
                if isinstance(v, datetime):
                    return int(v.timestamp() * 1000)
                s = str(v)
                # ISO string?
                if "T" in s:
                    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
                return int(float(s))
            except Exception:
                continue
    return None


@dataclass
class _Lot:
    entry: float
    qty: float


@dataclass
class _State:
    # position decomposition into "lots" for sub-sell logic
    lots: List[_Lot] = field(default_factory=list)

    # trailing TP for whole position
    tp_hit: bool = False
    tp_peak: float = 0.0

    # signal throttling
    sig_window: Deque[Tuple[int, int]] = field(default_factory=deque)  # (bar_ts_ms, count)

    # restart control
    restart_next_bar: bool = False


class CryptomineCLimit14TVPort(BaseStrategy):
    """
    Strategy name for YAML:
      strategy: cryptomine_c_limit14_tv_port.CryptomineCLimit14TVPort
    """
    name = "cryptomine_c_limit14_tv_port"

    def __init__(self, cfg: Dict):
        super().__init__(cfg)

        sp = (cfg.get("strategy_params") or {}) if isinstance(cfg, dict) else {}

        # --- Core parameters (Pine naming / tuner naming) ---
        self.first_buy_usdt = _safe_float(sp.get("first_buy_usdt", sp.get("first_buy", 5.0)), 5.0)

        # Whole TP and trailing callback (Pine: TP % (Whole warehouse), Callback % (Trailing))
        self.tp_pct = _parse_pct(sp.get("tp", sp.get("tp_pct", 1.1)), 1.1)
        self.cb_pct = _parse_pct(sp.get("cb", sp.get("callback", 0.2)), 0.2)

        # Sub-sell TP for last lot (Pine: "Sub-sell TP % (last lot)")
        self.subtp_pct = _parse_pct(sp.get("subtp", sp.get("sub_tp_pct", 0.5)), 0.5)

        # Max buys / steps
        self.max_steps = int(_safe_float(sp.get("max_steps", sp.get("max_buys", 244)), 244))

        # After level 5, margin call drop percent (linear step)
        self.lin_drop_pct = _parse_pct(sp.get("lin-drop", sp.get("linear_drop", 0.5)), 0.5)
        # --- Slippage-adaptive drop (proxy via dp6h) ---
        self.slip_window = int(sp.get('slip-window', 100))
        self.slip_floor  = float(sp.get('slip-floor', 1.0))  # 1.0 = no change
        self.slip_cap    = float(sp.get('slip-cap', 3.0))    # max multiplier
        self._slip_hist  = deque(maxlen=self.slip_window)

        # Max DCA fills per bar
        self.max_fills_per_bar = int(_safe_float(sp.get("fills", sp.get("max_fills_per_bar", 7)), 7))

        # Max sub-sells per bar (Pine has it hardcoded; we expose it)
        self.max_sub_sells_per_bar = int(_safe_float(sp.get('max_sub_sells_per_bar', 7), 7))

        # Nonlinear drops (levels 2..6) in percent
        self.drops = [
            _parse_pct(sp.get("d1", sp.get("drop2", 0.3)), 0.3),
            _parse_pct(sp.get("d2", sp.get("drop3", 0.4)), 0.4),
            _parse_pct(sp.get("d3", sp.get("drop4", 0.6)), 0.6),
            _parse_pct(sp.get("d4", sp.get("drop5", 0.8)), 0.8),
            _parse_pct(sp.get("d5", sp.get("drop6", 0.8)), 0.8),
        ]

        # Multipliers for lot sizes (levels 2..6). Pine pattern example: 1x,1.5x,2x,2.5x,3x,1x
        self.multipliers = [
            1.0,
            _safe_float(sp.get("m2", sp.get("mult2", 1.5)), 1.5),
            _safe_float(sp.get("m3", sp.get("mult3", 2.0)), 2.0),
            _safe_float(sp.get("m4", sp.get("mult4", 2.5)), 2.5),
            _safe_float(sp.get("m5", sp.get("mult5", 3.0)), 3.0),
            1.0,
        ]

        # Auto-merge profit into next entry (Pine "Auto Merge")
        self.auto_merge = bool(sp.get("auto_merge", True))

        # Signal throttle (like Pine)
        self.window_minutes = int(_safe_float(sp.get("window_minutes", 60), 60))
        self.max_signals_window = int(_safe_float(sp.get("max_signals_window", 70), 70))

        # Safety: negative drops mean "buy on rise" and can cause instant multi-fills each bar.
        # Allow it, but you can clamp in tuner plan if needed.
        self.allow_negative_drops = bool(sp.get("allow_negative_drops", True))

        # Internal state per symbol
        self._st: Dict[str, _State] = {}

    # ----------------- helpers -----------------

    def _get_state(self, sym: str) -> _State:
        st = self._st.get(sym)
        if st is None:
            st = _State()
            self._st[sym] = st
        return st

    def _prune_window(self, st: _State, bar_ts_ms: int) -> None:
        cutoff = bar_ts_ms - self.window_minutes * 60 * 1000
        while st.sig_window and st.sig_window[0][0] < cutoff:
            st.sig_window.popleft()

    def _sig_used(self, st: _State) -> int:
        return sum(c for _, c in st.sig_window)

    def _can_signal(self, st: _State, n: int, bar_ts_ms: int) -> bool:
        self._prune_window(st, bar_ts_ms)
        return (self._sig_used(st) + n) <= self.max_signals_window

    def _register_signal(self, st: _State, n: int, bar_ts_ms: int) -> None:
        if n <= 0:
            return
        # merge with same bar
        if st.sig_window and st.sig_window[-1][0] == bar_ts_ms:
            ts, c = st.sig_window.pop()
            st.sig_window.append((ts, c + n))
        else:
            st.sig_window.append((bar_ts_ms, n))

def _slip_factor(self, row: Optional[dict]) -> float:
    """Return multiplier for drop spacing.
    We don't have candle OHLC here, so we use dp6h (6h % change) as a proxy for 'panic move'.
    When dp6h is sharply negative relative to recent history, we widen drops so buys land lower.
    """
    if not row:
        return 1.0
    dp6h = row.get("dp6h", 0.0)
    try:
        dp6h = float(dp6h)
    except Exception:
        dp6h = 0.0
    proxy = max(0.0, -dp6h)  # only downside intensity
    if proxy <= 0:
        return 1.0

    # Update history on actual buy triggers (done in entry_signal), but keep a fallback here.
    if len(self._slip_hist) >= 5:
        avg = sum(self._slip_hist) / max(1, len(self._slip_hist))
    else:
        avg = proxy

    # If avg is tiny, avoid exploding.
    denom = max(1e-12, avg)
    raw = proxy / denom

    # Clamp multiplier.
    mult = min(self.slip_cap, max(self.slip_floor, raw))
    return mult

    def _next_drop_pct(self, step: int, row: Optional[dict] = None) -> float:
        """
        step is 1-based count of lots after the NEXT buy.
        For buy #2..#6 use nonlinear drops; beyond that use linear drop.
        """
        if step <= 1:
            return 0.0
        idx = step - 2  # step 2 -> idx 0
        if 0 <= idx < len(self.drops):
            d = self.drops[idx]
        else:
            d = self.lin_drop_pct * self._slip_factor(row)
        if (not self.allow_negative_drops) and d < 0:
            d = 0.0
        return d

    def _mult_for_step(self, step: int) -> float:
        # step 1..6 use multipliers list; after 6 keep last known (1.0 by default)
        if 1 <= step <= len(self.multipliers):
            return float(self.multipliers[step - 1])
        return float(self.multipliers[-1])

    # ----------------- strategy API -----------------

    def entry_signal(self, is_live: bool, sym: str, row: Dict, ctx=None):
        """
        Open new position (one "cycle") if no position is open.
        """
        st = self._get_state(sym)
        bar_ts_ms = _bar_time_from_row(row) or 0

        # If we flagged restart_next_bar after a full exit, allow now.
        if getattr(ctx, "pos", None) is not None:
            # Backtester will call manage_position instead.
            return None

        if st.restart_next_bar is True:
            st.restart_next_bar = False  # allow entry now

        # Basic throttle (1 signal)
        if not self._can_signal(st, 1, bar_ts_ms):
            return None

        price = _safe_float(row.get("close"), 0.0)
        if price <= 0:
            return None

        qty = self.first_buy_usdt / price
        if qty <= 0:
            return None

        # Initialize lots
        st.lots = [_Lot(entry=price, qty=qty)]
        st.tp_hit = False
        st.tp_peak = 0.0

        self._register_signal(st, 1, bar_ts_ms)

        # Create entry
        try:
            return EntrySignal(side="LONG", qty=qty, price=price)
        except Exception:
            # If your platform uses different signature, keep compatibility by returning a dict
            # record downside intensity at buy time (dp6h proxy)
            dp6h = float(row.get("dp6h", 0.0) or 0.0)
            proxy = max(0.0, -dp6h)
            if proxy > 0:
                self._slip_hist.append(proxy)

            return {"side": "LONG", "qty": qty, "price": price}

    def manage_position(self, is_live: bool, sym: str, row: Dict, pos, ctx=None):
        """
        Manage open position: full TP trailing, sub-sells, and DCA fills.
        """
        st = self._get_state(sym)
        bar_ts_ms = _bar_time_from_row(row) or 0

        close = _safe_float(row.get("close"), 0.0)
        high = _safe_float(row.get("high", close), close)
        low = _safe_float(row.get("low", close), close)
        if close <= 0:
            return None

        # --- FULL TP trailing ---
        # tp_price uses average entry (pos.entry). Trailing armed once price hits tp_price (on close, like Pine).
        tp_price = pos.entry * (1.0 + self.tp_pct / 100.0)
        if not st.tp_hit:
            if close >= tp_price and self._can_signal(st, 1, bar_ts_ms):
                st.tp_hit = True
                st.tp_peak = close
                self._register_signal(st, 1, bar_ts_ms)
        else:
            # update peak (Pine uses high of subsequent closes; we approximate with 'high')
            st.tp_peak = max(st.tp_peak, high)
            trail_stop = st.tp_peak * (1.0 - self.cb_pct / 100.0)
            if close <= trail_stop and self._can_signal(st, 1, bar_ts_ms):
                # exit full position
                self._register_signal(st, 1, bar_ts_ms)
                st.lots.clear()
                st.tp_hit = False
                st.tp_peak = 0.0
                st.restart_next_bar = True  # Pine can restart same bar; we restart next bar.
                try:
                    return ExitSignal(reason="TP_FULL", qty=pos.qty, price=close, take_profit=close, stop_price=close)
                except Exception:
                    return {"reason": "TP_FULL", "qty": pos.qty, "price": close, "take_profit": close, "stop_price": close}

        # --- SUB-SELLS (aggregate) ---
        # Pine sells the "last lot" when it reaches subtp, potentially multiple times per bar.
        sold_qty = 0.0
        sold_cost = 0.0
        sold_profit = 0.0
        sold_count = 0

        # Need enough lots to sub-sell (it can also sub-sell when only 1 lot exists; keep it allowed)
        while st.lots and sold_count < self.max_sub_sells_per_bar:
            last = st.lots[-1]
            subtp_price = last.entry * (1.0 + self.subtp_pct / 100.0)
            if close < subtp_price:
                break
            # throttle check for the next individual sell
            if not self._can_signal(st, 1, bar_ts_ms):
                break

            # execute sell of entire last lot at close
            st.lots.pop()
            sold_qty += last.qty
            sold_cost += last.entry * last.qty
            sold_profit += (close - last.entry) * last.qty
            sold_count += 1
            self._register_signal(st, 1, bar_ts_ms)

        if sold_qty > 0 and pos.qty > 0:
            # Apply auto-merge: reduce next entry notional by realized profit from subsells (like Pine).
            if self.auto_merge and st.lots:
                # decrease the next (last) lot entry "notional" by profit by reducing its effective entry price
                # we keep qty fixed and adjust entry down (increasing future TP chance).
                last = st.lots[-1]
                if last.qty > 0:
                    # distribute profit onto last lot by lowering its entry price by profit/qty
                    last.entry = max(1e-12, last.entry - (sold_profit / last.qty))

            # Recompute pos.entry as VWAP of remaining lots for subsequent MTM & TP
            if st.lots:
                tot_qty = sum(l.qty for l in st.lots)
                tot_cost = sum(l.entry * l.qty for l in st.lots)
                if tot_qty > 0:
                    pos.entry = tot_cost / tot_qty

            # To attribute realized PnL of this aggregated partial exit correctly in the backtester,
            # temporarily set pos.entry to avg entry of SOLD lots (so bt uses close - entry).
            avg_entry_sold = sold_cost / sold_qty if sold_qty > 0 else pos.entry
            qty_frac = min(1.0, sold_qty / pos.qty)

            old_entry = pos.entry
            pos.entry = avg_entry_sold
            try:
                sig = ExitSignal(reason="TP_PARTIAL", qty=pos.qty * qty_frac, price=close, take_profit=close, stop_price=close)
            except Exception:
                sig = {"reason": "TP_PARTIAL", "qty": pos.qty * qty_frac, "price": close, "take_profit": close, "stop_price": close}
            # restore pos.entry to remaining-lots vwap (best-effort)
            pos.entry = old_entry
            return sig

        # --- DCA fills (intrabar, at level price) ---
        # Determine current step count
        step = len(st.lots)  # current lots count
        if step <= 0:
            return None

        fills_done = 0
        # last fill anchor price
        last_fill_price = st.lots[-1].entry

        while fills_done < self.max_fills_per_bar and step < self.max_steps:
            next_step = step + 1
            drop_pct = self._next_drop_pct(next_step, row)
            next_level_price = last_fill_price * (1.0 - drop_pct / 100.0)

            # For negative drops, next_level_price > last_fill_price. Condition low <= next_level_price
            # will almost always be true, which means it can fill instantly. That's allowed if enabled.
            if low > next_level_price:
                break

            # Throttle check for this individual buy
            if not self._can_signal(st, 1, bar_ts_ms):
                break

            mult = self._mult_for_step(next_step)
            buy_usdt = self.first_buy_usdt * mult
            qty_add = buy_usdt / next_level_price if next_level_price > 0 else 0.0
            if qty_add <= 0:
                break

            # Add lot at the level price
            st.lots.append(_Lot(entry=next_level_price, qty=qty_add))
            self._register_signal(st, 1, bar_ts_ms)

            # Update pos.entry (VWAP) and pos.qty (backtester should update qty automatically on entry,
            # but we keep pos.entry consistent for TP decisions within same bar).
            step += 1
            fills_done += 1
            last_fill_price = next_level_price

            tot_qty = sum(l.qty for l in st.lots)
            tot_cost = sum(l.entry * l.qty for l in st.lots)
            if tot_qty > 0:
                pos.entry = tot_cost / tot_qty

            # We cannot emit multiple entry signals from manage_position; OBW backtester expects
            # entry fills to be initiated via ctx/order layer. In this platform, DCA is usually
            # represented as increasing position size via an ExitSignal/EntrySignal hybrid or
            # a special "ADD" signal. If your engine supports EntrySignal from manage_position,
            # return it here; otherwise rely on your backtester integration.
            #
            # In your current OBW backtester, DCA buys were implemented by returning an ExitSignal-like
            # "ADD" action. To preserve compatibility, we return a dict with action "ADD".
            #
            # IMPORTANT: returning after first fill means at most 1 fill per bar in engine.
            # If your backtester supports processing multiple adds, adapt the engine accordingly.
            # For now, we aggregate to ONE add per bar by accumulating qty_add_total.
            pass

        # If we did fills, issue ONE aggregated "ADD" with total qty added at weighted price.
        if fills_done > 0:
            # aggregate added lots are the last `fills_done` lots
            added = st.lots[-fills_done:]
            add_qty = sum(l.qty for l in added)
            # we report a representative price (vwap of adds)
            add_cost = sum(l.entry * l.qty for l in added)
            add_price = add_cost / add_qty if add_qty > 0 else close
            try:
                return EntrySignal(side="LONG", qty=add_qty, price=add_price)
            except Exception:
                # record downside intensity at buy time (dp6h proxy)
                dp6h = float(row.get("dp6h", 0.0) or 0.0)
                proxy = max(0.0, -dp6h)
                if proxy > 0:
                    self._slip_hist.append(proxy)
                return {"side": "LONG", "qty": add_qty, "price": add_price, "reason": "DCA_ADD"}

        return None