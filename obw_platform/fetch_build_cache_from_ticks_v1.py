#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_build_cache_from_ticks_v1.py

Build the standard SQLite cache DB directly from exchange trade/tick API data.

Standard DB schema matches the existing price_indicators format:
  symbol, datetime_utc, open, high, low, close, volume,
  rsi, stochastic, mfi, overbought_index,
  atr_ratio, gain_24h_before, dp6h, dp12h,
  quote_volume, qv_24h, vol_surge_mult

Examples:

  python3 fetch_build_cache_from_ticks_v1.py \
    -i obw_platform/universe/universe_ENA_1m.txt \
    -t 1s \
    --back-bars 604800 \
    -o DB/combined_cache_1s_7d.db \
    --exchange bybit \
    --ccxt-symbol-format usdtm

  python3 fetch_build_cache_from_ticks_v1.py \
    -i obw_platform/universe/universe_ENA_1m.txt \
    -t 30s \
    --start "2026-03-07 00:00" \
    --end "2026-03-15 00:00" \
    -o DB/combined_cache_30s_ena.db \
    --exchange bybit \
    --ccxt-symbol-format usdtm

  python3 fetch_build_cache_from_ticks_v1.py \
    -i obw_platform/universe/universe_ENA_1m.txt \
    -t 5m \
    --start "2026-03-07 00:00" \
    --end "2026-03-15 00:00" \
    -o DB/combined_cache_5m_ena.db \
    --exchange okx
