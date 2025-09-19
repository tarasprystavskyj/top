#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_build_cache_v16.py
- Adds date range (--start/--end) and "back from now" (--back-bars) fetching.
- When using --back-bars, only missing bars after the last stored row are fetched and appended.
- Chunks requests into <=1440 bars per call to satisfy exchange limits.
- Maintains v14 functionality (limit-based fetch) for backwards compatibility.

Outputs SQLite table 'price_indicators' with columns:
  symbol TEXT, datetime_utc TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
  rsi REAL, stochastic REAL, mfi REAL, overbought_index REAL,
  atr_ratio REAL, gain_24h_before REAL, dp6h REAL, dp12h REAL,
  quote_volume REAL, qv_24h REAL, vol_surge_mult REAL

Examples:
  # Back 5000 bars from "now" on 5m timeframe (chunked as needed)
  python3 fetch_build_cache_v16.py \
    -i universe_symbols_bingx.csv -t 5m --back-bars 5000 \
    -o combined_cache_5m.db --exchange bingx --ccxt-symbol-format usdtm

  # Explicit date range (UTC)
  python3 fetch_build_cache_v16.py \
    -i universe_symbols_bingx.csv -t 30m \
    --start "2025-06-01 00:00" --end "2025-08-22 00:00" \
    -o combined_cache_30m.db --exchange bingx

  # Legacy single-shot with limit (<=1440 bars)
  python3 fetch_build_cache_v16.py -i universe_symbols_bingx.csv -t 1h --limit 1440 -o out.db
