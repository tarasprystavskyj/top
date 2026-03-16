#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_build_cache_v17.py

v17 changes vs v16:
- Adds JSON output via --json-output
- Optional JSON-only mode via --json-only
- Keeps the same source data, exchange logic, timeframe handling, and fetch parameters
- JSON records use format:
    {"price": 0.1034, "timestamp": 1773073750000, "volume": 591.79}
- Can optionally include symbol in JSON via --json-include-symbol
- Supports both array JSON (.json) and JSONL (.jsonl / .ndjson)
- For backfill append mode, JSON writes only freshly fetched rows from this run
- SQLite behavior remains backward-compatible unless --json-only is used

Examples:
  # Same as old behavior, plus JSON file
  python3 fetch_build_cache_v17.py \
    -i universe_symbols_bingx.csv -t 1m --back-bars 5000 \
    -o combined_cache_1m.db --exchange bingx --ccxt-symbol-format usdtm \
    --json-output combined_cache_1m.jsonl

  # JSON only, no SQLite
  python3 fetch_build_cache_v17.py \
    -i universe_symbols_bingx.csv -t 1m --back-bars 5000 \
    --exchange bingx --ccxt-symbol-format usdtm \
    --json-output combined_cache_1m.jsonl --json-only
"""
import os
import sys
import json
import argparse
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
import numpy as np


# --- Optional .env loader ---
def try_load_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
        print("[.env] loaded", file=sys.stderr)
    except Exception:
        pass


try_load_dotenv()

try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None


# -------- Symbol helpers --------

def normalize_token(s: str) -> str:
    return str(s).strip().upper()


def _clean_symbol_entry(raw: str) -> Optional[str]:
    s = str(raw).strip()
    if not s:
        return None
    lowered = s.lower()
    if lowered in {"symbol", "symbols"}:
        return None
    if s.startswith("#"):
        return None
    return s


def load_universe_symbols(path: str) -> List[str]:
    """Load symbols from either CSV (with a 'symbol' column) or newline TXT."""
    if not os.path.exists(path):
        raise SystemExit(f"Universe file not found: {path}")

    symbols: List[str] = []

    try:
        df = pd.read_csv(path)
    except Exception:
        df = None

    if df is not None:
        if not df.columns.empty:
            lowered_cols = {str(c).strip().lower(): str(c) for c in df.columns}
            if "symbol" in lowered_cols:
                col_name = lowered_cols["symbol"]
                for val in df[col_name].tolist():
                    cleaned = _clean_symbol_entry(val)
                    if cleaned:
                        symbols.append(cleaned)
            elif df.shape[1] == 1:
                col_name = str(df.columns[0]).strip()
                header_candidate = _clean_symbol_entry(col_name)
                if header_candidate:
                    symbols.append(header_candidate)
                for val in df.iloc[:, 0].tolist():
                    cleaned = _clean_symbol_entry(val)
                    if cleaned:
                        symbols.append(cleaned)

    if not symbols:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                cleaned = _clean_symbol_entry(line)
                if cleaned:
                    symbols.append(cleaned)

    deduped: List[str] = []
    seen = set()
    for sym in symbols:
        key = sym.upper()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(sym)

    if not deduped:
        raise SystemExit(f"No symbols found in universe file: {path}")

    return deduped


def parse_base_quote(raw: str) -> Tuple[str, Optional[str]]:
    """
    Accepts: BTC, BTCUSDT, BTC-USDT, BTC/USDT, BTC/USDT:USDT
    Returns (base, quote or None)
    """
    s = normalize_token(raw)
    if "/" in s:
        base, rest = s.split("/", 1)
        return base, rest.split(":")[0]
    if "-" in s:
        parts = s.split("-")
        if len(parts) >= 2:
            return parts[0], parts[1]
    if s.endswith("USDT") and len(s) > 4:
        return s[:-4], "USDT"
    if s.endswith("USDC") and len(s) > 4:
        return s[:-4], "USDC"
    return s, None


def resolve_market(ex, raw: str, fmt_bias: str = "auto") -> Optional[str]:
    """
    Try to find a valid CCXT market id on the exchange.
    """
    s = normalize_token(raw)
    markets = ex.markets if getattr(ex, "markets", None) else ex.load_markets()
    if s in markets:
        return s

    base, guess = parse_base_quote(s)

    ladders = {
        "auto":        [f"{base}/USDT:USDT", f"{base}/USDT", f"{base}/USDC:USDC", f"{base}/USDC"],
        "usdtm":       [f"{base}/USDT:USDT", f"{base}/USDC:USDC", f"{base}/USDT", f"{base}/USDC"],
        "usdt":        [f"{base}/USDT", f"{base}/USDT:USDT", f"{base}/USDC", f"{base}/USDC:USDC"],
        "spot_only":   [f"{base}/USDT", f"{base}/USDC"],
        "perp_only":   [f"{base}/USDT:USDT", f"{base}/USDC:USDC"],
    }

    cand = ladders.get(fmt_bias, ladders["auto"])

    if guess in {"USDT", "USDC"}:
        prioritized = []
        prioritized += [c for c in cand if c.endswith(guess) or f"/{guess}" in c]
        prioritized += [c for c in cand if not (c.endswith(guess) or f"/{guess}" in c)]
        cand = prioritized

    for c in cand:
        if c in markets:
            return c
    return None


# -------- Timeframe helpers --------

TF_ALIASES = {
    "1min": "1m", "1 minute": "1m", "1 minutes": "1m",
    "3min": "3m", "3 minutes": "3m",
    "5min": "5m", "5 minutes": "5m", "5 mins": "5m",
    "15min": "15m", "15 minutes": "15m",
    "30min": "30m", "30 minutes": "30m",
    "45min": "45m", "45 minutes": "45m",
    "60min": "1h", "60 minutes": "1h",
    "1hour": "1h", "1 hr": "1h",
    "2hour": "2h", "2 hr": "2h",
    "4hour": "4h", "4 hr": "4h",
    "6hour": "6h", "6 hr": "6h",
    "12hour": "12h", "12 hr": "12h",
    "24hour": "1d", "24 hr": "1d",
}


def normalize_timeframe(tf: str) -> str:
    s = tf.strip().lower().replace("_", "").replace("-", " ").replace("/", " ").replace(".", "")
    if s in TF_ALIASES:
        return TF_ALIASES[s]
    return tf


def timeframe_to_seconds(tf: str) -> int:
    tf = normalize_timeframe(tf).strip().lower()
    units = {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800, 'M': 2592000}
    try:
        val = int(tf[:-1])
        unit = tf[-1]
        return val * units.get(unit, 3600)
    except Exception:
        return 3600


def timeframe_to_milliseconds(tf: str) -> int:
    return timeframe_to_seconds(tf) * 1000


# -------- DB helpers --------

def ensure_schema(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS price_indicators(
            symbol TEXT,
            datetime_utc TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            rsi REAL, stochastic REAL, mfi REAL, overbought_index REAL,
            atr_ratio REAL,
            gain_24h_before REAL,
            dp6h REAL, dp12h REAL,
            quote_volume REAL, qv_24h REAL, vol_surge_mult REAL,
            PRIMARY KEY (symbol, datetime_utc)
        )"""
    )
    cur.execute("PRAGMA journal_mode=WAL;")
    con.commit()
    con.close()


