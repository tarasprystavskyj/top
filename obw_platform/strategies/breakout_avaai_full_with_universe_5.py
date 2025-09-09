
# strategies/breakout_avaai_full_with_universe_5.py
# Refactored strategy: ALL entry/exit & TP/SL decisions live here.
# Backtester must only:
#   - apply allow/deny universe for OPENING,
#   - call universe()/rank() to get candidates,
#   - call entry_signal() to open (TP/SL must be provided here),
#   - call manage_position() to close,
#   - optionally print "heat" (purely reporting; not used for decisions).
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Literal, Mapping, Any, List, Dict, Tuple

import numpy as np
import pandas as pd

Side = Literal["LONG","SHORT"]
ExitAction = Literal["HOLD","TP","SL","EXIT"]

@dataclass
class Sig:
    side: Side
    take_profit: float
    stop_price: float
    confidence: float = 0.0
    size: Optional[float] = None
    reason: Optional[str] = None
    tags: Optional[List[str]] = None
    heat: Optional[float] = None

    # --- aliases for compatibility ---
    @property
    def tp(self): return self.take_profit
    @property
    def tp_price(self): return self.take_profit
    @property
    def sl(self): return self.stop_price
    @property
    def sl_price(self): return self.stop_price

@dataclass
class ExitSig:
    action: ExitAction
    exit_price: Optional[float] = None
    reason: Optional[str] = None

def _f(x, default=0.0) -> float:
    try: return float(x)
    except Exception: return float(default)


def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _adx(high, low, close, n: int = 14):
    # простий ADX без сторонніх бібліотек
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.DataFrame({"tr1": tr1, "tr2": tr2, "tr3": tr3}).max(axis=1)
    atr = tr.rolling(n).mean()
    plus_di = 100 * pd.Series(plus_dm, index=high.index).rolling(n).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=high.index).rolling(n).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    return dx.rolling(n).mean().fillna(0)


def pick_side(df: pd.DataFrame, cfg: Mapping) -> str | None:
    """
    Вибирає сторону по голосах:
    +1 за LONG, -1 за SHORT. Якщо |score| < votes_needed — сигнал пропускаємо.
    """
    c = cfg["side_filter"]
    close = df["close"]
    high = df["high"]
    low = df["low"]
    vol = df.get("volume", pd.Series(index=df.index, data=np.nan))

    ema_f = _ema(close, c["ema_fast"])
    ema_s = _ema(close, c["ema_slow"])
    vwap = ((high + low + close) / 3 * vol).rolling(c["vwap_len"]).sum() / vol.rolling(c["vwap_len"]).sum()
    atr = (high.combine(low, max) - low.combine(high, min)).rolling(c["atr_len"]).mean()

    adx = _adx(high, low, close, c["adx_len"])

    z = (close - close.rolling(c["z_len"]).mean()) / (atr.replace(0, np.nan))
    z = z.replace([np.inf, -np.inf], np.nan).fillna(0)

    htf_ema_f = _ema(close, c["htf_ema_fast"])
    htf_ema_s = _ema(close, c["htf_ema_slow"])

    score = 0

    if ema_f.iloc[-1] > ema_s.iloc[-1] and ema_f.diff().iloc[-1] > 0:
        score += 1
    if ema_f.iloc[-1] < ema_s.iloc[-1] and ema_f.diff().iloc[-1] < 0:
        score -= 1

    if close.iloc[-1] > vwap.iloc[-1]:
        score += 1
    elif close.iloc[-1] < vwap.iloc[-1]:
        score -= 1

    if z.iloc[-1] >= c["z_long_min"]:
        score += 1
    if z.iloc[-1] <= -c["z_short_min"]:
        score -= 1

    if adx.iloc[-1] < c["min_adx"]:
        score = np.sign(score) * max(0, abs(score) - 1)

    if c.get("htf_trend_filter", True):
        if htf_ema_f.iloc[-1] > htf_ema_s.iloc[-1]:
            if score < 0:
                score += 1
        elif htf_ema_f.iloc[-1] < htf_ema_s.iloc[-1]:
            if score > 0:
                score -= 1

    sym = df.attrs.get("symbol")
    overrides = c.get("symbol_overrides", {})
    if sym in overrides:
        ov = overrides[sym]
        if ov.get("block_short_on_htf_uptrend", False):
            if htf_ema_f.iloc[-1] > htf_ema_s.iloc[-1] and score < 0:
                score = 0
        if ov.get("block_long_on_htf_downtrend", False):
            if htf_ema_f.iloc[-1] < htf_ema_s.iloc[-1] and score > 0:
                score = 0

    if abs(score) < c["votes_needed"]:
        return None

    return "LONG" if score > 0 else "SHORT"

