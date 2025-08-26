# strategies/breakout_avaai_full.py
# Pine-like breakout adapter for backtester_core_speed3.py with veto support.
# Requires core to respect sig.take == False (patch provided).
from collections import deque
import math

def _rma(prev, value, length):
    if length <= 1:
        return value
    if prev is None:
        return value
    alpha = 1.0 / length
    return prev + alpha * (value - prev)

class BreakoutAVAAIFull:
    class Sig:
        __slots__ = ("side","take")
        def __init__(self, side=None, take=False):
            self.side = side
            self.take = take

    def __init__(self, cfg):
        sp = (cfg or {}).get("strategy_params", {}) or {}
        # Core-exposed params (still read by core): side, tp/sl multipliers, top-n etc.
        self.side_pref = str(sp.get("side","BOTH")).upper()
        # Pine-like params
        self.length = int(sp.get("length", 10))
        self.atr_period = int(sp.get("atr_period", 14))
        # Threshold expects ATR/close ratio (like atr_ratio). If None -> disabled
        self.atr_threshold_ratio = float(sp.get("atr_threshold_ratio", 0.002))
        self.require_trending = bool(sp.get("require_trending", True))
        # Optional filters
        self.volatility_length = int(sp.get("volatility_length", 0))   # ATR > SMA(ATR)
        self.volume_length = int(sp.get("volume_length", 0))           # volume > SMA(volume)
        self.adx_length = int(sp.get("adx_length", 0))                 # 0 disables
        self.adx_threshold = float(sp.get("adx_threshold", 1.0))
        self.macd_filter = int(sp.get("macd_filter", 0))               # 1 enables
        # Tick epsilon
        self.eps = float(sp.get("tick_size", 0.0) or 0.0)
        if self.eps <= 0:
            self.eps = 1e-12

        # Per-symbol state
        self.state = {}

    def _sym(self, sym):
        S = self.state.get(sym)
        if S is None:
            S = {
                "highs": deque(maxlen=max(2, self.length+1)),
                "lows":  deque(maxlen=max(2, self.length+1)),
                "vols":  deque(maxlen=max(2, self.volume_length or 2)),
                "atrs":  deque(maxlen=max(2, self.volatility_length or 2)),
                # For ADX
                "prev_h": None, "prev_l": None, "prev_c": None,
                "rma_tr": None, "rma_pdm": None, "rma_mdm": None,
                # For MACD
                "ema_fast": None, "ema_slow": None, "ema_sig": None,
            }
            self.state[sym] = S
        return S

    def _update_indicators(self, S, row):
        close = float(row.get("close", 0.0) or 0.0)
        high  = float(row.get("high", close) or close)
        low   = float(row.get("low", close) or close)
        vol   = float(row.get("quote_volume", row.get("volume", 0.0)) or 0.0)

        # rolling H/L and volume
        S["highs"].append(high)
        S["lows"].append(low)
        if self.volume_length > 0:
            S["vols"].append(vol)

        # ATR absolute: prefer atr_ratio*close if present, else TR with RMA(atr_period)
        atr_ratio = row.get("atr_ratio", None)
        atr_abs = None
        if atr_ratio is not None:
            try:
                atr_abs = float(atr_ratio) * max(close, 1e-12)
            except Exception:
                atr_abs = None

        tr = None
        if atr_abs is None:
            ph, pl, pc = S["prev_h"], S["prev_l"], S["prev_c"]
            if ph is not None and pl is not None and pc is not None:
                tr = max(ph - pl, abs(ph - pc), abs(pl - pc))
                S["rma_tr"] = _rma(S["rma_tr"], tr, self.atr_period)
                atr_abs = S["rma_tr"]
            else:
                atr_abs = None

        if self.volatility_length > 0 and atr_abs is not None:
            S["atrs"].append(atr_abs)

        # ADX
        adx_val = None
        if self.adx_length > 0 and S["prev_h"] is not None and S["prev_l"] is not None:
            up_move = high - S["prev_h"]
            down_move = S["prev_l"] - low
            plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
            minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
            true_range = None
            pc = S["prev_c"]
            if pc is not None:
                true_range = max(high - low, abs(high - pc), abs(low - pc))
            if true_range is not None and true_range > 0:
                S["rma_tr"] = _rma(S["rma_tr"], true_range, self.adx_length)
                S["rma_pdm"] = _rma(S["rma_pdm"], plus_dm, self.adx_length)
                S["rma_mdm"] = _rma(S["rma_mdm"], minus_dm, self.adx_length)
                if S["rma_tr"] and S["rma_tr"] > 0:
                    plus_di = 100.0 * (S["rma_pdm"] / S["rma_tr"])
                    minus_di = 100.0 * (S["rma_mdm"] / S["rma_tr"])
                    dx = 100.0 * abs(plus_di - minus_di) / max(plus_di + minus_di, 1e-12)
                    # RMA(dx, length)
                    S["adx_r"] = _rma(S.get("adx_r"), dx, self.adx_length)
                    adx_val = S.get("adx_r")

        # MACD (12,26,9)
        macd_ok = True
        if self.macd_filter:
            ema_fast = S["ema_fast"] = _rma(S["ema_fast"], close, 12)
            ema_slow = S["ema_slow"] = _rma(S["ema_slow"], close, 26)
            if ema_fast is not None and ema_slow is not None:
                macd = ema_fast - ema_slow
                ema_sig = S["ema_sig"] = _rma(S["ema_sig"], macd, 9)
                if ema_sig is not None:
                    macd_ok = macd > ema_sig  # bullish by default; bearish checked later
            else:
                macd_ok = False

        # Save prevs
        S["prev_h"], S["prev_l"], S["prev_c"] = high, low, close
        return atr_abs, adx_val, macd_ok

    def entry_signal(self, t, sym, row, ctx=None):
        S = self._sym(sym)
        close = float(row.get("close", 0.0) or 0.0)
        high  = float(row.get("high", close) or close)
        low   = float(row.get("low", close) or close)

        # Update indicators/state
        atr_abs, adx_val, macd_bull = self._update_indicators(S, row)

        # Need enough history for breakout levels
        if len(S["highs"]) < self.length+1 or len(S["lows"]) < self.length+1:
            return None  # Not enough history yet

        # Breakout levels from *previous* bars
        prev_highs = list(S["highs"])[:-1]
        prev_lows  = list(S["lows"])[:-1]
        up_bound = max(prev_highs)
        dn_bound = min(prev_lows)

        # ATR trending filter like Pine: atr_ratio > threshold (relative) if require_trending
        atr_ok = True
        if self.require_trending and self.atr_threshold_ratio is not None:
            atr_ratio = row.get("atr_ratio", None)
            if atr_ratio is None:
                if atr_abs is None or close <= 0:  # No ATR info → pass
                    atr_ok = True
                else:
                    atr_ok = (atr_abs / close) > self.atr_threshold_ratio
            else:
                try:
                    atr_ok = float(atr_ratio) > self.atr_threshold_ratio
                except Exception:
                    atr_ok = True

        # Volatility filter ATR > SMA(ATR)
        vol_ok = True
        if self.volatility_length > 0 and len(S["atrs"]) >= self.volatility_length:
            atrs = list(S["atrs"])
            sma = sum(atrs[-self.volatility_length:]) / float(self.volatility_length)
            vol_ok = atrs[-1] > sma

        # Volume filter
        volume_ok = True
        if self.volume_length > 0 and len(S["vols"]) >= self.volume_length:
            vs = list(S["vols"])
            sma_v = sum(vs[-self.volume_length:]) / float(self.volume_length)
            volume_ok = vs[-1] > sma_v

        # ADX filter
        adx_ok = True
        if self.adx_length > 0 and self.adx_threshold > 0.0:
            adx_ok = (adx_val is not None) and (adx_val > self.adx_threshold)

        # Combined condition
        filters_ok = atr_ok and vol_ok and volume_ok and adx_ok

        # Breakout triggers on current bar's extremes vs previous bounds
        long_break = high >= (up_bound + self.eps)
        short_break = low <= (dn_bound - self.eps)

        # Decide
        if not filters_ok:
            return self.Sig(None, take=False)

        # MACD direction handling if enabled
        if self.macd_filter:
            macd_bear = not macd_bull
        else:
            macd_bear = True  # allow

        if long_break and macd_bull:
            return self.Sig("LONG", take=True)
        if short_break and macd_bear:
            return self.Sig("SHORT", take=True)

        return self.Sig(None, take=False)
