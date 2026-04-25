#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rank_short_leg_all_symbols_akela_v2.py

Purpose:
  Rank futures symbols for short-side "Akela missed" candidates:
  assets that are still liquid/volatile, but lose leadership, fail pumps,
  sell off after volume spikes, and produce enough short-scalp pullbacks.

Inputs:
  1) SQLite DB, e.g.
     ../DB/combined_cache_3m_7200_500u.db

  2) Optional NPZ cache, e.g.
     ../DB/fast_cache_1m_short_top15_5000b.npz

  3) Optional strategy YAML + strategy class for real short-leg backtest.

This script is intentionally defensive:
  - auto-detects SQLite table with OHLCV fields;
  - tries several common NPZ layouts;
  - if strategy import/backtest fails, it still produces Akela ranking.

Expected SQLite columns:
  required: symbol, datetime_utc or timestamp/time/datetime, open, high, low, close
  optional: volume, quote_volume, qv_24h, atr_ratio, dp6h, dp12h

Examples:

  # Structural Akela ranking only:
  python3 rank_short_leg_all_symbols_akela_v2.py \
    --db ../DB/combined_cache_3m_7200_500u.db \
    --out _reports/akela_500u.csv \
    --top 50 \
    --no-backtest

  # Structural + short-leg strategy backtest:
  python3 rank_short_leg_all_symbols_akela_v2.py \
    --cfg configs/final_best_ena_1y_pack_04-14_bingx_live_bundle_open_first_bar_partial_fix_v1.yaml \
    --db ../DB/combined_cache_3m_7200_500u.db \
    --top 50 \
    --min-bars 1000 \
    --min-trades 5 \
    --max-mtm-dd -40 \
    --out _reports/akela_500u_with_bt.csv \
    --json-out _reports/akela_500u_top50.json

  # Analyze NPZ too:
  python3 rank_short_leg_all_symbols_akela_v2.py \
    --npz ../DB/fast_cache_1m_short_top15_5000b.npz \
    --out _reports/akela_npz_top15.csv \
    --no-backtest

Notes:
  - For 3m bars, defaults assume 7200 bars ~= 15 days.
  - For 1m bars, pass:
      --bar-minutes 1 --pump-window 1440 --pump-fail-window 480 --high-lookback 7200
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    import yaml
except Exception:
    yaml = None


# -------------------------
# Utility
# -------------------------

@dataclass
class Position:
    side: str
    entry: float
    sl: float
    tp: float
    qty: float


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    try:
        if b is None or abs(float(b)) < 1e-12 or not np.isfinite(b):
            return default
        return float(a) / float(b)
    except Exception:
        return default


def max_drawdown_pct(curve: Iterable[float]) -> float:
    arr = np.asarray(list(curve), dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size <= 1:
        return 0.0
    peak = np.maximum.accumulate(arr)
    dd = (arr - peak) / np.maximum(peak, 1e-12)
    return float(np.nanmin(dd) * 100.0)


def import_by_path(path: str):
    mod_name, cls_name = path.rsplit(".", 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, cls_name)


def load_yaml(path: Optional[str]) -> dict:
    if not path:
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is not installed. Install with: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_strategy_class_short(cfg: dict, cli_strategy: Optional[str] = None) -> Optional[str]:
    if cli_strategy:
        return cli_strategy

    # Common direct keys.
    for key in (
        "strategy_class_short",
        "strategy_short_class",
        "short_strategy_class",
        "strategy_class",
    ):
        if cfg.get(key):
            return str(cfg[key])

    # Common nested keys.
    for section in ("strategy", "short", "strategy_short", "engine"):
        v = cfg.get(section)
        if isinstance(v, dict):
            for key in ("class_short", "strategy_class_short", "class", "strategy_class"):
                if v.get(key):
                    return str(v[key])

    return None


def normalize_symbol(s: Any) -> str:
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="ignore")
    s = str(s)
    s = s.strip()
    return s


def maybe_datetime_from_numeric(x: np.ndarray) -> pd.Series:
    arr = np.asarray(x)
    if arr.size == 0:
        return pd.Series([], dtype="datetime64[ns, UTC]")

    arr_float = pd.to_numeric(pd.Series(arr.reshape(-1)), errors="coerce")
    med = arr_float.dropna().median() if arr_float.notna().any() else np.nan

    # seconds, milliseconds, microseconds, nanoseconds heuristic.
    if not np.isfinite(med):
        return pd.to_datetime(arr.reshape(-1), utc=True, errors="coerce")

    if med > 1e17:
        unit = "ns"
    elif med > 1e14:
        unit = "us"
    elif med > 1e11:
        unit = "ms"
    else:
        unit = "s"

    return pd.to_datetime(arr.reshape(-1), unit=unit, utc=True, errors="coerce")


# -------------------------
# SQLite loader
# -------------------------

TIME_COL_CANDIDATES = ["datetime_utc", "timestamp", "time", "datetime", "date", "ts"]
VOLUME_COL_CANDIDATES = ["quote_volume", "qv_24h", "volume", "vol"]


def list_sqlite_tables(con: sqlite3.Connection) -> List[str]:
    q = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    return [r[0] for r in con.execute(q).fetchall()]


def get_table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    return pd.read_sql_query(f"PRAGMA table_info({table})", con)["name"].tolist()


