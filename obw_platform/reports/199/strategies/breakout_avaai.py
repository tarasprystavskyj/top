# strategies/breakout_avaai.py
# Breakout AVAAI (Pine v6 inspired) — Python implementation for generic OHLC backtesters.
# Assumptions:
#   - df columns: ["open","high","low","close","volume"]
#   - We emit a DataFrame with columns describing intended stop-entry levels and filters,
#     plus binary columns for long/short entry signals on each bar using *previous-bar* levels.
#   - Exits (TP/SL) are returned as *percent* so the engine can compute absolute prices
#     from its actual fills.
#
# Notes:
#   - "length" uses previous bars (shifted) to avoid lookahead when setting stop levels.
#   - ATR trending filter: atr(atr_period) > atr_threshold (or > close*threshold if atr_normalize==True)
#   - Volatility filter (optional): atr(volatility_length) > sma(atr(volatility_length), volatility_length)
#   - Volume filter (optional): volume > sma(volume, volume_length)
#   - ADX filter (optional): adx(adx_length) > adx_threshold
#   - MACD filter (optional): require macd>signal for longs and macd<signal for shorts (macd_filter=1)
#
# Integration:
#   - Call `compute(df, params)` to get result DataFrame.
#   - The engine can interpret:
#       * 'entry_stop_long'/'entry_stop_short' as stop prices (place before current bar).
#       * 'signal_long'/'signal_short' as bar-internal triggers if high>=entry_stop_long or low<=entry_stop_short.
#       * 'tp_pct'/'sl_pct' as percent exits from the entry price (engine applies).
#       * 'dir' parameter: 'BOTH'|'LONG'|'SHORT'
#
#   - Warmup requirement: max(length+1, atr_period*3, volatility_length*3, adx_length*3, 35)
#
# Default disabling: set any *_length or *_threshold to 0 to disable the respective filter.
#
from __future__ import annotations
import numpy as np
import pandas as pd