def insert_ignore_rows(db_path: str, rows: List[dict]) -> None:
    if not rows:
        return
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cols = [
        "symbol", "datetime_utc", "open", "high", "low", "close", "volume",
        "rsi", "stochastic", "mfi", "overbought_index", "atr_ratio", "gain_24h_before",
        "dp6h", "dp12h", "quote_volume", "qv_24h", "vol_surge_mult"
    ]
    placeholders = ",".join(["?"] * len(cols))
    data = [tuple(r.get(c) for c in cols) for r in rows]
    cur.executemany(
        f"INSERT OR IGNORE INTO price_indicators ({','.join(cols)}) VALUES ({placeholders})",
        data
    )
    con.commit()
    con.close()


def last_timestamp_ms(db_path: str, symbol: str) -> Optional[int]:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        "SELECT MAX(datetime_utc) FROM price_indicators WHERE symbol = ?",
        (symbol,)
    )
    row = cur.fetchone()
    con.close()
    if row and row[0]:
        dt = pd.to_datetime(row[0], utc=True)
        return int(dt.value // 10**6)
    return None


# -------- JSON helpers --------

def json_mode_from_path(path: str) -> str:
    p = str(path).lower()
    if p.endswith(".jsonl") or p.endswith(".ndjson"):
        return "jsonl"
    return "json"


def reset_json_output(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    mode = json_mode_from_path(path)
    if mode == "json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write("")


def _row_to_json_obj(row: dict, include_symbol: bool = False) -> dict:
    obj = {
        "price": float(row["close"]),
        "timestamp": int(pd.to_datetime(row["datetime_utc"], utc=True).value // 10**6),
        "volume": float(row["volume"]),
    }
    if include_symbol:
        obj["symbol"] = row["symbol"]
    return obj


def append_json_rows(path: str, rows: List[dict], include_symbol: bool = False) -> int:
    """
    Appends rows to either:
    - JSON array file (.json)
    - JSONL file (.jsonl / .ndjson)
    Returns number of written rows.
    """
    if not path or not rows:
        return 0

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    mode = json_mode_from_path(path)

    if mode == "jsonl":
        with open(path, "a", encoding="utf-8") as f:
            for row in rows:
                json.dump(_row_to_json_obj(row, include_symbol=include_symbol), f, ensure_ascii=False)
                f.write("\n")
        return len(rows)

    # JSON array mode (.json)
    existing = []
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
                if not isinstance(existing, list):
                    existing = []
        except Exception:
            existing = []

    existing.extend(_row_to_json_obj(row, include_symbol=include_symbol) for row in rows)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False)

    return len(rows)


# -------- Feature engineering --------

def calc_atr_ratio(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            (df["high"] - df["low"]).abs(),
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return (atr / df["close"]).replace([np.inf, -np.inf], np.nan).fillna(0.0)


def compute_features(df: pd.DataFrame, tf_seconds: int = 3600) -> pd.DataFrame:
    out = df.copy()
    bars_24h = max(1, int(round(24 * 3600 / max(1, tf_seconds))))
    bars_12h = max(1, int(round(12 * 3600 / max(1, tf_seconds))))
    bars_6h = max(1, int(round(6 * 3600 / max(1, tf_seconds))))
    out["gain_24h_before"] = (out["close"] / out["close"].shift(bars_24h) - 1.0).fillna(0.0)
    out["dp6h"] = (out["close"] / out["close"].shift(bars_6h) - 1.0).fillna(0.0)
    out["dp12h"] = (out["close"] / out["close"].shift(bars_12h) - 1.0).fillna(0.0)
    out["atr_ratio"] = calc_atr_ratio(out, 14)
    out["quote_volume"] = (out["volume"] * out["close"]).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    out["qv_24h"] = out["quote_volume"].rolling(bars_24h, min_periods=1).sum()
    avg_per_bar = out["qv_24h"] / float(bars_24h)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["vol_surge_mult"] = np.where(avg_per_bar > 0, out["quote_volume"] / avg_per_bar, 0.0)
    out["rsi"] = 0.0
    out["stochastic"] = 0.0
    out["mfi"] = 0.0
    out["overbought_index"] = 0.0
    return out


# -------- CCXT OHLCV helpers --------

MAX_PER_REQUEST = 1440


def _parse_dt_to_ms_utc(s: str) -> int:
    ts = pd.to_datetime(s, utc=True)
    return int(ts.value // 10**6)


def _df_from_ohlcv(ohlcv: list) -> pd.DataFrame:
    if not ohlcv:
        return pd.DataFrame()
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["datetime_utc"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    df = df.set_index("datetime_utc")[["open", "high", "low", "close", "volume"]].astype(float)
    return df


def fetch_ohlcv_limit(ex, market: str, timeframe: str, limit: int) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(market, timeframe=timeframe, limit=limit)
    return _df_from_ohlcv(ohlcv)


def fetch_ohlcv_range(ex, market: str, timeframe: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    tf_ms = timeframe_to_milliseconds(timeframe)
    if end_ms <= start_ms:
        return pd.DataFrame()

    cursor = start_ms
    frames = []
    safety_iter = 0

    while cursor < end_ms:
        chunk_bars = MAX_PER_REQUEST
        remaining_ms = max(0, end_ms - cursor)
        est_rem_bars = int(np.ceil(remaining_ms / tf_ms))
        if est_rem_bars > 0:
            chunk_bars = min(MAX_PER_REQUEST, est_rem_bars)

        ohlcv = ex.fetch_ohlcv(market, timeframe=timeframe, since=cursor, limit=chunk_bars)
        if not ohlcv:
            break

        df = _df_from_ohlcv(ohlcv)
        if df.empty:
            break
        frames.append(df)

        last_ts = int(pd.to_datetime(df.index[-1]).value // 10**6)
        next_cursor = last_ts + tf_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor

        safety_iter += 1
        if safety_iter > 100000:
            break

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    idx_ms = pd.to_datetime(out.index).view("int64") // 10**6
    mask = (idx_ms >= start_ms) & (idx_ms < end_ms)
    return out.loc[mask]


# -------- Main --------

def main():
    env_input = os.getenv("INPUT_CSV")
    env_timeframe = os.getenv("TIMEFRAME", "1h")
    env_limit = int(os.getenv("LIMIT", "500"))
    env_output = os.getenv("OUTPUT", "combined_cache.db")
    env_exchange = os.getenv("EXCHANGE", "bingx")
    env_fresh = os.getenv("FRESH", "false").lower() in {"1", "true", "yes", "y"}
    env_ccxt_fmt = os.getenv("CCXT_SYMBOL_FORMAT", "auto")
    env_json_output = os.getenv("JSON_OUTPUT", None)
    env_json_only = os.getenv("JSON_ONLY", "false").lower() in {"1", "true", "yes", "y"}

    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input-csv", default=env_input, required=(env_input is None),
                    help="CSV/TXT with symbols")
    ap.add_argument("-t", "--timeframe", default=env_timeframe,
                    help="CCXT timeframe (e.g., 1m, 5m, 1h). Aliases like '5min' supported.")
    ap.add_argument("--limit", type=int, default=env_limit,
                    help="Legacy single-shot fetch (<=1440) if --start/--back-bars not used.")
    ap.add_argument("--start", dest="start_utc", default=None,
                    help="UTC start datetime (e.g., '2025-06-01 00:00' or ISO).")
    ap.add_argument("--end", dest="end_utc", default=None,
                    help="UTC end datetime (default: now).")
    ap.add_argument("--back-bars", dest="back_bars", type=int, default=None,
                    help="Fetch this many bars back from now (overrides --limit).")
    ap.add_argument("-o", "--output", default=env_output,
                    help="SQLite DB path (still used unless --json-only is passed).")
    ap.add_argument("--exchange", default=env_exchange)
    ap.add_argument("--fresh", action="store_true", default=env_fresh)
    ap.add_argument("--ccxt-symbol-format", dest="ccxt_symbol_format",
                    choices=["auto", "usdtm", "usdt", "spot_only", "perp_only"], default=env_ccxt_fmt,
                    help="Bias how symbols are resolved on the exchange")

    # New JSON flags
    ap.add_argument("--json-output", dest="json_output", default=env_json_output,
                    help="Optional JSON output path. Use .json for array JSON or .jsonl/.ndjson for line-delimited JSON.")
    ap.add_argument("--json-only", action="store_true", default=env_json_only,
                    help="Write only JSON and skip SQLite writes.")
    ap.add_argument("--json-include-symbol", action="store_true", default=False,
                    help="Also include symbol field in each JSON object.")
    ap.add_argument("--json-reset", action="store_true", default=False,
                    help="Reset JSON output file before writing.")
    args = ap.parse_args()

    if ccxt is None:
        print("ERROR: ccxt not installed. pip install ccxt", file=sys.stderr)
        sys.exit(2)

    args.timeframe = normalize_timeframe(args.timeframe)

    if args.json_only and not args.json_output:
        raise SystemExit("ERROR: --json-only requires --json-output")

    if not args.json_only:
        ensure_schema(args.output)
        if args.fresh:
            con = sqlite3.connect(args.output)
            cur = con.cursor()
            cur.execute("DROP TABLE IF EXISTS price_indicators")
            con.commit()
            con.close()
            ensure_schema(args.output)

    if args.json_output and (args.json_reset or args.fresh):
        reset_json_output(args.json_output)

    ex_klass = getattr(ccxt, args.exchange)
    ex_kwargs = {}
    api_key = os.getenv("CCXT_API_KEY") or os.getenv("API_KEY")
    api_secret = os.getenv("CCXT_SECRET") or os.getenv("API_SECRET")
    api_password = os.getenv("CCXT_PASSWORD") or os.getenv("API_PASSWORD")
    if api_key and api_secret:
        ex_kwargs["apiKey"] = api_key
        ex_kwargs["secret"] = api_secret
    if api_password:
        ex_kwargs["password"] = api_password

    http_proxy = os.getenv("HTTP_PROXY") or os.getenv("PROXY") or os.getenv("PROXY_HTTP")
    https_proxy = os.getenv("HTTPS_PROXY") or os.getenv("PROXY_HTTPS")
    if http_proxy or https_proxy:
        ex_kwargs["proxies"] = {}
        if http_proxy:
            ex_kwargs["proxies"]["http"] = http_proxy
        if https_proxy:
            ex_kwargs["proxies"]["https"] = https_proxy

    ex = ex_klass(ex_kwargs)
    ex.enableRateLimit = True
    ex.load_markets()

    start_ms = None
    end_ms = None
    if args.back_bars is not None and args.back_bars > 0:
        pass
    elif args.start_utc is not None:
        start_ms = _parse_dt_to_ms_utc(args.start_utc)
        if args.end_utc:
            end_ms = _parse_dt_to_ms_utc(args.end_utc)
        else:
            end_ms = ex.milliseconds() if hasattr(ex, "milliseconds") else int(pd.Timestamp.utcnow().value // 10**6)

    symbols = load_universe_symbols(args.input_csv)
    bases = [normalize_token(x) for x in symbols]

    tf_seconds = timeframe_to_seconds(args.timeframe)
    tf_ms = tf_seconds * 1000

    total_sql_rows = 0
    total_json_rows = 0

    for raw in bases:
        mkt = resolve_market(ex, raw, fmt_bias=args.ccxt_symbol_format)
        if not mkt:
            print(f"[SKIP] {raw} — no matching market on {args.exchange}")
            continue

        try:
            if args.back_bars is not None and args.back_bars > 0:
                now_ms = ex.milliseconds() if hasattr(ex, "milliseconds") else int(pd.Timestamp.utcnow().value // 10**6)
                start_limit_ms = max(0, now_ms - args.back_bars * tf_ms)

                # SQLite append logic remains the same.
                if not args.json_only:
                    last_ms = last_timestamp_ms(args.output, mkt)
                    fetch_start_ms = start_limit_ms if last_ms is None else max(start_limit_ms, last_ms + tf_ms)
                else:
                    # In JSON-only mode we do not inspect existing JSON for de-duplication.
                    # We fetch the requested range for this run.
                    fetch_start_ms = start_limit_ms

                df = fetch_ohlcv_range(ex, mkt, args.timeframe, fetch_start_ms, now_ms)

            elif start_ms is not None and end_ms is not None:
                df = fetch_ohlcv_range(ex, mkt, args.timeframe, start_ms, end_ms)
            else:
                lim = min(MAX_PER_REQUEST, int(args.limit))
                df = fetch_ohlcv_limit(ex, mkt, args.timeframe, lim)

            expected_rows = None
            if args.back_bars is not None and args.back_bars > 0:
                expected_rows = args.back_bars
            elif start_ms is not None and end_ms is not None:
                expected_rows = int(np.ceil((end_ms - start_ms) / tf_ms))

            if df.empty and expected_rows is not None:
                lim = min(expected_rows, MAX_PER_REQUEST)
                df = fetch_ohlcv_limit(ex, mkt, args.timeframe, lim)

            if df.empty:
                print(f"[WARN] {mkt} — no OHLCV")
                continue

            feats = compute_features(df, tf_seconds=tf_seconds)
            rows = []
            for idx, r in feats.iterrows():
                rows.append({
                    "symbol": mkt,
                    "datetime_utc": idx,
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r["volume"]),
                    "rsi": float(r.get("rsi", 0.0)),
                    "stochastic": float(r.get("stochastic", 0.0)),
                    "mfi": float(r.get("mfi", 0.0)),
                    "overbought_index": float(r.get("overbought_index", 0.0)),
                    "atr_ratio": float(r["atr_ratio"]),
                    "gain_24h_before": float(r["gain_24h_before"]),
                    "dp6h": float(r["dp6h"]),
                    "dp12h": float(r["dp12h"]),
                    "quote_volume": float(r["quote_volume"]),
                    "qv_24h": float(r["qv_24h"]),
                    "vol_surge_mult": float(r["vol_surge_mult"]),
                })

            if not args.json_only:
                insert_ignore_rows(args.output, rows)
                total_sql_rows += len(rows)

            if args.json_output:
                written = append_json_rows(
                    args.json_output,
                    rows,
                    include_symbol=args.json_include_symbol
                )
                total_json_rows += written

            msg = "[OK]"
            if expected_rows is not None and len(rows) < expected_rows:
                msg = "[WARN]"

            suffix = []
            if not args.json_only:
                suffix.append(f"sqlite={len(rows)}")
            if args.json_output:
                suffix.append(f"json={len(rows)}")
            suffix_s = " ".join(suffix) if suffix else f"rows={len(rows)}"

            print(f"{msg} {raw} -> {mkt} tf={args.timeframe} {suffix_s}")

        except Exception as e:
            print(f"[ERR] {raw} -> {mkt} {e}", file=sys.stderr)

    summary = []
    if not args.json_only:
        summary.append(f"total_sql_rows={total_sql_rows}")
    if args.json_output:
        summary.append(f"total_json_rows={total_json_rows}")
    print("[DONE]", " ".join(summary) if summary else "done")


if __name__ == "__main__":
    main()