"""
import os
import sys
import argparse
import sqlite3
from typing import List, Optional, Tuple

import pandas as pd
import numpy as np

# --- Optional .env loader ---
def try_load_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()  # loads .env from CWD if present
        print("[.env] loaded", file=sys.stderr)
    except Exception:
        # dotenv not installed or no .env — continue silently
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
    Order preference depends on fmt_bias:
      - auto      : perp USDT, spot USDT, perp USDC, spot USDC
      - usdtm     : perp USDT first, then perp USDC, then spot
      - usdt      : spot USDT first, then perp USDT
      - spot_only : spot USDT, spot USDC
      - perp_only : perp USDT, perp USDC
    Bias to provided quote if present.
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

    # If user hinted a quote, push those first while respecting perp/spot bias
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
    s = tf.strip().lower().replace("_", "").replace("-", " ").replace("/", " ").replace(".", "").replace("min", "min")
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
    """Insert rows, skipping any duplicates already in the table."""
    if not rows:
        return
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cols = [
        "symbol","datetime_utc","open","high","low","close","volume",
        "rsi","stochastic","mfi","overbought_index","atr_ratio","gain_24h_before",
        "dp6h","dp12h","quote_volume","qv_24h","vol_surge_mult"
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
    """Return the latest timestamp in ms for symbol in DB, or None if absent."""
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
    bars_24h = max(1, int(round(24*3600 / max(1, tf_seconds))))
    bars_12h = max(1, int(round(12*3600 / max(1, tf_seconds))))
    bars_6h  = max(1, int(round( 6*3600 / max(1, tf_seconds))))
    out["gain_24h_before"] = (out["close"] / out["close"].shift(bars_24h) - 1.0).fillna(0.0)
    out["dp6h"]  = (out["close"] / out["close"].shift(bars_6h) - 1.0).fillna(0.0)
    out["dp12h"] = (out["close"] / out["close"].shift(bars_12h) - 1.0).fillna(0.0)
    out["atr_ratio"] = calc_atr_ratio(out, 14)
    out["quote_volume"] = (out["volume"] * out["close"]).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    out["qv_24h"] = out["quote_volume"].rolling(bars_24h, min_periods=1).sum()
    avg_per_bar = out["qv_24h"] / float(bars_24h)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["vol_surge_mult"] = np.where(avg_per_bar > 0, out["quote_volume"] / avg_per_bar, 0.0)
    # placeholders for compatibility
    out["rsi"] = 0.0
    out["stochastic"] = 0.0
    out["mfi"] = 0.0
    out["overbought_index"] = 0.0
    return out


# -------- CCXT OHLCV helpers --------

MAX_PER_REQUEST = 1440

def _parse_dt_to_ms_utc(s: str) -> int:
    """Parse many datetime formats to epoch ms (UTC)."""
    # pandas handles many formats and tz-awareness
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
    """
    Fetch OHLCV in [start_ms, end_ms) chunked by <=1440 bars per request.
    """
    tf_ms = timeframe_to_milliseconds(timeframe)
    if end_ms <= start_ms:
        return pd.DataFrame()

    cursor = start_ms
    frames = []
    safety_iter = 0
    while cursor < end_ms:
        # Request up to 1440 bars but not beyond end_ms
        # Many exchanges ignore "end", so we use 'since' and a capped 'limit'
        chunk_bars = MAX_PER_REQUEST
        # If we can estimate remaining bars, reduce limit to avoid over-fetch
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

        # Advance cursor to after the last bar to avoid duplicates
        last_ts = int(pd.to_datetime(df.index[-1]).value // 10**6)
        next_cursor = last_ts + tf_ms
        if next_cursor <= cursor:
            # avoid infinite loop if exchange repeats data
            break
        cursor = next_cursor

        safety_iter += 1
        if safety_iter > 100000:
            # extreme guard
            break

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    # Trim strictly to end_ms in case we over-fetched last bar
    idx_ms = pd.to_datetime(out.index).view("int64") // 10**6
    mask = (idx_ms >= start_ms) & (idx_ms < end_ms)
    return out.loc[mask]

def fetch_ohlcv_back_from_now(ex, market: str, timeframe: str, back_bars: int) -> pd.DataFrame:
    tf_ms = timeframe_to_milliseconds(timeframe)
    now_ms = ex.milliseconds() if hasattr(ex, "milliseconds") else int(pd.Timestamp.utcnow().value // 10**6)
    start_ms = max(0, now_ms - back_bars * tf_ms)
    return fetch_ohlcv_range(ex, market, timeframe, start_ms, now_ms)


# -------- Main --------

def main():
    # Defaults from environment (populated by .env if present)
    env_input = os.getenv("INPUT_CSV")
    env_timeframe = os.getenv("TIMEFRAME", "1h")
    env_limit = int(os.getenv("LIMIT", "500"))
    env_output = os.getenv("OUTPUT", "combined_cache.db")
    env_exchange = os.getenv("EXCHANGE", "bingx")
    env_fresh = os.getenv("FRESH", "false").lower() in {"1","true","yes","y"}
    env_ccxt_fmt = os.getenv("CCXT_SYMBOL_FORMAT", "auto")

    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input-csv", default=env_input, required=(env_input is None),
                    help="CSV with column 'symbol' (BTC, BTC-USDT, BTCUSDT, BTC/USDT, BTC/USDT:USDT)")
    ap.add_argument("-t", "--timeframe", default=env_timeframe,
                    help="CCXT timeframe (e.g., 5m, 1h). Aliases like '5min' supported.")
    ap.add_argument("--limit", type=int, default=env_limit,
                    help="Legacy single-shot fetch (<=1440) if --start/--back-bars not used.")
    ap.add_argument("--start", dest="start_utc", default=None,
                    help="UTC start datetime (e.g., '2025-06-01 00:00' or ISO).")
    ap.add_argument("--end", dest="end_utc", default=None,
                    help="UTC end datetime (default: now).")
    ap.add_argument("--back-bars", dest="back_bars", type=int, default=None,
                    help="Fetch this many bars back from now (overrides --limit).")
    ap.add_argument("-o", "--output", default=env_output, help="SQLite DB path")
    ap.add_argument("--exchange", default=env_exchange)
    ap.add_argument("--fresh", action="store_true", default=env_fresh)
    ap.add_argument("--ccxt-symbol-format", dest="ccxt_symbol_format",
                    choices=["auto","usdtm","usdt","spot_only","perp_only"], default=env_ccxt_fmt,
                    help="Bias how symbols are resolved on the exchange")
    args = ap.parse_args()

    if ccxt is None:
        print("ERROR: ccxt not installed. pip install ccxt", file=sys.stderr)
        sys.exit(2)

    # Normalize timeframe (so '5min' works)
    args.timeframe = normalize_timeframe(args.timeframe)

    ensure_schema(args.output)
    if args.fresh:
        con = sqlite3.connect(args.output)
        cur = con.cursor()
        cur.execute("DROP TABLE IF EXISTS price_indicators")
        con.commit()
        con.close()
        ensure_schema(args.output)

    # CCXT exchange init + API keys from env (.env)
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

    # Optional proxies from env
    http_proxy = os.getenv("HTTP_PROXY") or os.getenv("PROXY") or os.getenv("PROXY_HTTP")
    https_proxy = os.getenv("HTTPS_PROXY") or os.getenv("PROXY_HTTPS")
    if http_proxy or https_proxy:
        ex_kwargs["proxies"] = {}
        if http_proxy: ex_kwargs["proxies"]["http"] = http_proxy
        if https_proxy: ex_kwargs["proxies"]["https"] = https_proxy

    ex = ex_klass(ex_kwargs)
    ex.enableRateLimit = True
    ex.load_markets()

    # Resolve time bounds
    start_ms = None
    end_ms = None
    if args.back_bars is not None and args.back_bars > 0:
        # will be computed per-symbol using exchange now()
        pass
    elif args.start_utc is not None:
        start_ms = _parse_dt_to_ms_utc(args.start_utc)
        if args.end_utc:
            end_ms = _parse_dt_to_ms_utc(args.end_utc)
        else:
            end_ms = ex.milliseconds() if hasattr(ex, "milliseconds") else int(pd.Timestamp.utcnow().value // 10**6)

    # Load universe
    symbols = load_universe_symbols(args.input_csv)
    bases = [normalize_token(x) for x in symbols]

    tf_seconds = timeframe_to_seconds(args.timeframe)
    tf_ms = tf_seconds * 1000

    for raw in bases:
        mkt = resolve_market(ex, raw, fmt_bias=args.ccxt_symbol_format)
        if not mkt:
            print(f"[SKIP] {raw} — no matching market on {args.exchange}")
            continue
        try:
            # Choose fetching mode
            if args.back_bars is not None and args.back_bars > 0:
                now_ms = ex.milliseconds() if hasattr(ex, "milliseconds") else int(pd.Timestamp.utcnow().value // 10**6)
                start_limit_ms = max(0, now_ms - args.back_bars * tf_ms)
                last_ms = last_timestamp_ms(args.output, mkt)
                fetch_start_ms = start_limit_ms
                if last_ms is not None:
                    fetch_start_ms = max(start_limit_ms, last_ms + tf_ms)
                df = fetch_ohlcv_range(ex, mkt, args.timeframe, fetch_start_ms, now_ms)
            elif start_ms is not None and end_ms is not None:
                df = fetch_ohlcv_range(ex, mkt, args.timeframe, start_ms, end_ms)
            else:
                # legacy single-shot fetch (<=1440 on most exchanges)
                lim = min(MAX_PER_REQUEST, int(args.limit))
                df = fetch_ohlcv_limit(ex, mkt, args.timeframe, lim)

            expected_rows = None
            if args.back_bars is not None and args.back_bars > 0:
                expected_rows = args.back_bars
            elif start_ms is not None and end_ms is not None:
                expected_rows = int(np.ceil((end_ms - start_ms) / tf_ms))

            if df.empty and expected_rows is not None:
                # Symbol might be newly listed and lacks full history; fetch what is available.
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
            insert_ignore_rows(args.output, rows)
            msg = "[OK]"
            if expected_rows is not None and len(rows) < expected_rows:
                msg = "[WARN]"
            print(f"{msg} {raw} -> {mkt} tf={args.timeframe} rows={len(rows)}")
        except Exception as e:
            print(f"[ERR] {raw} -> {mkt} {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
