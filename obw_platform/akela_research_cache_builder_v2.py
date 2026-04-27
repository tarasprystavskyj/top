#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
akela_research_cache_builder_v1.py

Research-cache builder for Akela / multi-currency meta-short work.

It is designed as a layer ABOVE the existing OBW framework:
  - fetch_build_cache_and_fast_v1.py already fetches OHLCV/trades and writes price_indicators DB + fast NPZ.
  - rank_short_leg_all_symbols_akela_v2.py / monthly_akela_phase_proxybt.py already consume DB/NPZ.

This script adds the research dataset layer:
  1) Convert an existing price_indicators SQLite DB to the standard flat fast NPZ.
  2) Enrich a DB/NPZ with derived features, cross-sectional features, forward labels,
     trainable_mask, missing_mask, quality_score.
  3) Optionally call the existing framework fetch_build_cache_and_fast_v1.py first,
     then enrich the resulting DB/NPZ.
  4) Optionally fetch best-effort CURRENT futures/orderbook snapshots into CSV/SQLite.

Important:
  - Historical orderbook snapshots are not available through generic ccxt REST for most exchanges.
    --fetch-market-snapshots writes current snapshots only. Run it periodically if you need history.
  - Futures metrics availability depends on exchange/ccxt support.
  - Features are computed using data <= t. Labels use data > t.

Recommended location:
  obw_platform/research/akela_research_cache_builder_v1.py

Example 1: convert + enrich existing DB:
  python3 obw_platform/research/akela_research_cache_builder_v1.py \
    enrich-db \
    --db DB/combined_cache_5m_5000_04.09.db \
    --raw-npz DB/combined_cache_5m_5000_04.09.raw.npz \
    --out-npz DB/combined_cache_5m_5000_04.09.akela_research.npz \
    --reports-dir _reports/akela_research_5m5000 \
    --timeframe-sec 300 \
    --indicator-workers 12 \
    --build-cross-sectional

Example 2: enrich existing NPZ:
  python3 obw_platform/research/akela_research_cache_builder_v1.py \
    enrich-npz \
    --npz DB/fast_cache_akela_shortlist_1m_30d.npz \
    --out-npz DB/fast_cache_akela_shortlist_1m_30d.akela_research.npz \
    --reports-dir _reports/akela_research_shortlist \
    --timeframe-sec 60 \
    --indicator-workers 8 \
    --build-cross-sectional

Example 3: use existing framework fetcher, then enrich:
  python3 obw_platform/research/akela_research_cache_builder_v1.py \
    fetch-build-enrich \
    --framework-fetch-script obw_platform/fetch_build_cache_and_fast_v1.py \
    --universe-file obw_platform/universe/universe_akela_top200.txt \
    --exchange bingx \
    --ccxt-symbol-format usdtm \
    --timeframe 1m \
    --start "2026-03-27 00:00:00" \
    --end "2026-04-27 00:00:00" \
    --db DB/akela_top200_1m_30d.db \
    --raw-npz DB/akela_top200_1m_30d.raw.npz \
    --out-npz DB/akela_top200_1m_30d.akela_research.npz \
    --reports-dir _reports/akela_top200_1m_30d \
    --feature-set none \
    --indicator-workers 12 \
    --build-cross-sectional
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    import ccxt  # type: ignore
except Exception:  # pragma: no cover
    ccxt = None


# -------------------------
# Generic helpers
# -------------------------

TIME_COL_CANDIDATES = ["datetime_utc", "timestamp_s", "timestamp", "time", "datetime", "date", "ts"]
BASE_COLS = {"symbol", "datetime_utc", "timestamp_s", "open", "high", "low", "close", "volume", "volume_base", "volume_quote", "quote_volume", "trade_count"}
FRAMEWORK_PLACEHOLDERS = {"rsi", "stochastic", "mfi", "overbought_index"}


def log(msg: str) -> None:
    print(msg, flush=True)


def norm_symbol(x: Any) -> str:
    if isinstance(x, bytes):
        return x.decode("utf-8", errors="ignore")
    return str(x).strip()


def safe_float(x: Any, default: float = np.nan) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def parse_ref_symbols(s: str) -> List[str]:
    return [x.strip() for x in str(s).split(",") if x.strip()]


def timeframe_to_sec(tf: str) -> int:
    tf = str(tf).strip().lower()
    if not tf:
        raise ValueError("empty timeframe")
    unit = tf[-1]
    n = int(tf[:-1])
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if unit not in mult:
        raise ValueError(f"unsupported timeframe: {tf}")
    return n * mult[unit]