def _ema(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(span=n, adjust=False, min_periods=n).mean()

def _rma(x: pd.Series, n: int) -> pd.Series:
    # Wilder's RMA
    return x.ewm(alpha=1.0/n, adjust=False).mean()

def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = np.maximum(h - l, np.maximum((h - prev_c).abs(), (l - prev_c).abs()))
    return _rma(tr, n)

def _macd(close: pd.Series, fast=12, slow=26, sig=9):
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    macd = ema_fast - ema_slow
    signal = _ema(macd, sig)
    hist = macd - signal
    return macd, signal, hist

def _adx(df: pd.DataFrame, n: int) -> pd.Series:
    # Classic Wilder's ADX
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    down = -l.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    tr = _atr(df, 1)  # true range 1 (raw, not averaged)
    tr_n = _rma(tr, n)
    pdi = 100 * _rma(plus_dm, n) / tr_n
    mdi = 100 * _rma(minus_dm, n) / tr_n
    dx = ( (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan) ) * 100
    adx = _rma(dx.fillna(0), n)
    return adx

def warmup_bars(params: dict) -> int:
    length = int(params.get("length", 10))
    atr_period = int(params.get("atr_period", 14))
    vol_len = int(params.get("volatility_length", 0))
    adx_len = int(params.get("adx_length", 0))

    return int(max(length + 1, atr_period * 3, (vol_len or 1) * 3, (adx_len or 1) * 3, 35))

def compute(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """
    Compute signals/levels for Breakout AVAAI.

    Parameters (with defaults matching Pine snippet intent):
        length: int = 10
        take_profit_pct: float = 2.0       # 200% (2.0) TP, set to 0 to disable
        stop_loss_pct: float = 0.10        # 10%  (0.10) SL, set to 0 to disable
        trade_direction: str = 'BOTH'      # 'LONG' | 'SHORT' | 'BOTH'
        atr_period: int = 14
        atr_threshold: float = 0.002       # if atr_normalize==True, treated as relative to price
        atr_normalize: int = 0             # 1 to compare atr/close to threshold
        volatility_length: int = 0         # 0 disables
        adx_length: int = 0                # 0 disables
        adx_threshold: float = 1.0         # used only if adx_length>0 and >0
        volume_length: int = 0             # 0 disables
        macd_filter: int = 0               # 1 to enforce MACD direction agreement
        tick_size: float = 0.0             # additive to breakout stop level
    """
    p = {
        "length": 10,
        "take_profit_pct": 2.0,
        "stop_loss_pct": 0.10,
        "trade_direction": "BOTH",
        "atr_period": 14,
        "atr_threshold": 0.002,
        "atr_normalize": 0,
        "volatility_length": 0,
        "adx_length": 0,
        "adx_threshold": 1.0,
        "volume_length": 0,
        "macd_filter": 0,
        "tick_size": 0.0,
    }
    p.update({k:v for k,v in params.items() if v is not None})

    out = pd.DataFrame(index=df.index)

    # Core indicators
    atr_main = _atr(df, int(p["atr_period"]))
    if int(p["atr_normalize"]) == 1:
        atr_comp = atr_main / df["close"]
    else:
        atr_comp = atr_main

    # Trending condition
    if float(p["atr_threshold"]) > 0:
        is_trending = atr_comp > float(p["atr_threshold"])
    else:
        is_trending = pd.Series(True, index=df.index)

    # Volatility momentum (optional)
    vol_len = int(p["volatility_length"])
    if vol_len > 0:
        atr_v = _atr(df, vol_len)
        atr_v_sma = atr_v.rolling(vol_len, min_periods=vol_len).mean()
        vol_ok = atr_v > atr_v_sma
    else:
        vol_ok = pd.Series(True, index=df.index)

    # Volume filter (optional)
    vol_ma_len = int(p["volume_length"])
    if vol_ma_len > 0 and "volume" in df.columns:
        vol_ma = df["volume"].rolling(vol_ma_len, min_periods=vol_ma_len).mean()
        volume_ok = df["volume"] > vol_ma
    else:
        vol_ma = pd.Series(np.nan, index=df.index)
        volume_ok = pd.Series(True, index=df.index)

    # ADX filter (optional)
    adx_len = int(p["adx_length"])
    if adx_len > 0 and float(p["adx_threshold"]) > 0:
        adx_val = _adx(df, adx_len)
        adx_ok = adx_val > float(p["adx_threshold"])
    else:
        adx_val = pd.Series(np.nan, index=df.index)
        adx_ok = pd.Series(True, index=df.index)

    # MACD (optional directional confirmation)
    macd, macd_sig, _ = _macd(df["close"], 12, 26, 9)
    is_bull = macd > macd_sig
    is_bear = macd < macd_sig

    # Combined trade condition (use previous bar values to avoid lookahead)
    filters_ok = (is_trending & vol_ok & volume_ok & adx_ok).shift(1).fillna(False)
    if int(p["macd_filter"]) == 1:
        bull_ok = filters_ok & is_bull.shift(1).fillna(False)
        bear_ok = filters_ok & is_bear.shift(1).fillna(False)
    else:
        bull_ok, bear_ok = filters_ok, filters_ok

    # Breakout levels based on previous bars
    L = int(p["length"])
    up_level = df["high"].shift(1).rolling(L, min_periods=L).max()
    dn_level = df["low"].shift(1).rolling(L, min_periods=L).min()

    tick = float(p["tick_size"] or 0.0)
    entry_stop_long = (up_level + tick)
    entry_stop_short = (dn_level - tick)

    # Entry triggers intra-bar using current OHLC
    # Approximate stop fill: trigger if high >= stop (long) / low <= stop (short)
    long_ok_dir = p["trade_direction"].upper() in ("BOTH", "LONG")
    short_ok_dir = p["trade_direction"].upper() in ("BOTH", "SHORT")

    trig_long = bull_ok & (df["high"] >= entry_stop_long)
    trig_short = bear_ok & (df["low"] <= entry_stop_short)

    signal_long = trig_long & long_ok_dir
    signal_short = trig_short & short_ok_dir

    # Distances to boundary (for diagnostics/heat; clipped at 0)
    dist_to_long = ((df["high"] - entry_stop_long) / entry_stop_long).clip(lower=0)
    dist_to_short = ((entry_stop_short - df["low"]) / entry_stop_short).clip(lower=0)

    out["entry_stop_long"] = entry_stop_long
    out["entry_stop_short"] = entry_stop_short
    out["signal_long"] = signal_long.astype(int)
    out["signal_short"] = signal_short.astype(int)
    out["filters_ok"] = filters_ok.astype(int)
    out["atr"] = atr_main
    out["adx"] = adx_val
    out["volume_ma"] = vol_ma
    out["up_level"] = up_level
    out["down_level"] = dn_level
    out["dist_to_long"] = dist_to_long
    out["dist_to_short"] = dist_to_short

    # Exit configuration as percents (engine to apply to actual entry price)
    out["tp_pct"] = float(p["take_profit_pct"] or 0.0)
    out["sl_pct"] = float(p["stop_loss_pct"] or 0.0)

    # Helpful metadata
    out.attrs["params"] = p
    out.attrs["warmup_bars"] = warmup_bars(p)
    out.attrs["execution"] = "stop_breakout_prev_levels"
    out.attrs["direction"] = p["trade_direction"].upper()
    out.attrs["notes"] = "Signals generated using previous-bar breakout levels; MACD filter optional."
    return out