class BreakoutAVAAIFull:
    """
    A deterministic, self-contained strategy that encodes BOTH the candidate selection
    and the trading rules. This mirrors the profitable behaviour discovered in your runs:
      - Universe/Rank: score = dp6h + dp12h (SHORT inverts sign).  No momentum threshold by default.
      - Entry: side from params; BOTH follows sign(mom_sum).
      - TP/SL: ALWAYS from ATR multiples at entry.
      - Exit: CLOSE-based TP/SL checks (match old backtester).
    Non-zero defaults that used to live in the backtester (e.g., top-n=8) are moved here.
    Top-level YAML keys override strategy_params to preserve your prior “lucky” overrides.
    """
    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self.cfg = dict(cfg or {})
        sp = (self.cfg.get("strategy_params") or {})

        # --- read knobs (top-level overrides SP to preserve historical behaviour) ---
        def _read(key, default):
            return self.cfg.get(key, sp.get(key, default))

        self.side: str = str(_read("side", "BOTH")).upper()
        self.top_n: int = int(_read("top_n", _read("top-n", 8)))  # accept both spellings
        self.min_atr_ratio: float = float(_read("min_atr_ratio", 0.02))
        # Important: default to 0.0 to reproduce the wide-entry behaviour unless overridden
        self.min_momentum_sum: float = float(_read("min_momentum_sum", 0.0))
        self.tp_mult: float = float(_read("tp_atr_mult", 3.8))
        self.sl_mult: float = float(_read("sl_atr_mult", 1.04))

        # liquidity floors (kept but set lenient defaults; can be overridden in YAML)
        self.min_qv_24h: float = float(_read("min_qv_24h", 0.0))
        self.min_qv_1h: float  = float(_read("min_qv_1h", 0.0))

        # optional limits to control entry bursts
        self.max_new_positions_per_bar: int = int(_read("max_new_positions_per_bar", 0))
        self.first_bar_max_positions: int = int(
            _read("first_bar_max_positions", self.max_new_positions_per_bar)
        )
        # internal counters
        self._first_bar_ts: Optional[Any] = None
        self._last_bar_ts: Optional[Any] = None
        self._opens_this_bar: int = 0
    # ---------- helpers ----------
    def _mom_sum(self, row: Mapping[str, Any]) -> float:
        return _f(row.get("dp6h", 0.0)) + _f(row.get("dp12h", 0.0))

    def _liq_ok(self, row: Mapping[str, Any]) -> bool:
        qv24 = _f(row.get("qv_24h", 0.0))
        qv1  = _f(row.get("quote_volume", 0.0))
        if qv1 <= 0.0:
            # allow derived 1h volume if provided
            qv1 = _f(row.get("volume", 0.0)) * _f(row.get("close", 0.0))
        return (qv24 >= self.min_qv_24h) and (qv1 >= self.min_qv_1h)

    @staticmethod
    def _pct_gap(actual: float, thresh: float) -> float:
        """Percentage gap for checks of the form ``actual >= thresh``."""
        try:
            a = float(actual); t = float(thresh)
        except Exception:
            return 1.0
        if t <= 0:
            return 0.0
        if a >= t:
            return 0.0
        return max(0.0, min(1.0, (t - a) / t))

    @staticmethod
    def _pct_gap_rev(actual: float, thresh: float) -> float:
        """Reverse variant used for directional momentum thresholds."""
        try:
            a = float(actual); t = float(thresh)
        except Exception:
            return 1.0
        if t <= 0:
            return 0.0
        if a >= t:
            return 0.0
        return max(0.0, min(1.0, (t - a) / t))

    # ---------- universe & ranking ----------
    def universe(self, t: Any, md_map: Mapping[str, Mapping[str, Any]]) -> List[str]:
        """Filter symbols by minimal ATR and liquidity. Momentum threshold is optional and
        defaults to 0.0 (disabled) to match the profitable setting discovered earlier."""
        out: List[str] = []
        for sym, row in md_map.items():
            atr = _f(row.get("atr_ratio", 0.0))
            if self.min_atr_ratio > 0 and atr < self.min_atr_ratio:
                continue
            if not self._liq_ok(row):
                continue
            # Optional momentum threshold (mm<=0 disables)
            m = self._mom_sum(row)
            mm = self.min_momentum_sum
            if mm > 0:
                if self.side == "LONG" and m < +mm:
                    continue
                if self.side == "SHORT" and m > -mm:
                    continue
                if self.side == "BOTH" and abs(m) < mm:
                    continue
            out.append(sym)
        return out

    def rank(self, t: Any, md_map: Mapping[str, Mapping[str, Any]], universe_syms: List[str]) -> List[str]:
        """Sort by directional momentum score; return already cut to top_n (keeps stability)."""
        invert = (self.side == "SHORT")
        scored: List[Tuple[float,int,str]] = []
        for idx, sym in enumerate(universe_syms):
            m = self._mom_sum(md_map.get(sym, {}))
            score = (-m) if invert else (m)
            scored.append((score, idx, sym))
        scored.sort(key=lambda x: x[0], reverse=True)  # stable via index
        # CUT to top_n here (moved from backtester); top_n<=0 disables the limit
        take = int(self.top_n)
        if take <= 0:
            return [sym for _, __, sym in scored]
        return [sym for _, __, sym in scored[:take]]

    # ---------- entry / exit ----------
    def entry_signal(self, t: Any, symbol: str, row: Mapping[str, Any], ctx: Optional[Mapping[str, Any]] = None) -> Optional[Sig]:
        """Return full Sig with TP/SL and enforce per-bar entry caps."""
        # Track bar transition
        if self._first_bar_ts is None:
            self._first_bar_ts = t
        if t != self._last_bar_ts:
            self._last_bar_ts = t
            self._opens_this_bar = 0
        # Determine limit for this bar
        limit = self.max_new_positions_per_bar
        if t == self._first_bar_ts and self.first_bar_max_positions > 0:
            limit = self.first_bar_max_positions
        if limit > 0 and self._opens_this_bar >= limit:
            return None

        df: Optional[pd.DataFrame] = None
        if isinstance(ctx, Mapping):
            df = ctx.get("df")  # direct pass
            if df is None:
                md = ctx.get("md")
                if md is not None:
                    if hasattr(md, "dfs") and symbol in md.dfs:
                        df = md.dfs[symbol]
                    elif hasattr(md, "get_df"):
                        try:
                            df = md.get_df(symbol)
                        except Exception:
                            df = None
        if df is None and isinstance(row, Mapping):
            maybe_df = row.get("df")
            if isinstance(maybe_df, pd.DataFrame):
                df = maybe_df
        side_pick: Optional[str] = None
        if df is not None and len(df) > 0:
            df = df.copy()
            df.attrs["symbol"] = symbol
            side_pick = pick_side(df, self.cfg)
        else:
            mom = self._mom_sum(row)
            if self.side == "LONG":
                side_pick = "LONG"
            elif self.side == "SHORT":
                side_pick = "SHORT"
            else:
                side_pick = "LONG" if mom >= 0 else "SHORT"
        if side_pick is None:
            return None
        if self.side == "LONG" and side_pick != "LONG":
            return None
        if self.side == "SHORT" and side_pick != "SHORT":
            return None
        side: Side = "LONG" if side_pick == "LONG" else "SHORT"

        close = _f(row.get("close", None), None)
        atrr  = _f(row.get("atr_ratio", None), None)
        if close is None or atrr is None or close <= 0 or atrr <= 0:
            return None

        atr_abs = max(1e-12, close * atrr)
        if side == "LONG":
            tp = close + self.tp_mult * atr_abs
            sl = close - self.sl_mult * atr_abs
        else:
            tp = close - self.tp_mult * atr_abs
            sl = close + self.sl_mult * atr_abs

        self._opens_this_bar += 1
        sig = Sig(side=side, take_profit=float(tp), stop_price=float(sl), reason="rule/atr-multipliers")
        sig.tags = (sig.tags or []) + [f"side={side_pick}", "consensus"]
        return sig

    def manage_position(self, symbol: str, row: Mapping[str, Any], pos: Any, ctx: Optional[Mapping[str, Any]] = None):
        """CLOSE-based TP/SL (match the old behaviour)."""
        close = _f(row.get("close", 0.0))
        side  = str(getattr(pos, "side", "LONG")).upper()
        tp    = _f(getattr(pos, "tp", getattr(pos, "take_profit", getattr(pos, "tp_price", None))), None)
        sl    = _f(getattr(pos, "sl", getattr(pos, "stop_price", getattr(pos, "sl_price", None))), None)

        if side == "LONG":
            if sl is not None and close <= sl: return ExitSig("SL", exit_price=sl)
            if tp is not None and close >= tp: return ExitSig("TP", exit_price=tp)
        else:
            if sl is not None and close >= sl: return ExitSig("SL", exit_price=sl)
            if tp is not None and close <= tp: return ExitSig("TP", exit_price=tp)
        return ExitSig("HOLD")

    # ---------- optional: heat reporting only (no decisions here) ----------
    def entry_distance(self, t: Any, sym: str, row: Mapping[str, Any]) -> Dict[str, Any]:
        """Compute gaps against the strategy's filters for a single symbol."""
        m = self._mom_sum(row)
        atrr = _f(row.get("atr_ratio", 0.0))
        qv24 = _f(row.get("qv_24h", 0.0))
        qv1  = _f(row.get("quote_volume", 0.0))
        if qv1 <= 0.0:
            qv1 = _f(row.get("volume", 0.0)) * _f(row.get("close", 0.0))

        min_atr = float(self.min_atr_ratio)
        min_qv24 = float(self.min_qv_24h)
        min_qv1h = float(self.min_qv_1h)
        min_mom = float(self.min_momentum_sum)

        if self.side == "LONG":
            gap_mom = self._pct_gap_rev(m, +min_mom)
        elif self.side == "SHORT":
            gap_mom = self._pct_gap_rev(-m, +min_mom)
        else:
            gap_mom = self._pct_gap_rev(abs(m), +min_mom)

        gap_atr = self._pct_gap(atrr, min_atr)
        gap_qv24 = self._pct_gap(qv24, min_qv24)
        gap_qv1 = self._pct_gap(qv1, min_qv1h)

        combined_gap = max(gap_atr, gap_qv24, gap_qv1, gap_mom)

        gaps_map = {
            "atr": gap_atr,
            "qv24": gap_qv24,
            "qv1h": gap_qv1,
            "momentum": gap_mom,
        }
        worst_key = max(gaps_map, key=lambda k: gaps_map[k])
        reason = ""
        if worst_key == "atr":
            reason = f"atr low: {atrr:.4f} < {min_atr:.4f}"
        elif worst_key == "qv24":
            reason = f"qv24 low: {qv24:.0f} < {min_qv24:.0f}"
        elif worst_key == "qv1h":
            reason = f"qv1h low: {qv1:.0f} < {min_qv1h:.0f}"
        elif worst_key == "momentum":
            reason = f"momentum low: {m:.4f} < {min_mom:.4f}"

        return {
            "symbol": sym,
            "combined_gap": float(combined_gap),
            "gaps": {
                "atr": float(gap_atr),
                "qv24": float(gap_qv24),
                "qv1h": float(gap_qv1),
                "momentum": float(gap_mom),
            },
            "actuals": {
                "atr_ratio": float(atrr),
                "qv_24h": float(qv24),
                "qv_1h": float(qv1),
                "mom_sum": float(m),
            },
            "thresholds": {
                "min_atr_ratio": float(min_atr),
                "min_qv_24h": float(min_qv24),
                "min_qv_1h": float(min_qv1h),
                "min_momentum_sum": float(min_mom),
            },
            "reason": reason,
        }

    def best_entry_distance(self, t: Any, md_slice: dict, symbols=None) -> Optional[Dict[str, Any]]:
        """Evaluate distances for many symbols and return the nearest-to-entry one."""
        if not md_slice:
            return None
        if symbols is None:
            symbols = list(md_slice.keys())

        best = None
        best_gap = 1.0
        for sym in symbols:
            row = md_slice.get(sym)
            if not row:
                continue
            dist = self.entry_distance(t, sym, row)
            gap = float(dist.get("combined_gap", 1.0))
            if gap < best_gap:
                best_gap = gap
                best = dist
        return best

    def heat(self, t: Any, symbol: str, row: Mapping[str, Any]) -> float:
        """Return heat in [0..1] computed as ``1 - max(gaps)``."""
        try:
            dist = self.entry_distance(t, symbol, row)
            gaps = (dist or {}).get("gaps") or {}
            if not gaps:
                return 0.0
            worst = max(float(v) for v in gaps.values() if v is not None)
            return max(0.0, min(1.0, 1.0 - worst))
        except Exception:
            return 0.0