"""

import os
import sys
import time
import argparse
import sqlite3
from typing import Dict, List, Optional, Tuple, Any

import pandas as pd
import numpy as np


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


TF_ALIASES = {
    "1sec": "1s", "1 second": "1s", "1 seconds": "1s",
    "5sec": "5s", "5 seconds": "5s",
    "10sec": "10s", "10 seconds": "10s",
    "15sec": "15s", "15 seconds": "15s",
    "30sec": "30s", "30 seconds": "30s",
    "1min": "1m", "1 minute": "1m", "1 minutes": "1m",
    "3min": "3m", "3 minutes": "3m",
    "5min": "5m", "5 minutes": "5m",
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
    return tf.strip().lower()


def timeframe_to_seconds(tf: str) -> int:
    tf = normalize_timeframe(tf)
    units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    val = int(tf[:-1])
    unit = tf[-1]
    if unit not in units:
        raise ValueError(f"Unsupported timeframe: {tf}")
    return val * units[unit]


def timeframe_to_milliseconds(tf: str) -> int:
    return timeframe_to_seconds(tf) * 1000


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
    cur.execute("SELECT MAX(datetime_utc) FROM price_indicators WHERE symbol = ?", (symbol,))
    row = cur.fetchone()
    con.close()
    if row and row[0]:
        dt = pd.to_datetime(row[0], utc=True)
        return int(dt.value // 10**6)
    return None


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


def _parse_dt_to_ms_utc(s: str) -> int:
    ts = pd.to_datetime(s, utc=True)
    return int(ts.value // 10**6)


def _trade_price(t: Dict[str, Any]) -> Optional[float]:
    if t.get("price") is not None:
        return float(t["price"])
    info = t.get("info", {})
    if isinstance(info, dict):
        for k in ("price", "px", "p"):
            if info.get(k) is not None:
                return float(info[k])
    return None


def _trade_amount(t: Dict[str, Any]) -> float:
    if t.get("amount") is not None:
        return float(t["amount"])
    info = t.get("info", {})
    if isinstance(info, dict):
        for k in ("size", "qty", "sz", "q", "amount", "v"):
            if info.get(k) is not None:
                try:
                    return float(info[k])
                except Exception:
                    pass
    return 0.0


def fetch_trades_range(
    ex,
    market: str,
    start_ms: int,
    end_ms: int,
    limit_per_call: int = 1000,
    max_empty_tries: int = 3,
    sleep_sec: float = 0.15,
) -> pd.DataFrame:
    all_rows: List[Tuple[int, float, float, str]] = []
    cursor = start_ms
    empty_tries = 0
    last_seen_key = None

    while cursor < end_ms:
        trades = ex.fetch_trades(market, since=cursor, limit=limit_per_call)
        if not trades:
            empty_tries += 1
            if empty_tries >= max_empty_tries:
                break
            time.sleep(sleep_sec)
            cursor += 1000
            continue

        empty_tries = 0
        batch_rows: List[Tuple[int, float, float, str]] = []
        for t in trades:
            ts = t.get("timestamp")
            px = _trade_price(t)
            amt = _trade_amount(t)
            if ts is None or px is None:
                continue
            if ts < start_ms or ts >= end_ms:
                continue
            trade_id = str(t.get("id") or f"{ts}-{px}-{amt}")
            batch_rows.append((int(ts), float(px), float(amt), trade_id))

        if not batch_rows:
            cursor += 1000
            continue

        batch_rows = sorted(set(batch_rows), key=lambda x: (x[0], x[3]))
        all_rows.extend(batch_rows)

        last_ts = batch_rows[-1][0]
        last_key = (batch_rows[-1][0], batch_rows[-1][3])

        if last_seen_key == last_key:
            cursor = last_ts + 1
        else:
            cursor = last_ts + 1
            last_seen_key = last_key

        time.sleep(sleep_sec)

    if not all_rows:
        return pd.DataFrame(columns=["timestamp", "price", "amount"])

    df = pd.DataFrame(all_rows, columns=["timestamp", "price", "amount", "trade_id"])
    df = df.sort_values(["timestamp", "trade_id"]).drop_duplicates(subset=["timestamp", "trade_id"], keep="last")
    return df[["timestamp", "price", "amount"]].reset_index(drop=True)


def aggregate_trades_to_bars(trades_df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if trades_df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    tf_ms = timeframe_to_milliseconds(timeframe)
    work = trades_df.copy()
    work["bucket"] = (work["timestamp"] // tf_ms) * tf_ms

    grouped = work.groupby("bucket", sort=True)
    bars = pd.DataFrame({
        "open": grouped["price"].first(),
        "high": grouped["price"].max(),
        "low": grouped["price"].min(),
        "close": grouped["price"].last(),
        "volume": grouped["amount"].sum(),
    })

    bars.index = pd.to_datetime(bars.index, unit="ms", utc=True).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return bars.astype(float)


def main() -> None:
    env_input = os.getenv("INPUT_CSV")
    env_timeframe = os.getenv("TIMEFRAME", "1m")
    env_output = os.getenv("OUTPUT", "combined_cache_from_ticks.db")
    env_exchange = os.getenv("EXCHANGE", "bybit")
    env_fresh = os.getenv("FRESH", "false").lower() in {"1", "true", "yes", "y"}
    env_ccxt_fmt = os.getenv("CCXT_SYMBOL_FORMAT", "auto")

    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input-csv", default=env_input, required=(env_input is None), help="CSV/TXT with symbols")
    ap.add_argument("-t", "--timeframe", default=env_timeframe, help="Target timeframe built from trades, e.g. 1s, 30s, 1m, 5m, 1h")
    ap.add_argument("--start", dest="start_utc", default=None, help="UTC start datetime")
    ap.add_argument("--end", dest="end_utc", default=None, help="UTC end datetime (default: now)")
    ap.add_argument("--back-bars", dest="back_bars", type=int, default=None, help="Fetch this many OUTPUT bars back from now")
    ap.add_argument("-o", "--output", default=env_output, help="SQLite DB path")
    ap.add_argument("--exchange", default=env_exchange)
    ap.add_argument("--fresh", action="store_true", default=env_fresh)
    ap.add_argument("--ccxt-symbol-format", dest="ccxt_symbol_format",
                    choices=["auto", "usdtm", "usdt", "spot_only", "perp_only"], default=env_ccxt_fmt)
    ap.add_argument("--trade-limit-per-call", type=int, default=1000, help="Limit for ccxt.fetch_trades()")
    ap.add_argument("--sleep-sec", type=float, default=0.15, help="Sleep between API calls")
    ap.add_argument("--max-empty-tries", type=int, default=3, help="How many empty trade pages to tolerate")
    args = ap.parse_args()

    if ccxt is None:
        print("ERROR: ccxt not installed. pip install ccxt", file=sys.stderr)
        sys.exit(2)

    args.timeframe = normalize_timeframe(args.timeframe)
    tf_seconds = timeframe_to_seconds(args.timeframe)
    tf_ms = tf_seconds * 1000

    ensure_schema(args.output)
    if args.fresh:
        con = sqlite3.connect(args.output)
        cur = con.cursor()
        cur.execute("DROP TABLE IF EXISTS price_indicators")
        con.commit()
        con.close()
        ensure_schema(args.output)

    ex_klass = getattr(ccxt, args.exchange)
    ex_kwargs: Dict[str, Any] = {}
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

    if args.back_bars is not None and args.back_bars > 0:
        now_ms = ex.milliseconds() if hasattr(ex, "milliseconds") else int(pd.Timestamp.utcnow().value // 10**6)
        end_ms = now_ms
        start_ms = max(0, now_ms - args.back_bars * tf_ms)
    elif args.start_utc is not None:
        start_ms = _parse_dt_to_ms_utc(args.start_utc)
        if args.end_utc:
            end_ms = _parse_dt_to_ms_utc(args.end_utc)
        else:
            end_ms = ex.milliseconds() if hasattr(ex, "milliseconds") else int(pd.Timestamp.utcnow().value // 10**6)
    else:
        raise SystemExit("Provide either --back-bars or --start/--end")

    symbols = load_universe_symbols(args.input_csv)
    bases = [normalize_token(x) for x in symbols]

    total_rows = 0
    for raw in bases:
        mkt = resolve_market(ex, raw, fmt_bias=args.ccxt_symbol_format)
        if not mkt:
            print(f"[SKIP] {raw} — no matching market on {args.exchange}")
            continue

        try:
            fetch_start_ms = start_ms
            if args.back_bars is not None and args.back_bars > 0:
                last_ms = last_timestamp_ms(args.output, mkt)
                if last_ms is not None:
                    fetch_start_ms = max(start_ms, last_ms + tf_ms)

            trades_df = fetch_trades_range(
                ex=ex,
                market=mkt,
                start_ms=fetch_start_ms,
                end_ms=end_ms,
                limit_per_call=args.trade_limit_per_call,
                max_empty_tries=args.max_empty_tries,
                sleep_sec=args.sleep_sec,
            )

            if trades_df.empty:
                print(f"[WARN] {mkt} — no trades returned in range")
                continue

            bars_df = aggregate_trades_to_bars(trades_df, args.timeframe)
            if bars_df.empty:
                print(f"[WARN] {mkt} — trades fetched but no bars built")
                continue

            feats = compute_features(bars_df, tf_seconds=tf_seconds)
            rows: List[dict] = []
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

            insert_ignore_rows(args.output, rows)
            total_rows += len(rows)
            print(f"[OK] {raw} -> {mkt} tf={args.timeframe} trades={len(trades_df)} bars={len(rows)}")
        except Exception as e:
            print(f"[ERR] {raw} -> {mkt} {e}", file=sys.stderr)

    print(f"[DONE] total_rows={total_rows}")


if __name__ == "__main__":
    main()