def maybe_open_zip_db(path: str, extract_dir: str) -> str:
    """If path is a zip with a single DB, extract safely and return DB path. Otherwise return original."""
    p = Path(path)
    if p.suffix.lower() != ".zip":
        return str(p)
    outdir = Path(extract_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p) as z:
        db_members = [m for m in z.infolist() if m.filename.lower().endswith(".db")]
        if not db_members:
            raise SystemExit(f"No .db inside zip: {path}")
        member = db_members[0]
        target = outdir / Path(member.filename).name
        if target.exists() and target.stat().st_size == member.file_size:
            log(f"[zip] already extracted {target}")
            return str(target)
        log(f"[zip] extracting {member.filename} -> {target} bytes={member.file_size}")
        with z.open(member) as src, open(target, "wb") as dst:
            while True:
                chunk = src.read(4 * 1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        return str(target)

def maybe_open_zip_npz(path: str, extract_dir: str = "DB/_extracted") -> str:
    """If path is a zip with a single NPZ, extract safely and return NPZ path. Otherwise return original."""
    p = Path(path)
    if p.suffix.lower() != ".zip":
        return str(p)
    outdir = Path(extract_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p) as z:
        npz_members = [m for m in z.infolist() if m.filename.lower().endswith(".npz")]
        if not npz_members:
            raise SystemExit(f"No .npz inside zip: {path}")
        member = npz_members[0]
        target = outdir / Path(member.filename).name
        if target.exists() and target.stat().st_size == member.file_size:
            log(f"[zip] already extracted {target}")
            return str(target)
        log(f"[zip] extracting {member.filename} -> {target} bytes={member.file_size}")
        with z.open(member) as src, open(target, "wb") as dst:
            while True:
                chunk = src.read(4 * 1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        return str(target)


# -------------------------
# SQLite DB reader/converter
# -------------------------


def sqlite_header_check(db_path: str) -> dict:
    p = Path(db_path)
    b = p.read_bytes()[:100]
    if b[:16] != b"SQLite format 3\x00":
        return {"ok": False, "error": "not a SQLite 3 database header"}
    page_size = int.from_bytes(b[16:18], "big")
    if page_size == 1:
        page_size = 65536
    page_count = int.from_bytes(b[28:32], "big")
    expected = page_size * page_count
    actual = p.stat().st_size
    return {
        "ok": actual >= expected,
        "page_size": page_size,
        "page_count": page_count,
        "expected_bytes": expected,
        "actual_bytes": actual,
        "actual_expected_ratio": actual / expected if expected else None,
    }


def sqlite_quick_check(db_path: str) -> str:
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    try:
        row = con.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "no result"
    finally:
        con.close()


def list_tables(con: sqlite3.Connection) -> List[str]:
    return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]


def table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def detect_ohlcv_table(db_path: str, preferred: str = "") -> Tuple[str, Dict[str, str]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    try:
        tables = list_tables(con)
        if preferred:
            tables = [preferred] + [t for t in tables if t != preferred]
        for table in tables:
            cols = table_columns(con, table)
            low = {c.lower(): c for c in cols}
            if "symbol" not in low:
                continue
            if not all(k in low for k in ["open", "high", "low", "close"]):
                continue
            time_col = None
            for cand in TIME_COL_CANDIDATES:
                if cand in low:
                    time_col = low[cand]
                    break
            if not time_col:
                continue
            mapping = {
                "symbol": low["symbol"],
                "time": time_col,
                "open": low["open"],
                "high": low["high"],
                "low": low["low"],
                "close": low["close"],
            }
            for opt in ["volume", "volume_base", "quote_volume", "volume_quote", "trade_count", "atr_ratio", "gain_24h_before", "dp6h", "dp12h", "qv_24h", "vol_surge_mult"]:
                if opt in low:
                    mapping[opt] = low[opt]
            return table, mapping
    finally:
        con.close()
    raise SystemExit("Could not detect OHLCV table. Need symbol, time, open, high, low, close.")


def db_to_raw_npz(
    db_path: str,
    out_npz: str,
    table: str = "",
    symbols_file: str = "",
    include_optional: bool = True,
    chunksize: int = 250_000,
    skip_integrity_check: bool = False,
) -> str:
    if not skip_integrity_check:
        header = sqlite_header_check(db_path)
        if not header.get("ok"):
            raise SystemExit("DB looks truncated:\n" + json.dumps(header, indent=2))
        qc = sqlite_quick_check(db_path)
        if qc.lower() != "ok":
            raise SystemExit(f"DB quick_check failed: {qc}")

    table_name, m = detect_ohlcv_table(db_path, table)
    log(f"[db] detected table={table_name} mapping={m}")

    select_map = {
        "symbol": m["symbol"],
        "time": m["time"],
        "open": m["open"],
        "high": m["high"],
        "low": m["low"],
        "close": m["close"],
    }
    if "volume" in m:
        select_map["volume"] = m["volume"]
    elif "volume_base" in m:
        select_map["volume"] = m["volume_base"]
    if "quote_volume" in m:
        select_map["quote_volume"] = m["quote_volume"]
    elif "volume_quote" in m:
        select_map["quote_volume"] = m["volume_quote"]
    if "trade_count" in m:
        select_map["trade_count"] = m["trade_count"]

    if include_optional:
        for opt in ["atr_ratio", "gain_24h_before", "dp6h", "dp12h", "qv_24h", "vol_surge_mult"]:
            if opt in m and opt not in select_map:
                select_map[opt] = m[opt]

    select_items = [f"{col} AS {alias}" for alias, col in select_map.items()]

    syms: List[str] = []
    if symbols_file:
        syms = [x.strip() for x in Path(symbols_file).read_text(encoding="utf-8").splitlines() if x.strip() and not x.strip().startswith("#")]

    where = ""
    params: List[Any] = []
    if syms:
        where = " WHERE %s IN (%s)" % (m["symbol"], ",".join(["?"] * len(syms)))
        params.extend(syms)

    sql = f"SELECT {', '.join(select_items)} FROM {table_name}{where} ORDER BY {m['symbol']} ASC, {m['time']} ASC"
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)

    symbols: List[str] = []
    offsets = [0]
    parts: Dict[str, List[np.ndarray]] = {
        "timestamp_s": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "quote_volume": [], "trade_count": []
    }
    extras: Dict[str, List[np.ndarray]] = {}
    cur_sym: Optional[str] = None
    cur_chunks: List[pd.DataFrame] = []
    pos = 0

    def flush(sym: Optional[str], chunks: List[pd.DataFrame]) -> None:
        nonlocal pos
        if sym is None or not chunks:
            return
        part = pd.concat(chunks, ignore_index=True).sort_values("time")
        n = len(part)
        if n <= 0:
            return
        symbols.append(str(sym))
        dt = pd.to_datetime(part["time"], utc=True, errors="coerce")
        # If datetime parsing failed and time is numeric, infer seconds/ms/us/ns.
        if dt.isna().mean() > 0.5:
            raw = pd.to_numeric(part["time"], errors="coerce")
            med = raw.dropna().median() if raw.notna().any() else np.nan
            unit = "s"
            if np.isfinite(med):
                if med > 1e17:
                    unit = "ns"
                elif med > 1e14:
                    unit = "us"
                elif med > 1e11:
                    unit = "ms"
            dt = pd.to_datetime(raw, unit=unit, utc=True, errors="coerce")
        ts = (dt.astype("int64") // 1_000_000_000).to_numpy(dtype=np.int64)
        parts["timestamp_s"].append(ts)
        for c in ["open", "high", "low", "close"]:
            parts[c].append(pd.to_numeric(part[c], errors="coerce").to_numpy(dtype=np.float64))
        if "volume" in part.columns:
            vol = pd.to_numeric(part["volume"], errors="coerce").to_numpy(dtype=np.float64)
        else:
            vol = np.zeros(n, dtype=np.float64)
        parts["volume"].append(vol)
        if "quote_volume" in part.columns:
            qv = pd.to_numeric(part["quote_volume"], errors="coerce").to_numpy(dtype=np.float64)
        else:
            close = np.maximum(pd.to_numeric(part["close"], errors="coerce").to_numpy(dtype=np.float64), 1e-12)
            qv = vol * close
        parts["quote_volume"].append(qv)
        if "trade_count" in part.columns:
            tc = pd.to_numeric(part["trade_count"], errors="coerce").to_numpy(dtype=np.float64)
        else:
            tc = np.full(n, np.nan, dtype=np.float64)
        parts["trade_count"].append(tc)
        for c in part.columns:
            if c in {"symbol", "time", "open", "high", "low", "close", "volume", "quote_volume", "trade_count"}:
                continue
            if c in FRAMEWORK_PLACEHOLDERS:
                continue
            try:
                extras.setdefault(c, []).append(pd.to_numeric(part[c], errors="coerce").to_numpy(dtype=np.float64))
            except Exception:
                pass
        pos += n
        offsets.append(pos)
        if len(symbols) % 50 == 0:
            log(f"[db->npz] symbols={len(symbols)} rows={pos}")

    try:
        for chunk in pd.read_sql_query(sql, con, params=params, chunksize=chunksize):
            for sym, g in chunk.groupby("symbol", sort=False):
                sym = str(sym)
                if cur_sym is None:
                    cur_sym = sym
                if sym != cur_sym:
                    flush(cur_sym, cur_chunks)
                    cur_sym = sym
                    cur_chunks = []
                cur_chunks.append(g)
        flush(cur_sym, cur_chunks)
    finally:
        con.close()

    if not symbols:
        raise SystemExit("No rows converted from DB")

    payload: Dict[str, Any] = {
        "symbols": np.asarray(symbols, dtype=object),
        "offsets": np.asarray(offsets, dtype=np.int64),
    }
    for c, arrs in parts.items():
        payload[c] = np.concatenate(arrs).astype(np.int64 if c == "timestamp_s" else np.float64)
    for c, arrs in extras.items():
        if len(arrs) == len(symbols):
            payload[c] = np.concatenate(arrs).astype(np.float64)
    Path(out_npz).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_npz, **payload)
    meta = {
        "source_db": db_path,
        "table": table_name,
        "symbols": len(symbols),
        "rows": pos,
        "keys": sorted(payload.keys()),
    }
    Path(out_npz).with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[ok] raw npz {out_npz} symbols={len(symbols)} rows={pos}")
    return out_npz


# -------------------------
# NPZ loader
# -------------------------


def load_raw_npz(npz_path: str, min_bars: int = 1) -> Dict[str, pd.DataFrame]:
    with np.load(npz_path, allow_pickle=True) as z:
        keys = set(z.keys())
        required = {"symbols", "offsets", "timestamp_s", "close"}
        missing = required - keys
        if missing:
            raise SystemExit(f"Missing required NPZ keys {sorted(missing)}. Existing={sorted(keys)}")
        symbols = [norm_symbol(x) for x in z["symbols"]]
        offsets = np.asarray(z["offsets"], dtype=np.int64)
        ts_all = np.asarray(z["timestamp_s"], dtype=np.int64)
        # Support both offset conventions: len=S+1 or len=S.
        if len(offsets) == len(symbols):
            offsets = np.r_[offsets, len(ts_all)].astype(np.int64)
        def get(name: str, fallback: Optional[np.ndarray] = None) -> np.ndarray:
            if name in z:
                return np.asarray(z[name], dtype=np.float64)
            if fallback is not None:
                return fallback
            return np.full_like(np.asarray(z["close"], dtype=np.float64), np.nan, dtype=np.float64)
        close_all = np.asarray(z["close"], dtype=np.float64)
        open_all = get("open", close_all)
        high_all = get("high", np.maximum(open_all, close_all))
        low_all = get("low", np.minimum(open_all, close_all))
        volume_all = get("volume", get("volume_base", np.zeros_like(close_all)))
        qv_all = get("quote_volume", get("volume_quote", volume_all * close_all))
        tc_all = get("trade_count")

    out: Dict[str, pd.DataFrame] = {}
    for i, sym in enumerate(symbols):
        s, e = int(offsets[i]), int(offsets[i + 1])
        if e - s < min_bars:
            continue
        df = pd.DataFrame({
            "timestamp_s": ts_all[s:e],
            "datetime_utc": pd.to_datetime(ts_all[s:e], unit="s", utc=True),
            "open": open_all[s:e],
            "high": high_all[s:e],
            "low": low_all[s:e],
            "close": close_all[s:e],
            "volume": volume_all[s:e],
            "quote_volume": qv_all[s:e],
            "trade_count": tc_all[s:e],
        })
        df = df.drop_duplicates("timestamp_s").sort_values("timestamp_s").reset_index(drop=True)
        good = np.isfinite(df["close"].to_numpy()) & (df["close"].to_numpy() > 0)
        df = df.loc[good].reset_index(drop=True)
        if len(df) >= min_bars:
            out[sym] = df
    return out


# -------------------------
# Features and labels
# -------------------------


def bars_for(seconds: int, timeframe_sec: int) -> int:
    return max(1, int(round(seconds / max(1, timeframe_sec))))


def rolling_return(close: pd.Series, n: int) -> pd.Series:
    return close / close.shift(n) - 1.0


def realized_vol(close: pd.Series, n: int) -> pd.Series:
    r = np.log(close / close.shift(1))
    return r.rolling(n, min_periods=max(2, n // 4)).std() * math.sqrt(n)


def atr_pct(df: pd.DataFrame, n: int) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([(df["high"] - df["low"]).abs(), (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=max(2, n // 3)).mean() / df["close"]



def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=max(2, span // 4)).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / max(1, n), adjust=False, min_periods=max(2, n // 2)).mean()
    avg_loss = loss.ewm(alpha=1.0 / max(1, n), adjust=False, min_periods=max(2, n // 2)).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _stoch(close: pd.Series, high: pd.Series, low: pd.Series, n: int = 14) -> pd.Series:
    lo = low.rolling(n, min_periods=max(2, n // 3)).min()
    hi = high.rolling(n, min_periods=max(2, n // 3)).max()
    return (close - lo) / (hi - lo).replace(0, np.nan)


def _zscore(s: pd.Series, n: int) -> pd.Series:
    m = s.rolling(n, min_periods=max(2, n // 4)).mean()
    sd = s.rolling(n, min_periods=max(2, n // 4)).std()
    return (s - m) / sd.replace(0, np.nan)


def _rolling_slope_pct(close: pd.Series, n: int) -> pd.Series:
    """
    Rolling linear-regression slope of log(close), converted to approx percent over the window.
    Uses rolling.apply, intentionally cached because it is too expensive to recompute in every backtest.
    """
    if n <= 2:
        return pd.Series(np.nan, index=close.index)
    x = np.arange(n, dtype=float)
    x_mean = x.mean()
    denom = ((x - x_mean) ** 2).sum()
    logc = np.log(close.replace(0, np.nan))

    def slope_win(y: np.ndarray) -> float:
        if not np.isfinite(y).all():
            return np.nan
        y_mean = y.mean()
        slope = ((x - x_mean) * (y - y_mean)).sum() / denom
        return math.exp(slope * n) - 1.0

    return logc.rolling(n, min_periods=n).apply(slope_win, raw=True)


def _adx(df: pd.DataFrame, n: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / n, adjust=False, min_periods=max(2, n // 2)).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=max(2, n // 2)).mean() / atr.replace(0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=max(2, n // 2)).mean() / atr.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1.0 / n, adjust=False, min_periods=max(2, n // 2)).mean()
    return adx, plus_di, minus_di


def _future_first_hit_bars(close: pd.Series, threshold: float, max_bars: int, direction: str) -> pd.Series:
    """
    Expensive path label. For each bar, returns first future bar index where price crosses threshold.
    direction='down': close_future/close_now - 1 <= -threshold
    direction='up':   close_future/close_now - 1 >= +threshold
    This is O(N * max_bars). Use only for moderate horizons. It is exactly the kind of thing
    that should be cached once, not recomputed in every strategy backtest.
    """
    arr = close.to_numpy(dtype=float)
    n = len(arr)
    out = np.full(n, np.nan, dtype=float)
    max_bars = int(max(1, max_bars))
    for i in range(n - 1):
        base = arr[i]
        if not np.isfinite(base) or base <= 0:
            continue
        end = min(n, i + max_bars + 1)
        fut = arr[i + 1:end]
        if fut.size == 0:
            continue
        ret = fut / base - 1.0
        if direction == "down":
            hit = np.where(ret <= -threshold)[0]
        else:
            hit = np.where(ret >= threshold)[0]
        if hit.size:
            out[i] = float(hit[0] + 1)
    return pd.Series(out, index=close.index)


def compute_features_for_symbol(symbol: str, df: pd.DataFrame, timeframe_sec: int) -> Tuple[str, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float)
    volume = df["volume"].astype(float).replace([np.inf, -np.inf], np.nan)
    qv = df["quote_volume"].astype(float)
    qv = qv.where(np.isfinite(qv), volume * close)
    trade_count = df["trade_count"].astype(float) if "trade_count" in df.columns else pd.Series(np.nan, index=df.index)

    feat = pd.DataFrame({"timestamp_s": df["timestamp_s"].to_numpy(dtype=np.int64)})

    # --- Momentum / returns. Keep many horizons cached; this is used by rankers and labels.
    horizons = {
        "5m": 5 * 60, "15m": 15 * 60, "30m": 30 * 60, "1h": 3600, "2h": 2 * 3600,
        "4h": 4 * 3600, "8h": 8 * 3600, "12h": 12 * 3600, "24h": 24 * 3600,
        "3d": 3 * 86400, "7d": 7 * 86400, "14d": 14 * 86400, "30d": 30 * 86400,
    }
    for name, sec in horizons.items():
        feat[f"ret_{name}"] = rolling_return(close, bars_for(sec, timeframe_sec)).to_numpy()

    one_bar_ret = close.pct_change()
    log_ret = np.log(close / close.shift(1))
    feat["ret_1bar"] = one_bar_ret.to_numpy()
    feat["logret_1bar"] = log_ret.to_numpy()

    # Acceleration / momentum breakdown.
    feat["momentum_1h_minus_4h"] = (feat["ret_1h"] - feat["ret_4h"] / 4.0).to_numpy()
    feat["momentum_4h_minus_24h"] = (feat["ret_4h"] - feat["ret_24h"] / 6.0).to_numpy()
    feat["momentum_breakdown_1h_vs_24h"] = ((feat["ret_24h"] > 0) & (feat["ret_1h"] < 0)).astype(float).to_numpy()
    feat["momentum_breakdown_4h_vs_7d"] = ((feat["ret_7d"] > 0) & (feat["ret_4h"] < 0)).astype(float).to_numpy()

    # Rolling slopes are expensive; cache them.
    for name, sec in [("1h", 3600), ("4h", 4 * 3600), ("24h", 86400), ("7d", 7 * 86400)]:
        n = bars_for(sec, timeframe_sec)
        if n <= max(10, len(close) // 2):
            feat[f"log_slope_{name}"] = _rolling_slope_pct(close, n).to_numpy()
        else:
            feat[f"log_slope_{name}"] = np.nan

    # --- Volatility / range / trend state.
    for name, sec in [("1h", 3600), ("4h", 4 * 3600), ("24h", 86400), ("7d", 7 * 86400)]:
        feat[f"realized_vol_{name}"] = realized_vol(close, bars_for(sec, timeframe_sec)).to_numpy()

    feat["atr_percent_14"] = atr_pct(df, 14).to_numpy()
    feat["atr_percent_60"] = atr_pct(df, 60).to_numpy()
    feat["atr_percent_288"] = atr_pct(df, min(len(df), max(14, bars_for(24 * 3600, timeframe_sec)))).to_numpy()

    for name, sec in [("15m", 900), ("1h", 3600), ("4h", 4 * 3600), ("24h", 86400)]:
        n = bars_for(sec, timeframe_sec)
        feat[f"range_percent_{name}"] = (
            high.rolling(n, min_periods=max(2, n // 4)).max()
            / low.rolling(n, min_periods=max(2, n // 4)).min()
            - 1.0
        ).to_numpy()

    rng = (high - low).replace(0, np.nan)
    body_hi = pd.concat([open_, close], axis=1).max(axis=1)
    body_lo = pd.concat([open_, close], axis=1).min(axis=1)
    body_abs = (close - open_).abs()
    feat["wick_up_ratio"] = ((high - body_hi) / rng).fillna(0.0).to_numpy()
    feat["wick_down_ratio"] = ((body_lo - low) / rng).fillna(0.0).to_numpy()
    feat["body_to_range_ratio"] = (body_abs / rng).fillna(0.0).to_numpy()
    feat["close_location_in_bar"] = ((close - low) / rng).fillna(0.5).to_numpy()

    # RSI / stochastic / EMA / MACD / Bollinger.
    for n in [14, 60, 288]:
        if n < len(df):
            feat[f"rsi_{n}"] = _rsi(close, n).to_numpy()
            feat[f"stoch_{n}"] = _stoch(close, high, low, n).to_numpy()
        else:
            feat[f"rsi_{n}"] = np.nan
            feat[f"stoch_{n}"] = np.nan

    ema_spans = [12, 26, 50, 100, 200, 288]
    ema_values: Dict[int, pd.Series] = {}
    for span in ema_spans:
        if span < len(df):
            ema_values[span] = _ema(close, span)
            feat[f"ema_dist_{span}"] = (close / ema_values[span] - 1.0).to_numpy()
        else:
            ema_values[span] = pd.Series(np.nan, index=df.index)
            feat[f"ema_dist_{span}"] = np.nan
    feat["macd_12_26"] = ((ema_values[12] - ema_values[26]) / close).to_numpy()
    feat["macd_signal_12_26_9"] = _ema(ema_values[12] - ema_values[26], 9).div(close).to_numpy()
    feat["macd_hist_12_26_9"] = (feat["macd_12_26"] - feat["macd_signal_12_26_9"]).to_numpy()

    for n in [20, 120, 288]:
        if n < len(df):
            ma = close.rolling(n, min_periods=max(2, n // 4)).mean()
            sd = close.rolling(n, min_periods=max(2, n // 4)).std()
            feat[f"bb_z_{n}"] = ((close - ma) / sd.replace(0, np.nan)).to_numpy()
            feat[f"bb_width_{n}"] = ((4.0 * sd) / ma.replace(0, np.nan)).to_numpy()
        else:
            feat[f"bb_z_{n}"] = np.nan
            feat[f"bb_width_{n}"] = np.nan

    adx, plus_di, minus_di = _adx(df, 14)
    feat["adx_14"] = adx.to_numpy()
    feat["plus_di_14"] = plus_di.to_numpy()
    feat["minus_di_14"] = minus_di.to_numpy()
    feat["di_minus_minus_plus_14"] = (minus_di - plus_di).to_numpy()

    # --- Volume / liquidity decay proxies.
    for name, sec in [("15m", 900), ("1h", 3600), ("4h", 4 * 3600), ("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400)]:
        n = bars_for(sec, timeframe_sec)
        feat[f"qv_{name}"] = qv.rolling(n, min_periods=max(2, n // 4)).sum().to_numpy()
        feat[f"volume_base_{name}"] = volume.rolling(n, min_periods=max(2, n // 4)).sum().to_numpy()
        feat[f"trade_count_{name}"] = trade_count.rolling(n, min_periods=max(2, n // 4)).sum().to_numpy()

    qv_avg_1h = qv.rolling(bars_for(3600, timeframe_sec), min_periods=2).mean()
    qv_avg_24h = qv.rolling(bars_for(86400, timeframe_sec), min_periods=max(2, bars_for(86400, timeframe_sec) // 4)).mean()
    qv_avg_7d = qv.rolling(bars_for(7 * 86400, timeframe_sec), min_periods=max(2, bars_for(7 * 86400, timeframe_sec) // 4)).mean()
    qv_avg_30d = qv.rolling(bars_for(30 * 86400, timeframe_sec), min_periods=max(2, bars_for(30 * 86400, timeframe_sec) // 4)).mean()
    feat["volume_now_vs_1h_avg"] = (qv / qv_avg_1h).to_numpy()
    feat["volume_now_vs_24h_avg"] = (qv / qv_avg_24h).to_numpy()
    feat["volume_now_vs_7d_avg"] = (qv / qv_avg_7d).to_numpy()
    feat["volume_1h_avg_vs_24h_avg"] = (qv_avg_1h / qv_avg_24h).to_numpy()
    feat["volume_24h_avg_vs_7d_avg"] = (qv_avg_24h / qv_avg_7d).to_numpy()
    feat["volume_7d_avg_vs_30d_avg"] = (qv_avg_7d / qv_avg_30d).to_numpy()
    feat["liquidity_decay_24h_vs_7d"] = (1.0 - qv_avg_24h / qv_avg_7d).to_numpy()
    feat["liquidity_decay_7d_vs_30d"] = (1.0 - qv_avg_7d / qv_avg_30d).to_numpy()
    feat["qv_z_24h"] = _zscore(qv, bars_for(86400, timeframe_sec)).to_numpy()

    if trade_count.notna().any():
        tc_avg_24h = trade_count.rolling(bars_for(86400, timeframe_sec), min_periods=max(2, bars_for(86400, timeframe_sec)//4)).mean()
        tc_avg_7d = trade_count.rolling(bars_for(7*86400, timeframe_sec), min_periods=max(2, bars_for(7*86400, timeframe_sec)//4)).mean()
        feat["trade_count_now_vs_24h_avg"] = (trade_count / tc_avg_24h).to_numpy()
        feat["trade_count_decay_24h_vs_7d"] = (1.0 - tc_avg_24h / tc_avg_7d).to_numpy()
        feat["avg_trade_size_quote"] = (qv / trade_count.replace(0, np.nan)).to_numpy()
        feat["high_volume_low_trade_count"] = ((feat["volume_now_vs_24h_avg"] > 3.0) & (feat["trade_count_now_vs_24h_avg"] < 1.2)).astype(float).to_numpy()
    else:
        feat["trade_count_now_vs_24h_avg"] = np.nan
        feat["trade_count_decay_24h_vs_7d"] = np.nan
        feat["avg_trade_size_quote"] = np.nan
        feat["high_volume_low_trade_count"] = np.nan

    # --- Post-pump exhaustion / distance from high / local failure structure.
    for name, sec in [("1d", 86400), ("3d", 3 * 86400), ("7d", 7 * 86400), ("14d", 14 * 86400), ("30d", 30 * 86400)]:
        n = bars_for(sec, timeframe_sec)
        roll_low = low.rolling(n, min_periods=max(5, n // 10)).min()
        roll_high = high.rolling(n, min_periods=max(5, n // 10)).max()
        feat[f"max_return_{name}"] = (roll_high / roll_low - 1.0).to_numpy()
        feat[f"distance_from_{name}_high"] = (close / roll_high - 1.0).to_numpy()
        feat[f"distance_from_{name}_low"] = (close / roll_low - 1.0).to_numpy()
        feat[f"drawdown_from_{name}_high"] = (1.0 - close / roll_high).to_numpy()

    feat["volume_spike_ratio"] = (qv / qv_avg_24h).to_numpy()
    feat["volume_spike_without_price_continuation"] = ((feat["volume_spike_ratio"] > 3.0) & (feat["ret_1h"].fillna(0) < 0.01)).astype(float).to_numpy()
    feat["pump_fail_24h"] = ((feat["ret_24h"] > 0.10) & (feat["ret_1h"] < 0.0) & (feat["wick_up_ratio"] > 0.35)).astype(float).to_numpy()
    feat["pump_fail_7d"] = ((feat["ret_7d"] > 0.25) & (feat["ret_4h"] < 0.0) & (feat["distance_from_7d_high"] < -0.03)).astype(float).to_numpy()
    feat["pump_then_flat_volume_decay"] = ((feat["ret_7d"] > 0.20) & (feat["ret_24h"].abs() < 0.03) & (feat["liquidity_decay_24h_vs_7d"] > 0.25)).astype(float).to_numpy()

    # Volatility / squeeze risk proxies.
    feat["volatility_expansion_1h_vs_24h"] = (feat["realized_vol_1h"] / pd.Series(feat["realized_vol_24h"]).replace(0, np.nan)).to_numpy()
    feat["volatility_expansion_24h_vs_7d"] = (feat["realized_vol_24h"] / pd.Series(feat["realized_vol_7d"]).replace(0, np.nan)).to_numpy()
    feat["many_wicks_no_trend"] = (
        ((pd.Series(feat["wick_up_ratio"]) + pd.Series(feat["wick_down_ratio"]))
         .rolling(bars_for(3600, timeframe_sec), min_periods=2).mean() > 0.9)
        & (pd.Series(feat["ret_1h"]).abs().fillna(0) < 0.01)
    ).astype(float).to_numpy()
    rolling_std_24h = one_bar_ret.rolling(bars_for(86400, timeframe_sec), min_periods=max(10, bars_for(86400, timeframe_sec)//8)).std()
    feat["extreme_bar_count_24h"] = (one_bar_ret.abs() > 5.0 * rolling_std_24h).astype(float).rolling(bars_for(86400, timeframe_sec), min_periods=2).sum().to_numpy()
    feat["gap_like_move_count_24h"] = (one_bar_ret.abs() > 0.05).astype(float).rolling(bars_for(86400, timeframe_sec), min_periods=2).sum().to_numpy()
    feat["squeeze_risk_static"] = (
        0.35 * pd.Series(feat["ret_24h"]).clip(lower=0).fillna(0)
        + 0.25 * pd.Series(feat["volatility_expansion_1h_vs_24h"]).clip(lower=0, upper=5).fillna(0) / 5.0
        + 0.20 * pd.Series(feat["volume_spike_ratio"]).clip(lower=0, upper=10).fillna(0) / 10.0
        + 0.20 * pd.Series(feat["wick_up_ratio"]).clip(lower=0, upper=1).fillna(0)
    ).to_numpy()

    # Static meta-short score proxy. This is NOT final ML; it is a deterministic baseline.
    feat["weakness_score_static"] = (
        -0.35 * pd.Series(feat["ret_1h"]).fillna(0)
        -0.25 * pd.Series(feat["ret_4h"]).fillna(0)
        -0.20 * pd.Series(feat["momentum_4h_minus_24h"]).fillna(0)
        +0.20 * pd.Series(feat["liquidity_decay_24h_vs_7d"]).fillna(0)
    ).to_numpy()
    feat["post_pump_exhaustion_score_static"] = (
        0.40 * pd.Series(feat["pump_fail_7d"]).fillna(0)
        +0.30 * pd.Series(feat["drawdown_from_7d_high"]).clip(lower=0, upper=1).fillna(0)
        +0.20 * pd.Series(feat["volume_spike_without_price_continuation"]).fillna(0)
        +0.10 * pd.Series(feat["pump_then_flat_volume_decay"]).fillna(0)
    ).to_numpy()
    feat["margin_occupation_penalty_static20"] = (
        0.20
        * (1.0 + pd.Series(feat["squeeze_risk_static"]).fillna(0))
        * (1.0 + pd.Series(feat["volatility_expansion_1h_vs_24h"]).clip(lower=0, upper=5).fillna(0) / 5.0)
    ).to_numpy()
    feat["meta_short_score_static"] = (
        pd.Series(feat["weakness_score_static"]).fillna(0)
        + pd.Series(feat["post_pump_exhaustion_score_static"]).fillna(0)
        - pd.Series(feat["squeeze_risk_static"]).fillna(0)
        - pd.Series(feat["margin_occupation_penalty_static20"]).fillna(0)
    ).to_numpy()

    # --- Forward labels. Only future windows.
    labels = pd.DataFrame({"timestamp_s": df["timestamp_s"].to_numpy(dtype=np.int64)})
    label_h = {"1h": 3600, "4h": 4 * 3600, "24h": 86400, "3d": 3 * 86400, "7d": 7 * 86400, "14d": 14 * 86400, "30d": 30 * 86400}
    for name, sec in label_h.items():
        n = bars_for(sec, timeframe_sec)
        labels[f"future_return_{name}"] = (close.shift(-n) / close - 1.0).to_numpy()
        fut_hi = high.shift(-1).iloc[::-1].rolling(n, min_periods=max(2, n // 4)).max().iloc[::-1]
        fut_lo = low.shift(-1).iloc[::-1].rolling(n, min_periods=max(2, n // 4)).min().iloc[::-1]
        labels[f"future_max_up_{name}"] = (fut_hi / close - 1.0).to_numpy()
        labels[f"future_max_down_{name}"] = (fut_lo / close - 1.0).to_numpy()
        labels[f"future_downside_upside_ratio_{name}"] = (labels[f"future_max_down_{name}"].abs() / labels[f"future_max_up_{name}"].clip(lower=1e-6)).to_numpy()
        labels[f"future_short_path_score_{name}"] = (-labels[f"future_return_{name}"] - labels[f"future_max_up_{name}"]).to_numpy()

    # Expensive path timing labels, intentionally cached.
    # Guard against impossible workloads on 1m multi-month datasets.
    # 5m x 5000 bars is OK; 1m x 30d+ can be too expensive and should be done in a separate exact pass.
    max_path_ops = 150_000_000
    b24 = bars_for(86400, timeframe_sec)
    b7 = bars_for(7 * 86400, timeframe_sec)
    if len(close) * b24 <= max_path_ops:
        labels["future_bars_to_down_3pct_24h"] = _future_first_hit_bars(close, 0.03, b24, "down").to_numpy()
        labels["future_bars_to_up_10pct_24h"] = _future_first_hit_bars(close, 0.10, b24, "up").to_numpy()
    else:
        labels["future_bars_to_down_3pct_24h"] = np.nan
        labels["future_bars_to_up_10pct_24h"] = np.nan
    if len(close) * b7 <= max_path_ops:
        labels["future_bars_to_down_5pct_7d"] = _future_first_hit_bars(close, 0.05, b7, "down").to_numpy()
        labels["future_bars_to_up_20pct_7d"] = _future_first_hit_bars(close, 0.20, b7, "up").to_numpy()
    else:
        labels["future_bars_to_down_5pct_7d"] = np.nan
        labels["future_bars_to_up_20pct_7d"] = np.nan

    labels["future_return_24h_negative"] = (labels["future_return_24h"] < 0).astype(float)
    labels["future_return_7d_negative"] = (labels["future_return_7d"] < 0).astype(float)
    labels["future_max_up_24h_below_10pct"] = (labels["future_max_up_24h"] < 0.10).astype(float)
    labels["future_max_up_7d_below_20pct"] = (labels["future_max_up_7d"] < 0.20).astype(float)
    labels["label_good_meta_short_24h_static20"] = (
        (labels["future_return_24h"] < -0.02)
        & (labels["future_max_up_24h"] < 0.20)
        & (labels["future_short_path_score_24h"] > 0.00)
    ).astype(float)
    labels["label_good_meta_short_7d_static20"] = (
        (labels["future_return_7d"] < -0.05)
        & (labels["future_max_up_7d"] < 0.20)
        & (labels["future_short_path_score_7d"] > 0.02)
    ).astype(float)

    # Quality per symbol/timestamp.
    q = pd.DataFrame({"timestamp_s": df["timestamp_s"].to_numpy(dtype=np.int64)})
    ts = df["timestamp_s"].to_numpy(dtype=np.int64)
    step = int(np.nanmedian(np.diff(ts))) if len(ts) > 2 else timeframe_sec
    prev_gap = np.r_[step, np.diff(ts)] > step * 1.5
    next_gap = np.r_[np.diff(ts), step] > step * 1.5
    q["missing_mask"] = (prev_gap | next_gap).astype(bool)
    q["quality_score"] = (~q["missing_mask"]).astype(float)

    for d in (feat, labels):
        d.replace([np.inf, -np.inf], np.nan, inplace=True)
    return symbol, feat, labels, q


def _worker(args: Tuple[str, pd.DataFrame, int]) -> Tuple[str, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return compute_features_for_symbol(*args)


def compute_all_features(data: Dict[str, pd.DataFrame], timeframe_sec: int, workers: int) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
    features: Dict[str, pd.DataFrame] = {}
    labels: Dict[str, pd.DataFrame] = {}
    quality: Dict[str, pd.DataFrame] = {}
    payloads = [(sym, df, timeframe_sec) for sym, df in data.items()]
    log(f"[features] symbols={len(payloads)} workers={workers}")
    if workers <= 1:
        iterator = map(_worker, payloads)
    else:
        pool = cf.ProcessPoolExecutor(max_workers=workers)
        iterator = pool.map(_worker, payloads)
    try:
        for i, (sym, f, l, q) in enumerate(iterator, 1):
            features[sym] = f
            labels[sym] = l
            quality[sym] = q
            if i % 25 == 0 or i == len(payloads):
                log(f"[features] done {i}/{len(payloads)}")
    finally:
        if workers > 1:
            pool.shutdown()
    return features, labels, quality



def build_cross_sectional(data: Dict[str, pd.DataFrame], features: Dict[str, pd.DataFrame], refs: Sequence[str]) -> Dict[str, pd.DataFrame]:
    """
    Cross-sectional features are the expensive universe-level layer:
      - ranks at each timestamp;
      - z-scores vs universe;
      - relative strength vs universe median / BTC / ETH;
      - leadership decay proxies.
    This is deliberately cached because recomputing it inside every backtest is waste.
    """
    rank_cols = [
        "ret_1h", "ret_4h", "ret_24h", "ret_7d", "ret_30d",
        "qv_1h", "qv_24h", "qv_7d",
        "realized_vol_1h", "realized_vol_24h", "realized_vol_7d",
        "atr_percent_14", "range_percent_1h", "range_percent_24h",
        "volume_spike_ratio", "liquidity_decay_24h_vs_7d", "liquidity_decay_7d_vs_30d",
        "squeeze_risk_static", "weakness_score_static", "post_pump_exhaustion_score_static",
        "meta_short_score_static", "margin_occupation_penalty_static20",
        "distance_from_7d_high", "drawdown_from_7d_high",
    ]
    rel_cols = ["ret_1h", "ret_4h", "ret_24h", "ret_7d", "ret_30d", "qv_24h", "realized_vol_24h", "meta_short_score_static"]

    parts = []
    for sym, f in features.items():
        cols = ["timestamp_s"] + [c for c in sorted(set(rank_cols + rel_cols)) if c in f.columns]
        x = f[cols].copy()
        x["symbol"] = sym
        parts.append(x)
    if not parts:
        return {}

    allf = pd.concat(parts, ignore_index=True)
    out_rows: Dict[str, List[Dict[str, Any]]] = {sym: [] for sym in features}
    refs_norm = list(refs)

    for ts, g in allf.groupby("timestamp_s", sort=True):
        payloads = {sym: {"timestamp_s": int(ts)} for sym in g["symbol"].tolist()}

        # Percentile rank and universe z-score for selected expensive features.
        for col in rank_cols:
            if col not in g.columns:
                continue
            s = pd.to_numeric(g[col], errors="coerce")
            ranks = s.rank(pct=True, method="average")
            mean = s.mean()
            std = s.std()
            z = (s - mean) / std if std and np.isfinite(std) and std > 0 else pd.Series(np.nan, index=s.index)
            for sym, rnk, zz in zip(g["symbol"], ranks, z):
                payloads[sym][f"{col}_rank"] = np.nan if pd.isna(rnk) else float(rnk)
                payloads[sym][f"{col}_z_universe"] = np.nan if pd.isna(zz) else float(zz)

        # Relative to universe median.
        med = {c: safe_float(pd.to_numeric(g[c], errors="coerce").median()) for c in rel_cols if c in g.columns}
        for _, r in g.iterrows():
            sym = r["symbol"]
            for col in rel_cols:
                if col in g.columns:
                    payloads[sym][f"{col}_minus_universe_median"] = safe_float(r.get(col)) - med.get(col, np.nan)

        # BTC/ETH relative strength, if references are present in the same cache.
        ref_ret: Dict[str, Dict[str, float]] = {}
        for ref in refs_norm:
            rg = g[g["symbol"] == ref]
            if not rg.empty:
                ref_ret[ref] = {p: safe_float(rg.iloc[0].get(f"ret_{p}")) for p in ["1h", "4h", "24h", "7d", "30d"]}
        for _, r in g.iterrows():
            sym = r["symbol"]
            for ref, rd in ref_ret.items():
                prefix = "btc" if ref.upper().startswith("BTC") else "eth" if ref.upper().startswith("ETH") else ref.lower().replace("/", "_").replace(":", "_")
                for p in ["1h", "4h", "24h", "7d", "30d"]:
                    payloads[sym][f"relative_strength_vs_{prefix}_{p}"] = safe_float(r.get(f"ret_{p}")) - rd.get(p, np.nan)

        # Leadership/risk bucket flags useful for rule-based allocator.
        for _, r in g.iterrows():
            sym = r["symbol"]
            pl = payloads[sym]
            pl["is_top10_meta_short_score"] = float(pl.get("meta_short_score_static_rank", np.nan) >= 0.90) if np.isfinite(pl.get("meta_short_score_static_rank", np.nan)) else np.nan
            pl["is_bottom30_ret_7d"] = float(pl.get("ret_7d_rank", np.nan) <= 0.30) if np.isfinite(pl.get("ret_7d_rank", np.nan)) else np.nan
            pl["is_top20_squeeze_risk"] = float(pl.get("squeeze_risk_static_rank", np.nan) >= 0.80) if np.isfinite(pl.get("squeeze_risk_static_rank", np.nan)) else np.nan
            pl["is_liquidity_decay_top20"] = float(pl.get("liquidity_decay_24h_vs_7d_rank", np.nan) >= 0.80) if np.isfinite(pl.get("liquidity_decay_24h_vs_7d_rank", np.nan)) else np.nan

        for sym, payload in payloads.items():
            out_rows.setdefault(sym, []).append(payload)

    return {sym: pd.DataFrame(rows) for sym, rows in out_rows.items() if rows}


# -------------------------
# Snapshot fetching
# -------------------------


def fetch_market_snapshots(symbols: Sequence[str], exchange_id: str, out_csv: str, orderbook_limit: int = 100) -> None:
    if ccxt is None:
        raise SystemExit("ccxt missing. pip install ccxt")
    klass = getattr(ccxt, exchange_id)
    ex = klass({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    ex.load_markets()
    rows = []
    now = int(time.time())
    for i, sym in enumerate(symbols, 1):
        row: Dict[str, Any] = {"exchange": exchange_id, "symbol": sym, "timestamp_s": now}
        try:
            ticker = ex.fetch_ticker(sym)
            row["last_price"] = ticker.get("last")
        except Exception as e:
            row["ticker_error"] = repr(e)
        try:
            if hasattr(ex, "fetch_funding_rate"):
                fr = ex.fetch_funding_rate(sym)
                row["funding_rate"] = fr.get("fundingRate")
                row["mark_price"] = fr.get("markPrice")
                row["index_price"] = fr.get("indexPrice")
        except Exception as e:
            row["funding_error"] = repr(e)
        try:
            if hasattr(ex, "fetch_open_interest"):
                oi = ex.fetch_open_interest(sym)
                row["open_interest"] = oi.get("openInterestAmount") or oi.get("openInterest")
                row["open_interest_value"] = oi.get("openInterestValue")
        except Exception as e:
            row["oi_error"] = repr(e)
        try:
            ob = ex.fetch_order_book(sym, limit=orderbook_limit)
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            bid = float(bids[0][0]) if bids else np.nan
            ask = float(asks[0][0]) if asks else np.nan
            mid = (bid + ask) / 2 if np.isfinite(bid) and np.isfinite(ask) else np.nan
            row["best_bid"] = bid
            row["best_ask"] = ask
            row["spread_bps"] = (ask - bid) / mid * 10000 if np.isfinite(mid) and mid > 0 else np.nan
            for pct, name in [(0.001, "0_1pct"), (0.005, "0_5pct"), (0.01, "1_0pct")]:
                if np.isfinite(mid):
                    bd = sum(float(p) * float(q) for p, q in bids if mid * (1 - pct) <= float(p) <= mid)
                    ad = sum(float(p) * float(q) for p, q in asks if mid <= float(p) <= mid * (1 + pct))
                else:
                    bd = ad = np.nan
                row[f"bid_depth_{name}"] = bd
                row[f"ask_depth_{name}"] = ad
                row[f"imbalance_{name}"] = (bd - ad) / (bd + ad) if np.isfinite(bd + ad) and (bd + ad) > 0 else np.nan
        except Exception as e:
            row["orderbook_error"] = repr(e)
        rows.append(row)
        if i % 25 == 0:
            log(f"[snap] {i}/{len(symbols)}")
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    keys = sorted({k for r in rows for k in r.keys()})
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    log(f"[snap] wrote {out_csv}")


# -------------------------
# Export enriched NPZ + reports
# -------------------------


def export_enriched_npz(
    data: Dict[str, pd.DataFrame],
    features: Dict[str, pd.DataFrame],
    labels: Dict[str, pd.DataFrame],
    quality: Dict[str, pd.DataFrame],
    cross: Dict[str, pd.DataFrame],
    out_npz: str,
    timeframe_sec: int,
    warmup_hours: int,
    min_future_hours: int,
) -> dict:
    symbols = sorted(data.keys())
    offsets = [0]
    raw_parts = {"timestamp_s": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "quote_volume": [], "trade_count": []}
    feature_mats = []
    cross_mats = []
    label_mats = []
    trainable_parts = []
    missing_parts = []
    quality_parts = []

    feature_names: List[str] = []
    cross_names: List[str] = []
    label_names: List[str] = []
    for sym in symbols:
        if sym in features and feature_names == []:
            feature_names = [c for c in features[sym].columns if c != "timestamp_s"]
        if sym in cross and cross_names == []:
            cross_names = [c for c in cross[sym].columns if c != "timestamp_s"]
        if sym in labels and label_names == []:
            label_names = [c for c in labels[sym].columns if c != "timestamp_s"]
    for sym in symbols:
        df = data[sym].sort_values("timestamp_s").reset_index(drop=True)
        n = len(df)
        for col in raw_parts:
            raw_parts[col].append(df[col].to_numpy(dtype=np.int64 if col == "timestamp_s" else np.float64))
        f = features.get(sym, pd.DataFrame({"timestamp_s": df["timestamp_s"]}))
        l = labels.get(sym, pd.DataFrame({"timestamp_s": df["timestamp_s"]}))
        q = quality.get(sym, pd.DataFrame({"timestamp_s": df["timestamp_s"], "missing_mask": False, "quality_score": 1.0}))
        c = cross.get(sym, pd.DataFrame({"timestamp_s": df["timestamp_s"]}))
        f2 = df[["timestamp_s"]].merge(f, on="timestamp_s", how="left")
        l2 = df[["timestamp_s"]].merge(l, on="timestamp_s", how="left")
        q2 = df[["timestamp_s"]].merge(q, on="timestamp_s", how="left")
        c2 = df[["timestamp_s"]].merge(c, on="timestamp_s", how="left")
        feature_mats.append(f2.reindex(columns=["timestamp_s"] + feature_names).drop(columns=["timestamp_s"]).to_numpy(dtype=np.float32) if feature_names else np.empty((n, 0), dtype=np.float32))
        cross_mats.append(c2.reindex(columns=["timestamp_s"] + cross_names).drop(columns=["timestamp_s"]).to_numpy(dtype=np.float32) if cross_names else np.empty((n, 0), dtype=np.float32))
        label_mats.append(l2.reindex(columns=["timestamp_s"] + label_names).drop(columns=["timestamp_s"]).to_numpy(dtype=np.float32) if label_names else np.empty((n, 0), dtype=np.float32))
        ts = df["timestamp_s"].to_numpy(dtype=np.int64)
        train = (ts >= ts[0] + warmup_hours * 3600) & (ts <= ts[-1] - min_future_hours * 3600)
        miss = q2.get("missing_mask", pd.Series(False, index=q2.index)).fillna(False).to_numpy(dtype=bool)
        qual = q2.get("quality_score", pd.Series(1.0, index=q2.index)).fillna(1.0).to_numpy(dtype=np.float32)
        trainable_parts.append(train)
        missing_parts.append(miss)
        quality_parts.append(qual)
        offsets.append(offsets[-1] + n)

    payload: Dict[str, Any] = {
        "symbols": np.asarray(symbols, dtype=object),
        "offsets": np.asarray(offsets, dtype=np.int64),
        "feature_names": np.asarray(feature_names, dtype=object),
        "cross_sectional_feature_names": np.asarray(cross_names, dtype=object),
        "label_names": np.asarray(label_names, dtype=object),
        "features": np.vstack(feature_mats) if feature_mats else np.empty((0, 0), dtype=np.float32),
        "cross_sectional_features": np.vstack(cross_mats) if cross_mats else np.empty((0, 0), dtype=np.float32),
        "labels": np.vstack(label_mats) if label_mats else np.empty((0, 0), dtype=np.float32),
        "trainable_mask": np.concatenate(trainable_parts).astype(bool),
        "missing_mask": np.concatenate(missing_parts).astype(bool),
        "quality_score": np.concatenate(quality_parts).astype(np.float32),
    }
    for col, arrs in raw_parts.items():
        payload[col] = np.concatenate(arrs).astype(np.int64 if col == "timestamp_s" else np.float64)
    Path(out_npz).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_npz, **payload)
    meta = {
        "out_npz": out_npz,
        "symbols": len(symbols),
        "rows": int(len(payload["timestamp_s"])),
        "feature_count": len(feature_names),
        "cross_feature_count": len(cross_names),
        "label_count": len(label_names),
        "trainable_rows": int(payload["trainable_mask"].sum()),
        "timeframe_sec": timeframe_sec,
    }
    Path(out_npz).with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[ok] enriched npz {out_npz} {meta}")
    return meta


def write_reports(data: Dict[str, pd.DataFrame], labels: Dict[str, pd.DataFrame], features: Dict[str, pd.DataFrame], out_dir: str, meta: dict) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for sym, df in data.items():
        ts = df["timestamp_s"].to_numpy(dtype=np.int64)
        step = int(np.nanmedian(np.diff(ts))) if len(ts) > 2 else np.nan
        gaps = int((np.diff(ts) > step * 1.5).sum()) if np.isfinite(step) else 0
        rows.append({
            "symbol": sym,
            "rows": len(df),
            "first_ts": int(ts[0]) if len(ts) else None,
            "last_ts": int(ts[-1]) if len(ts) else None,
            "duration_hours": (ts[-1] - ts[0]) / 3600 if len(ts) > 1 else 0,
            "median_step_sec": step,
            "gaps": gaps,
            "zero_volume_pct": float((df["volume"].fillna(0) == 0).mean()) if "volume" in df else np.nan,
            "qv_median": float(df["quote_volume"].median()) if "quote_volume" in df else np.nan,
        })
    pd.DataFrame(rows).sort_values("rows", ascending=False).to_csv(out / "symbol_coverage.csv", index=False)

    # Label summary.
    label_rows = []
    for sym, l in labels.items():
        for col in [c for c in l.columns if c != "timestamp_s"]:
            s = pd.to_numeric(l[col], errors="coerce")
            label_rows.append({"symbol": sym, "label": col, "count": int(s.notna().sum()), "mean": safe_float(s.mean()), "median": safe_float(s.median()), "p10": safe_float(s.quantile(0.10)), "p90": safe_float(s.quantile(0.90))})
    if label_rows:
        pd.DataFrame(label_rows).to_csv(out / "label_distribution_by_symbol.csv", index=False)

    # Feature NaN report aggregated.
    nan: Dict[str, List[float]] = {}
    for f in features.values():
        for col in [c for c in f.columns if c != "timestamp_s"]:
            nan.setdefault(col, []).append(float(f[col].isna().mean()))
    pd.DataFrame([{"feature": k, "avg_nan_pct": float(np.mean(v)), "max_nan_pct": float(np.max(v))} for k, v in nan.items()]).to_csv(out / "feature_nan_report.csv", index=False)
    (out / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[reports] wrote {out}")


# -------------------------
# Framework fetch wrapper
# -------------------------


def run_framework_fetch(args: argparse.Namespace) -> None:
    script = args.framework_fetch_script
    if not script or not Path(script).exists():
        raise SystemExit(f"framework fetch script not found: {script}")
    cmd = [
        sys.executable, script,
        "-i", args.universe_file,
        "-t", args.timeframe,
        "--exchange", args.exchange,
        "--ccxt-symbol-format", args.ccxt_symbol_format,
        "--db-out", args.db,
        "--npz-out", args.raw_npz,
        "--feature-set", args.feature_set,
    ]
    if args.start:
        cmd += ["--start", args.start]
    if args.end:
        cmd += ["--end", args.end]
    if args.back_bars:
        cmd += ["--back-bars", str(args.back_bars)]
    if args.source:
        cmd += ["--source", args.source]
    if args.cache_pack_trend:
        cmd += ["--cache-pack-trend"]
    if args.debug_fetch:
        cmd += ["--debug"]
    log("[framework] " + " ".join(cmd))
    subprocess.check_call(cmd)


# -------------------------
# Commands
# -------------------------


def enrich_from_npz(args: argparse.Namespace) -> None:
    npz_path = maybe_open_zip_npz(args.npz)
    data = load_raw_npz(npz_path, min_bars=args.min_bars)
    log(f"[load] symbols={len(data)} from {args.npz}")
    features, labels, quality = compute_all_features(data, args.timeframe_sec, args.indicator_workers)
    cross: Dict[str, pd.DataFrame] = {}
    if args.build_cross_sectional and len(data) >= 2:
        cross = build_cross_sectional(data, features, parse_ref_symbols(args.reference_symbols))
        log(f"[cross] symbols={len(cross)}")
    meta = export_enriched_npz(data, features, labels, quality, cross, args.out_npz, args.timeframe_sec, args.warmup_hours, args.min_future_hours)
    write_reports(data, labels, features, args.reports_dir, meta)
    if args.fetch_market_snapshots:
        fetch_market_snapshots(list(data.keys()), args.exchange, str(Path(args.reports_dir) / "market_snapshots_current.csv"), args.orderbook_limit)


def enrich_from_db(args: argparse.Namespace) -> None:
    db_path = maybe_open_zip_db(args.db, args.extract_dir)
    raw_npz = args.raw_npz or str(Path(db_path).with_suffix(".raw.npz"))
    db_to_raw_npz(db_path, raw_npz, table=args.db_table, symbols_file=args.symbols_file, include_optional=args.include_optional, chunksize=args.chunksize, skip_integrity_check=args.skip_integrity_check)
    args.npz = raw_npz
    enrich_from_npz(args)


def fetch_build_enrich(args: argparse.Namespace) -> None:
    run_framework_fetch(args)
    args.npz = args.raw_npz
    enrich_from_npz(args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Akela research cache builder v1")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out-npz", required=True)
    common.add_argument("--reports-dir", required=True)
    common.add_argument("--timeframe-sec", type=int, default=60)
    common.add_argument("--indicator-workers", type=int, default=max(1, os.cpu_count() or 1))
    common.add_argument("--min-bars", type=int, default=100)
    common.add_argument("--warmup-hours", type=int, default=24)
    common.add_argument("--min-future-hours", type=int, default=24)
    common.add_argument("--build-cross-sectional", action="store_true")
    common.add_argument("--reference-symbols", default="BTC/USDT:USDT,ETH/USDT:USDT")
    common.add_argument("--fetch-market-snapshots", action="store_true")
    common.add_argument("--exchange", default="bingx")
    common.add_argument("--orderbook-limit", type=int, default=100)

    e_npz = sub.add_parser("enrich-npz", parents=[common])
    e_npz.add_argument("--npz", required=True)
    e_npz.set_defaults(func=enrich_from_npz)

    e_db = sub.add_parser("enrich-db", parents=[common])
    e_db.add_argument("--db", required=True, help="SQLite .db or .zip containing one .db")
    e_db.add_argument("--raw-npz", default="")
    e_db.add_argument("--db-table", default="")
    e_db.add_argument("--symbols-file", default="")
    e_db.add_argument("--include-optional", action="store_true")
    e_db.add_argument("--chunksize", type=int, default=250_000)
    e_db.add_argument("--extract-dir", default="DB/_extracted")
    e_db.add_argument("--skip-integrity-check", action="store_true")
    e_db.set_defaults(func=enrich_from_db)

    fb = sub.add_parser("fetch-build-enrich", parents=[common])
    fb.add_argument("--framework-fetch-script", required=True)
    fb.add_argument("--universe-file", required=True)
    fb.add_argument("--timeframe", default="1m")
    fb.add_argument("--ccxt-symbol-format", default="usdtm")
    fb.add_argument("--start", default="")
    fb.add_argument("--end", default="")
    fb.add_argument("--back-bars", type=int, default=0)
    fb.add_argument("--source", default="")
    fb.add_argument("--db", required=True)
    fb.add_argument("--raw-npz", required=True)
    fb.add_argument("--feature-set", default="none", choices=["none", "full"])
    fb.add_argument("--cache-pack-trend", action="store_true")
    fb.add_argument("--debug-fetch", action="store_true")
    fb.set_defaults(func=fetch_build_enrich)
    return p


def main() -> None:
    p = build_parser()
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