def detect_ohlcv_table(con: sqlite3.Connection, preferred_table: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    tables = list_sqlite_tables(con)
    if preferred_table:
        tables = [preferred_table] + [t for t in tables if t != preferred_table]

    required_price = ["open", "high", "low", "close"]

    for table in tables:
        cols = get_table_columns(con, table)
        cols_l = {c.lower(): c for c in cols}

        if "symbol" not in cols_l:
            continue
        if not all(c in cols_l for c in required_price):
            continue

        time_col = None
        for c in TIME_COL_CANDIDATES:
            if c in cols_l:
                time_col = cols_l[c]
                break
        if not time_col:
            continue

        mapping = {
            "symbol": cols_l["symbol"],
            "time": time_col,
            "open": cols_l["open"],
            "high": cols_l["high"],
            "low": cols_l["low"],
            "close": cols_l["close"],
        }
        for opt in ["atr_ratio", "dp6h", "dp12h", "quote_volume", "qv_24h", "volume", "vol"]:
            if opt in cols_l:
                mapping[opt] = cols_l[opt]
        return table, mapping

    raise RuntimeError(
        "Could not detect OHLCV table. Need columns: symbol, time/datetime/timestamp, open, high, low, close."
    )


def load_db_ohlcv(
    db_path: str,
    table: Optional[str],
    time_from: Optional[str],
    time_to: Optional[str],
    symbols: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    con = sqlite3.connect(db_path)
    table_name, m = detect_ohlcv_table(con, table)

    select_items = [
        f"{m['symbol']} AS symbol",
        f"{m['time']} AS datetime_utc",
        f"{m['open']} AS open",
        f"{m['high']} AS high",
        f"{m['low']} AS low",
        f"{m['close']} AS close",
    ]

    for out_name in ["atr_ratio", "dp6h", "dp12h", "quote_volume", "qv_24h", "volume", "vol"]:
        if out_name in m:
            select_items.append(f"{m[out_name]} AS {out_name}")

    where = []
    params: List[Any] = []

    # Only apply time filters as raw strings; works for ISO datetime columns.
    # For numeric timestamps, user should usually skip time filters unless schema is known.
    if time_from:
        where.append(f"{m['time']} >= ?")
        params.append(time_from)
    if time_to:
        where.append(f"{m['time']} < ?")
        params.append(time_to)

    if symbols:
        ph = ",".join(["?"] * len(symbols))
        where.append(f"{m['symbol']} IN ({ph})")
        params.extend(list(symbols))

    sql = f"SELECT {', '.join(select_items)} FROM {table_name}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {m['symbol']}, {m['time']}"

    print(f"[db] table={table_name}")
    print(f"[db] loading rows...")
    df = pd.read_sql_query(sql, con, params=params)
    con.close()

    if df.empty:
        raise RuntimeError("No rows loaded from DB.")

    df["symbol"] = df["symbol"].map(normalize_symbol)

    # Parse datetime; if numeric-like, use epoch heuristic.
    raw_time = df["datetime_utc"]
    numeric_time = pd.to_numeric(raw_time, errors="coerce")
    if numeric_time.notna().mean() > 0.8:
        df["datetime_utc"] = maybe_datetime_from_numeric(numeric_time.to_numpy())
    else:
        df["datetime_utc"] = pd.to_datetime(raw_time, utc=True, errors="coerce")

    df = df.dropna(subset=["symbol", "datetime_utc", "close"])
    for c in ["open", "high", "low", "close", "atr_ratio", "dp6h", "dp12h", "quote_volume", "qv_24h", "volume", "vol"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


# -------------------------
# NPZ loader
# -------------------------

def npz_keys(npz_path: str) -> List[str]:
    with np.load(npz_path, allow_pickle=True) as z:
        return list(z.keys())


def find_key(keys: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    kl = {k.lower(): k for k in keys}
    for c in candidates:
        if c.lower() in kl:
            return kl[c.lower()]
    return None


def decode_symbols_array(a: np.ndarray) -> List[str]:
    arr = np.asarray(a)
    if arr.dtype == object and arr.size == 1:
        item = arr.item()
        if isinstance(item, (list, tuple, np.ndarray)):
            arr = np.asarray(item)
    return [normalize_symbol(x) for x in arr.reshape(-1).tolist()]


def infer_ohlcv_array(z: Any, keys: Sequence[str], symbols: Optional[List[str]]) -> Tuple[Optional[str], Optional[np.ndarray]]:
    # Preferred dense cache keys.
    for k in keys:
        lk = k.lower()
        if lk in ("ohlcv", "data", "bars", "arr_0", "cache", "x"):
            a = np.asarray(z[k])
            if a.ndim == 3 and a.shape[-1] >= 4:
                return k, a

    # Any 3D numeric array with last dimension >= 4.
    for k in keys:
        a = np.asarray(z[k])
        if np.issubdtype(a.dtype, np.number) and a.ndim == 3 and a.shape[-1] >= 4:
            return k, a

    return None, None


def detect_npz_field_order(keys: Sequence[str]) -> List[str]:
    # Most project caches use OHLCV order.
    return ["open", "high", "low", "close", "volume"]


def load_npz_ohlcv(npz_path: str, symbols_filter: Optional[Sequence[str]] = None) -> pd.DataFrame:
    print(f"[npz] inspecting {npz_path}")
    with np.load(npz_path, allow_pickle=True) as z:
        keys = list(z.keys())
        print("[npz] keys:", ", ".join(keys[:30]) + ("..." if len(keys) > 30 else ""))

        symbols_key = find_key(keys, ["symbols", "symbol", "tickers", "pairs", "universe"])
        time_key = find_key(keys, ["timestamps", "timestamp", "times", "time", "datetime_utc", "datetime", "ts"])

        symbols: Optional[List[str]] = None
        if symbols_key:
            symbols = decode_symbols_array(z[symbols_key])
            print(f"[npz] symbols_key={symbols_key}, n_symbols={len(symbols)}")

        times: Optional[pd.Series] = None
        if time_key:
            times = maybe_datetime_from_numeric(np.asarray(z[time_key]))
            print(f"[npz] time_key={time_key}, n_times={len(times)}")

        # Layout 1: separate open/high/low/close arrays.
        k_open = find_key(keys, ["open", "opens", "o"])
        k_high = find_key(keys, ["high", "highs", "h"])
        k_low = find_key(keys, ["low", "lows", "l"])
        k_close = find_key(keys, ["close", "closes", "c"])
        k_vol = find_key(keys, ["volume", "volumes", "v", "quote_volume", "qv"])

        if k_open and k_high and k_low and k_close:
            arrays = {
                "open": np.asarray(z[k_open], dtype=float),
                "high": np.asarray(z[k_high], dtype=float),
                "low": np.asarray(z[k_low], dtype=float),
                "close": np.asarray(z[k_close], dtype=float),
            }
            if k_vol:
                arrays["volume"] = np.asarray(z[k_vol], dtype=float)

            close = arrays["close"]
            if close.ndim != 2:
                raise RuntimeError("Separate OHLC arrays detected but close is not 2D.")

            n0, n1 = close.shape
            if symbols and len(symbols) == n0:
                sym_axis = 0
            elif symbols and len(symbols) == n1:
                sym_axis = 1
            else:
                # If no symbols, use first axis as symbol when it is smaller.
                sym_axis = 0 if n0 <= n1 else 1
                n_syms = close.shape[sym_axis]
                symbols = [f"SYM_{i}" for i in range(n_syms)]

            frames = []
            n_syms = close.shape[sym_axis]
            n_bars = close.shape[1 - sym_axis]
            if times is None or len(times) != n_bars:
                times = pd.Series(pd.date_range("1970-01-01", periods=n_bars, freq="min", tz="UTC"))

            for i in range(n_syms):
                sym = symbols[i] if symbols and i < len(symbols) else f"SYM_{i}"
                if symbols_filter and sym not in symbols_filter:
                    continue
                part = pd.DataFrame({"symbol": sym, "datetime_utc": times.iloc[:n_bars].to_numpy()})
                for name, arr in arrays.items():
                    vals = arr[i, :] if sym_axis == 0 else arr[:, i]
                    part[name] = vals[:n_bars]
                frames.append(part)

            if not frames:
                raise RuntimeError("No symbols left from NPZ after filter.")
            return pd.concat(frames, ignore_index=True)

        # Layout 2: one dense 3D OHLCV array.
        dense_key, a = infer_ohlcv_array(z, keys, symbols)
        if a is not None:
            print(f"[npz] dense_ohlcv_key={dense_key}, shape={a.shape}")
            field_order = detect_npz_field_order(keys)

            # Shape can be [symbols, bars, fields] or [bars, symbols, fields].
            if symbols and len(symbols) == a.shape[0]:
                sym_axis = 0
                n_syms, n_bars = a.shape[0], a.shape[1]
            elif symbols and len(symbols) == a.shape[1]:
                sym_axis = 1
                n_syms, n_bars = a.shape[1], a.shape[0]
            else:
                # Usually fewer symbols than bars.
                sym_axis = 0 if a.shape[0] <= a.shape[1] else 1
                n_syms = a.shape[sym_axis]
                n_bars = a.shape[1 - sym_axis]
                if not symbols:
                    symbols = [f"SYM_{i}" for i in range(n_syms)]

            if times is None or len(times) != n_bars:
                times = pd.Series(pd.date_range("1970-01-01", periods=n_bars, freq="min", tz="UTC"))

            frames = []
            for i in range(n_syms):
                sym = symbols[i] if symbols and i < len(symbols) else f"SYM_{i}"
                if symbols_filter and sym not in symbols_filter:
                    continue

                block = a[i, :, :] if sym_axis == 0 else a[:, i, :]
                block = np.asarray(block, dtype=float)
                part = pd.DataFrame({"symbol": sym, "datetime_utc": times.iloc[:len(block)].to_numpy()})
                for j, name in enumerate(field_order):
                    if j < block.shape[1]:
                        part[name] = block[:, j]
                if "close" not in part.columns:
                    raise RuntimeError("Dense NPZ OHLCV did not expose close column.")
                frames.append(part)

            if not frames:
                raise RuntimeError("No symbols left from NPZ after filter.")
            return pd.concat(frames, ignore_index=True)

        # Layout 3: per-symbol arrays in keys, each shape [bars, fields].
        frames = []
        skip = set([k for k in [symbols_key, time_key] if k])
        for k in keys:
            if k in skip:
                continue
            arr = np.asarray(z[k])
            if not np.issubdtype(arr.dtype, np.number):
                continue
            if arr.ndim == 2 and arr.shape[1] >= 4:
                sym = normalize_symbol(k)
                if symbols_filter and sym not in symbols_filter:
                    continue
                n_bars = arr.shape[0]
                t = times.iloc[:n_bars] if times is not None and len(times) >= n_bars else pd.Series(
                    pd.date_range("1970-01-01", periods=n_bars, freq="min", tz="UTC")
                )
                part = pd.DataFrame({"symbol": sym, "datetime_utc": t.to_numpy()})
                # Assume OHLCV.
                part["open"] = arr[:, 0]
                part["high"] = arr[:, 1]
                part["low"] = arr[:, 2]
                part["close"] = arr[:, 3]
                if arr.shape[1] > 4:
                    part["volume"] = arr[:, 4]
                frames.append(part)

        if frames:
            return pd.concat(frames, ignore_index=True)

    raise RuntimeError(
        "Could not load NPZ. Supported layouts: separate open/high/low/close arrays, dense OHLCV 3D array, or per-symbol 2D arrays."
    )


# -------------------------
# Feature engineering: Akela indicators
# -------------------------

def add_market_proxy(df: pd.DataFrame) -> pd.DataFrame:
    d = df.sort_values(["symbol", "datetime_utc"]).copy()
    d["ret_bar"] = d.groupby("symbol")["close"].pct_change().replace([np.inf, -np.inf], np.nan)

    market = (
        d.groupby("datetime_utc", as_index=False)["ret_bar"]
        .median()
        .rename(columns={"ret_bar": "market_ret_bar"})
    )
    d = d.merge(market, on="datetime_utc", how="left")
    d["rel_ret_bar"] = d["ret_bar"] - d["market_ret_bar"]
    return d


def count_pump_failures(
    close: pd.Series,
    pump_window: int,
    fail_window: int,
    pump_pct: float,
    giveback_frac: float,
) -> Tuple[int, int, float]:
    px = close.to_numpy(dtype=float)
    n = len(px)
    fails = 0
    pumps = 0
    last_pump_i = -10**9

    # Avoid counting the same extended pump hundreds of times.
    cooldown = max(1, pump_window // 4)

    for i in range(pump_window, max(pump_window, n - fail_window)):
        if i - last_pump_i < cooldown:
            continue

        base = px[i - pump_window]
        top = px[i]
        if not np.isfinite(base) or not np.isfinite(top) or base <= 0:
            continue

        pump = top / base - 1.0
        if pump >= pump_pct:
            pumps += 1
            last_pump_i = i
            future_min = np.nanmin(px[i : i + fail_window + 1])
            giveback = safe_div(top - future_min, top - base, 0.0)
            if giveback >= giveback_frac:
                fails += 1

    return fails, pumps, fails / max(1, pumps)


def count_new_buyer_failures(
    g: pd.DataFrame,
    vol_col: Optional[str],
    lookahead: int,
    min_green_pct: float,
    volume_spike_mult: float,
) -> Tuple[int, int, float]:
    if vol_col is None or vol_col not in g.columns:
        return 0, 0, 0.0

    close = g["close"].to_numpy(dtype=float)
    open_ = g["open"].to_numpy(dtype=float)
    vol = g[vol_col].to_numpy(dtype=float)

    vma = pd.Series(vol).rolling(96, min_periods=20).median().to_numpy(dtype=float)
    events = 0
    fails = 0
    n = len(g)

    for i in range(1, max(1, n - lookahead)):
        if not np.isfinite(open_[i]) or not np.isfinite(close[i]) or open_[i] <= 0:
            continue
        green = close[i] / open_[i] - 1.0
        spike = np.isfinite(vol[i]) and np.isfinite(vma[i]) and vma[i] > 0 and vol[i] >= volume_spike_mult * vma[i]

        if green >= min_green_pct and spike:
            events += 1
            future_min = np.nanmin(close[i : i + lookahead + 1])
            future_close = close[min(i + lookahead, n - 1)]
            # Fail if price loses the candle body or closes below the spike close after lookahead.
            if future_min <= open_[i] or future_close < close[i]:
                fails += 1

    return fails, events, fails / max(1, events)


def choose_volume_col(df: pd.DataFrame) -> Optional[str]:
    for c in ["quote_volume", "qv_24h", "volume", "vol"]:
        if c in df.columns and pd.to_numeric(df[c], errors="coerce").notna().any():
            return c
    return None


def compute_akela_features(
    df: pd.DataFrame,
    pump_window: int,
    pump_fail_window: int,
    pump_pct: float,
    pump_giveback_frac: float,
    high_lookback: int,
    vol_fail_lookahead: int,
    min_green_pct: float,
    volume_spike_mult: float,
    min_bars: int,
) -> pd.DataFrame:
    d = add_market_proxy(df)
    vol_col = choose_volume_col(d)
    rows = []

    for sym, g in d.groupby("symbol", sort=False):
        g = g.sort_values("datetime_utc").reset_index(drop=True)
        if len(g) < min_bars:
            continue

        first = float(g["close"].iloc[0])
        last = float(g["close"].iloc[-1])
        if first <= 0 or last <= 0:
            continue

        ret_total_pct = (last / first - 1.0) * 100.0

        market_curve = (1.0 + g["market_ret_bar"].fillna(0.0)).cumprod()
        market_total_pct = (float(market_curve.iloc[-1]) - 1.0) * 100.0
        rel_total_pct = ret_total_pct - market_total_pct

        mg = g[g["market_ret_bar"] > 0]
        mr = g[g["market_ret_bar"] < 0]
        up_capture = safe_div(mg["ret_bar"].mean(), mg["market_ret_bar"].mean(), 0.0) if len(mg) else 0.0
        down_capture = safe_div(mr["ret_bar"].mean(), mr["market_ret_bar"].mean(), 0.0) if len(mr) else 0.0

        weak_up_score_raw = max(0.0, 1.0 - up_capture)
        down_capture_score_raw = max(0.0, down_capture - 1.0)

        hlb = min(high_lookback, len(g))
        recent_high = float(g["close"].rolling(hlb, min_periods=max(5, hlb // 5)).max().iloc[-1])
        distance_to_recent_high_pct = (recent_high - last) / recent_high * 100.0 if recent_high > 0 else 0.0

        ath = float(g["close"].max())
        distance_to_window_ath_pct = (ath - last) / ath * 100.0 if ath > 0 else 0.0
        idx_ath = int(g["close"].idxmax())
        bars_since_window_ath = len(g) - 1 - idx_ath

        failed_ath_score_raw = (
            max(0.0, distance_to_window_ath_pct / 35.0)
            * max(0.2, min(1.5, bars_since_window_ath / max(10, hlb)))
        )

        pump_fails, pump_events, pump_fail_ratio = count_pump_failures(
            g["close"], pump_window, pump_fail_window, pump_pct, pump_giveback_frac
        )
        nb_fails, nb_events, nb_fail_ratio = count_new_buyer_failures(
            g, vol_col, vol_fail_lookahead, min_green_pct, volume_spike_mult
        )

        if "atr_ratio" in g.columns:
            atr_med = float(pd.to_numeric(g["atr_ratio"], errors="coerce").median())
        else:
            tr_pct = (g["high"] / g["low"] - 1.0).replace([np.inf, -np.inf], np.nan)
            atr_med = float(tr_pct.rolling(96, min_periods=10).mean().median())

        bar_vol_pct = float(g["ret_bar"].std(skipna=True) * 100.0)

        intrabar_range = (g["high"] / g["low"] - 1.0).replace([np.inf, -np.inf], np.nan)
        pullback_events = int((intrabar_range >= 0.010).sum())
        pullback_events_per_1000b = pullback_events * 1000.0 / max(1, len(g))

        qv_med = float(pd.to_numeric(g[vol_col], errors="coerce").median()) if vol_col else 0.0

        rows.append({
            "symbol": sym,
            "bars": len(g),
            "ret_total_pct": ret_total_pct,
            "market_total_pct": market_total_pct,
            "rel_total_pct": rel_total_pct,
            "up_capture": up_capture,
            "down_capture": down_capture,
            "weak_up_score_raw": weak_up_score_raw,
            "down_capture_score_raw": down_capture_score_raw,
            "distance_to_recent_high_pct": distance_to_recent_high_pct,
            "distance_to_window_ath_pct": distance_to_window_ath_pct,
            "bars_since_window_ath": bars_since_window_ath,
            "failed_ath_score_raw": failed_ath_score_raw,
            "pump_fail_count": pump_fails,
            "pump_event_count": pump_events,
            "pump_fail_ratio": pump_fail_ratio,
            "new_buyer_fail_count": nb_fails,
            "new_buyer_event_count": nb_events,
            "new_buyer_fail_ratio": nb_fail_ratio,
            "atr_ratio_median": atr_med,
            "bar_vol_pct": bar_vol_pct,
            "pullback_events_per_1000b": pullback_events_per_1000b,
            "quote_volume_median": qv_med,
        })

    feat = pd.DataFrame(rows)
    if feat.empty:
        return feat

    # Cross-sectional scores. Higher = better for short basket.
    feat["rs_weak_score"] = (-feat["rel_total_pct"]).rank(pct=True)
    feat["pump_selloff_score"] = feat["pump_fail_ratio"].rank(pct=True)
    feat["buyer_failure_score"] = feat["new_buyer_fail_ratio"].rank(pct=True)
    feat["failed_high_score"] = feat["failed_ath_score_raw"].rank(pct=True)
    feat["scalp_opportunity_score"] = feat["pullback_events_per_1000b"].rank(pct=True)
    feat["volatility_score"] = feat["bar_vol_pct"].rank(pct=True)
    feat["liquidity_score"] = feat["quote_volume_median"].rank(pct=True) if feat["quote_volume_median"].max() > 0 else 0.5

    feat["akela_score"] = (
        1.50 * feat["rs_weak_score"]
        + 1.25 * feat["pump_selloff_score"]
        + 1.15 * feat["buyer_failure_score"]
        + 1.05 * feat["failed_high_score"]
        + 0.90 * feat["scalp_opportunity_score"]
        + 0.60 * feat["volatility_score"]
        + 0.45 * feat["liquidity_score"]
        + 0.70 * feat["weak_up_score_raw"].clip(0, 2)
        + 0.70 * feat["down_capture_score_raw"].clip(0, 2)
    )

    return feat


# -------------------------
# Optional strategy backtest
# -------------------------

def rows_for_backtest(df: pd.DataFrame) -> Dict[str, List[dict]]:
    cols = ["symbol", "datetime_utc", "open", "high", "low", "close"]
    for c in ["atr_ratio", "dp6h", "dp12h", "quote_volume", "qv_24h", "volume", "vol"]:
        if c in df.columns:
            cols.append(c)

    clean = df[cols].copy()
    clean["datetime_utc"] = clean["datetime_utc"].astype(str)

    out: Dict[str, List[dict]] = {}
    for sym, g in clean.groupby("symbol", sort=False):
        out[sym] = g.sort_values("datetime_utc").to_dict("records")
    return out


def get_attr_or_dict(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def try_strategy_methods(strat: Any, sym: str, row: dict, position: Optional[Position]) -> Tuple[Optional[Any], Optional[Any]]:
    """
    Returns (entry_signal, exit_signal). Supports a few likely strategy APIs.
    """
    entry_sig = None
    exit_sig = None

    if position is not None:
        for name in ("manage_position", "on_position", "exit_signal"):
            fn = getattr(strat, name, None)
            if callable(fn):
                try:
                    exit_sig = fn(sym, row, position, ctx=None)
                    break
                except TypeError:
                    try:
                        exit_sig = fn(sym, row, position)
                        break
                    except TypeError:
                        pass
                    except Exception:
                        raise
                except Exception:
                    raise

    if position is None:
        for name in ("entry_signal", "should_enter", "on_bar"):
            fn = getattr(strat, name, None)
            if callable(fn):
                try:
                    # Old known API: entry_signal(True, symbol, row, ctx=None)
                    if name == "entry_signal":
                        entry_sig = fn(True, sym, row, ctx=None)
                    else:
                        entry_sig = fn(sym, row, ctx=None)
                    break
                except TypeError:
                    try:
                        entry_sig = fn(sym, row)
                        break
                    except TypeError:
                        pass
                    except Exception:
                        raise
                except Exception:
                    raise

    return entry_sig, exit_sig


def run_short_symbol(rows: List[dict], cfg: dict, strategy_class_short: Optional[str]) -> dict:
    if not rows:
        return {}

    class_path = resolve_strategy_class_short(cfg, strategy_class_short)
    if not class_path:
        raise RuntimeError("No strategy class found. Pass --strategy-class-short or set strategy_class_short in YAML.")

    Strat = import_by_path(class_path)
    strat = Strat(cfg)

    portfolio = cfg.get("portfolio", {}) or {}
    initial_equity = float(portfolio.get("initial_equity_per_leg", portfolio.get("initial_equity", 100.0)))
    fee = float(portfolio.get("fee_rate", cfg.get("fee_rate", 0.0)))
    slippage = float(portfolio.get("slippage_per_side", cfg.get("slippage_per_side", 0.0)))

    first_usdt = float(
        getattr(strat, "first_usdt", 0.0)
        or cfg.get("first_usdt", 0.0)
        or (initial_equity * float(cfg.get("base_order_pct", 0.01)))
    )

    equity_realized = initial_equity
    position: Optional[Position] = None
    trades = wins = losses = 0
    pnl_pos = pnl_neg = 0.0
    eq_real_curve = [initial_equity]
    eq_mtm_curve = [initial_equity]

    def unreal(px: float) -> float:
        if position is None:
            return 0.0
        gross_ret = (position.entry - px) / max(position.entry, 1e-12)
        net_ret = gross_ret - 2.0 * slippage - 2.0 * fee
        return net_ret * (position.entry * position.qty)

    for row in rows:
        px = float(row["close"])
        sym = row["symbol"]

        entry_sig, exit_sig = try_strategy_methods(strat, sym, row, position)

        if position is not None and exit_sig is not None:
            action = get_attr_or_dict(exit_sig, "action", None)

            if action in ("TP", "SL", "EXIT", "CLOSE", "COVER"):
                exit_px = float(get_attr_or_dict(exit_sig, "exit_price", px) or px)
                notional = position.entry * position.qty
                gross_ret = (position.entry - exit_px) / max(position.entry, 1e-12)
                net_ret = gross_ret - 2.0 * slippage - 2.0 * fee
                pnl = net_ret * notional
                equity_realized += pnl
                trades += 1
                if pnl > 0:
                    wins += 1
                    pnl_pos += pnl
                else:
                    losses += 1
                    pnl_neg += pnl
                position = None

            elif action in ("TP_PARTIAL", "PARTIAL", "PARTIAL_CLOSE"):
                qty_frac = float(get_attr_or_dict(exit_sig, "qty_frac", 0.0) or 0.0)
                qty_frac = max(0.0, min(1.0, qty_frac))
                qty_close = position.qty * qty_frac
                if qty_close > 0:
                    exit_px = float(get_attr_or_dict(exit_sig, "exit_price", px) or px)
                    notional = position.entry * qty_close
                    gross_ret = (position.entry - exit_px) / max(position.entry, 1e-12)
                    net_ret = gross_ret - 2.0 * slippage - 2.0 * fee
                    pnl = net_ret * notional
                    equity_realized += pnl
                    trades += 1
                    if pnl > 0:
                        wins += 1
                        pnl_pos += pnl
                    else:
                        losses += 1
                        pnl_neg += pnl
                    position.qty -= qty_close
                    if position.qty <= 1e-12:
                        position = None

        if position is None and entry_sig is not None:
            action = get_attr_or_dict(entry_sig, "action", "SHORT")
            # Accept common short entry actions.
            if action is None or str(action).upper() in ("SHORT", "SELL", "ENTER_SHORT", "OPEN_SHORT"):
                tp = get_attr_or_dict(entry_sig, "tp", math.nan)
                sl = get_attr_or_dict(entry_sig, "sl", math.nan)
                qty = first_usdt / max(px, 1e-12)
                if qty > 0:
                    position = Position(
                        side="SHORT",
                        entry=px,
                        sl=float(sl) if sl is not None else math.nan,
                        tp=float(tp) if tp is not None else math.nan,
                        qty=qty,
                    )

        eq_real_curve.append(equity_realized)
        eq_mtm_curve.append(equity_realized + unreal(px))

    pf = safe_div(pnl_pos, -pnl_neg, 0.0) if pnl_pos > 0 and pnl_neg < 0 else 0.0
    return_pct = (equity_realized / initial_equity - 1.0) * 100.0 if initial_equity else 0.0

    return {
        "symbol": rows[0]["symbol"],
        "bt_bars": len(rows),
        "equity_end_realized": equity_realized,
        "realized_pnl": equity_realized - initial_equity,
        "return_pct": return_pct,
        "trades": trades,
        "win_rate_pct": wins * 100.0 / max(1, trades),
        "profit_factor": pf,
        "mdd_realized_pct": max_drawdown_pct(eq_real_curve),
        "mdd_mtm_pct": max_drawdown_pct(eq_mtm_curve),
    }


def run_optional_backtests(
    df: pd.DataFrame,
    cfg: dict,
    strategy_class_short: Optional[str],
    no_backtest: bool,
    progress_every: int,
) -> pd.DataFrame:
    if no_backtest:
        return pd.DataFrame({"symbol": sorted(df["symbol"].unique())})

    groups = rows_for_backtest(df)
    results = []
    total = len(groups)

    for i, (sym, rows) in enumerate(groups.items(), 1):
        try:
            results.append(run_short_symbol(rows, cfg, strategy_class_short))
        except Exception as e:
            results.append({
                "symbol": sym,
                "bt_error": repr(e),
                "bt_bars": len(rows),
                "return_pct": np.nan,
                "trades": 0,
                "profit_factor": np.nan,
                "mdd_mtm_pct": np.nan,
                "mdd_realized_pct": np.nan,
            })

        if progress_every and (i % progress_every == 0 or i == total):
            print(f"[progress] backtested {i}/{total}")

    return pd.DataFrame(results)


# -------------------------
# Combine scores and output
# -------------------------

def combine_scores(
    features: pd.DataFrame,
    bt: pd.DataFrame,
    akela_weight: float,
    bt_weight: float,
    min_trades: int,
    max_mtm_dd: float,
    min_quote_volume: float,
    no_backtest: bool,
) -> pd.DataFrame:
    merged = features.merge(bt, on="symbol", how="left")

    if min_quote_volume > 0 and "quote_volume_median" in merged.columns:
        merged = merged[merged["quote_volume_median"].fillna(0) >= min_quote_volume]

    if not no_backtest:
        if "trades" in merged.columns:
            merged = merged[merged["trades"].fillna(0) >= min_trades]
        if max_mtm_dd < 0 and "mdd_mtm_pct" in merged.columns:
            merged = merged[merged["mdd_mtm_pct"].fillna(-9999) >= max_mtm_dd]

    if merged.empty:
        return merged

    if no_backtest or "return_pct" not in merged.columns:
        merged["short_bt_score"] = 0.0
        merged["final_short_score"] = merged["akela_score"]
    else:
        merged["bt_return_score"] = merged["return_pct"].fillna(-1e9).rank(pct=True)
        merged["bt_pf_score"] = merged["profit_factor"].fillna(0).replace([np.inf, -np.inf], 0).rank(pct=True)
        merged["bt_win_score"] = merged["win_rate_pct"].fillna(0).rank(pct=True)
        merged["bt_mdd_score"] = merged["mdd_mtm_pct"].fillna(-9999).rank(pct=True)
        merged["short_bt_score"] = (
            1.30 * merged["bt_return_score"]
            + 0.85 * merged["bt_pf_score"]
            + 0.45 * merged["bt_win_score"]
            + 0.90 * merged["bt_mdd_score"]
        )
        merged["final_short_score"] = (
            akela_weight * merged["akela_score"].fillna(0)
            + bt_weight * merged["short_bt_score"].fillna(0)
        )

    sort_cols = ["final_short_score", "akela_score"]
    ascending = [False, False]
    if "return_pct" in merged.columns:
        sort_cols.append("return_pct")
        ascending.append(False)
    if "mdd_mtm_pct" in merged.columns:
        sort_cols.append("mdd_mtm_pct")
        ascending.append(False)

    merged = merged.sort_values(sort_cols, ascending=ascending)

    front = [
        "symbol",
        "final_short_score",
        "akela_score",
        "short_bt_score",
        "return_pct",
        "realized_pnl",
        "trades",
        "win_rate_pct",
        "profit_factor",
        "mdd_mtm_pct",
        "mdd_realized_pct",
        "rel_total_pct",
        "ret_total_pct",
        "market_total_pct",
        "up_capture",
        "down_capture",
        "pump_fail_count",
        "pump_event_count",
        "pump_fail_ratio",
        "new_buyer_fail_count",
        "new_buyer_event_count",
        "new_buyer_fail_ratio",
        "distance_to_window_ath_pct",
        "bars_since_window_ath",
        "pullback_events_per_1000b",
        "quote_volume_median",
        "bar_vol_pct",
        "atr_ratio_median",
        "bt_error",
    ]
    cols = [c for c in front if c in merged.columns] + [c for c in merged.columns if c not in front]
    return merged[cols]


def write_outputs(df: pd.DataFrame, out: str, json_out: str, top: int) -> None:
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)

    if json_out:
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        df.head(top).to_json(json_out, orient="records", force_ascii=False, indent=2)

    print("\n[AKELA SHORT CANDIDATES]")
    if df.empty:
        print("[empty]")
    else:
        max_cols = min(28, len(df.columns))
        print(df.head(top).iloc[:, :max_cols].to_string(index=False))

    print(f"\n[files] {out}")
    if json_out:
        print(f"[files] {json_out}")


# -------------------------
# Main
# -------------------------

def parse_symbols_arg(s: str) -> Optional[List[str]]:
    if not s:
        return None
    p = Path(s)
    if p.exists():
        return [line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [x.strip() for x in s.split(",") if x.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()

    # Sources.
    ap.add_argument("--db", default="", help="SQLite DB path, e.g. ../DB/combined_cache_3m_7200_500u.db")
    ap.add_argument("--db-table", default="", help="Optional explicit SQLite table name.")
    ap.add_argument("--npz", default="", help="Optional NPZ path, e.g. ../DB/fast_cache_1m_short_top15_5000b.npz")
    ap.add_argument("--prefer", choices=["db", "npz", "both"], default="db")
    ap.add_argument("--symbols", default="", help="Comma symbols or path to universe file.")

    # Strategy backtest.
    ap.add_argument("--cfg", default="", help="YAML config. Required unless --no-backtest.")
    ap.add_argument("--strategy-class-short", default="", help="e.g. cryptomine_pack_dual_full_partial_fix_v1.ShortStrategy")
    ap.add_argument("--no-backtest", action="store_true", help="Only compute Akela structural ranking.")

    # Time/filtering.
    ap.add_argument("--time-from", default=None)
    ap.add_argument("--time-to", default=None)
    ap.add_argument("--min-bars", type=int, default=500)
    ap.add_argument("--min-trades", type=int, default=5)
    ap.add_argument("--min-quote-volume", type=float, default=0.0)
    ap.add_argument("--max-mtm-dd", type=float, default=0.0, help="e.g. -40 keeps mdd_mtm_pct >= -40")

    # Output.
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--out", default="_reports/akela_short_candidates.csv")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--progress-every", type=int, default=50)

    # Scoring weights.
    ap.add_argument("--akela-weight", type=float, default=0.60)
    ap.add_argument("--bt-weight", type=float, default=0.40)

    # Indicator params.
    ap.add_argument("--bar-minutes", type=float, default=3.0, help="Only for your reference/logging.")
    ap.add_argument("--pump-window", type=int, default=480, help="3m: 480=24h. 1m: use 1440.")
    ap.add_argument("--pump-fail-window", type=int, default=160, help="3m: 160=8h. 1m: use 480.")
    ap.add_argument("--pump-pct", type=float, default=0.10)
    ap.add_argument("--pump-giveback-frac", type=float, default=0.50)
    ap.add_argument("--high-lookback", type=int, default=2400, help="3m: 2400=5d. 1m: use 7200.")
    ap.add_argument("--vol-fail-lookahead", type=int, default=96, help="3m: 96=4.8h. 1m: use 288.")
    ap.add_argument("--min-green-pct", type=float, default=0.018)
    ap.add_argument("--volume-spike-mult", type=float, default=2.0)

    args = ap.parse_args()

    symbols = parse_symbols_arg(args.symbols)
    cfg = load_yaml(args.cfg) if args.cfg else {}

    if not args.no_backtest and not args.cfg:
        print("[warn] --cfg not provided, switching to --no-backtest")
        args.no_backtest = True

    frames = []

    if args.prefer in ("db", "both") and args.db:
        df_db = load_db_ohlcv(
            args.db,
            table=args.db_table or None,
            time_from=args.time_from,
            time_to=args.time_to,
            symbols=symbols,
        )
        df_db["source"] = "db"
        frames.append(df_db)

    if args.prefer in ("npz", "both") and args.npz:
        df_npz = load_npz_ohlcv(args.npz, symbols_filter=symbols)
        df_npz["source"] = "npz"
        frames.append(df_npz)

    if not frames:
        raise RuntimeError("No data source loaded. Pass --db and/or --npz.")

    df = pd.concat(frames, ignore_index=True)
    df["symbol"] = df["symbol"].map(normalize_symbol)
    df = df.dropna(subset=["symbol", "datetime_utc", "open", "high", "low", "close"])
    df = df.sort_values(["symbol", "datetime_utc"]).reset_index(drop=True)

    # If both sources overlap, keep all rows but deduplicate exact timestamp/symbol.
    df = df.drop_duplicates(subset=["symbol", "datetime_utc"], keep="last")

    counts = df.groupby("symbol").size()
    keep_syms = counts[counts >= args.min_bars].index
    df = df[df["symbol"].isin(keep_syms)].copy()

    if df.empty:
        raise RuntimeError("No symbols left after --min-bars filter.")

    print(f"[data] symbols={df['symbol'].nunique()} rows={len(df)} bar_minutes={args.bar_minutes}")

    features = compute_akela_features(
        df=df,
        pump_window=args.pump_window,
        pump_fail_window=args.pump_fail_window,
        pump_pct=args.pump_pct,
        pump_giveback_frac=args.pump_giveback_frac,
        high_lookback=args.high_lookback,
        vol_fail_lookahead=args.vol_fail_lookahead,
        min_green_pct=args.min_green_pct,
        volume_spike_mult=args.volume_spike_mult,
        min_bars=args.min_bars,
    )

    if features.empty:
        raise RuntimeError("No Akela features generated.")

    bt = run_optional_backtests(
        df=df,
        cfg=cfg,
        strategy_class_short=args.strategy_class_short or None,
        no_backtest=args.no_backtest,
        progress_every=args.progress_every,
    )

    ranked = combine_scores(
        features=features,
        bt=bt,
        akela_weight=args.akela_weight,
        bt_weight=args.bt_weight,
        min_trades=args.min_trades,
        max_mtm_dd=args.max_mtm_dd,
        min_quote_volume=args.min_quote_volume,
        no_backtest=args.no_backtest,
    )

    write_outputs(ranked, args.out, args.json_out, args.top)


if __name__ == "__main__":
    main()
