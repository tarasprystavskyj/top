
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_build_cache_v14.py
- Loads .env for config defaults (api keys, args)
- SQLite-compatible (INSERT OR REPLACE)
- Produces table 'price_indicators' with columns:
  symbol TEXT, datetime_utc TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
  rsi REAL, stochastic REAL, mfi REAL, overbought_index REAL,
  atr_ratio REAL, gain_24h_before REAL, dp6h REAL, dp12h REAL,
  quote_volume REAL, qv_24h REAL, vol_surge_mult REAL
- Market resolve robust for BingX (and CCXT exchanges), with format bias

Examples:
  python3 fetch_build_cache_v14.py \
    -i universe_symbols_bingx.csv \
    -t 1h --limit 1440 \
    -o combined_cache_1440.db \
    --exchange bingx \
    --ccxt-symbol-format usdtm \
    --fresh

  # 5-minute timeframe, pulling defaults from .env if present
  python3 fetch_build_cache_v14.py -t 5m
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

    # Build candidate ladders per bias
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
        # place all entries with the guessed quote first
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
    # also allow raw ccxt ones like 5m, 1h, 1d
    return tf


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

def insert_or_replace_rows(db_path: str, rows: List[dict]) -> None:
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
        f"INSERT OR REPLACE INTO price_indicators ({','.join(cols)}) VALUES ({placeholders})",
        data
    )
    con.commit()
    con.close()


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

def timeframe_to_seconds(tf: str) -> int:
    """Return seconds represented by a CCXT timeframe string like '5m', '30m', '1h', '1d'."""
    tf = normalize_timeframe(tf)
    tf = tf.strip().lower()
    units = {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800, 'M': 2592000}
    try:
        val = int(tf[:-1])
        unit = tf[-1]
        return val * units.get(unit, 3600)
    except Exception:
        # fallback: default 1h
        return 3600

def compute_features(df: pd.DataFrame, tf_seconds: int = 3600) -> pd.DataFrame:
    out = df.copy()
    # time-aware windows based on chosen timeframe
    bars_24h = max(1, int(round(24*3600 / max(1, tf_seconds))))
    bars_12h = max(1, int(round(12*3600 / max(1, tf_seconds))))
    bars_6h  = max(1, int(round( 6*3600 / max(1, tf_seconds))))
    out["gain_24h_before"] = (out["close"] / out["close"].shift(bars_24h) - 1.0).fillna(0.0)
    out["dp6h"]  = (out["close"] / out["close"].shift(bars_6h) - 1.0).fillna(0.0)
    out["dp12h"] = (out["close"] / out["close"].shift(bars_12h) - 1.0).fillna(0.0)
    out["atr_ratio"] = calc_atr_ratio(out, 14)
    out["quote_volume"] = (out["volume"] * out["close"]).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    # rolling sums sized for 24h worth of bars and per-bar average for surge multiple
    bars_24h = max(1, int(round(24*3600 / max(1, tf_seconds))))
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


# -------- CCXT fetch --------

def fetch_ohlcv(ex, market: str, timeframe: str, limit: int) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(market, timeframe=timeframe, limit=limit)
    if not ohlcv:
        return pd.DataFrame()
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["datetime_utc"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    df = df.set_index("datetime_utc")[["open", "high", "low", "close", "volume"]].astype(float)
    return df


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
    ap.add_argument("--limit", type=int, default=env_limit)
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

    # Load universe
    uni = pd.read_csv(args.input_csv)
    if "symbol" not in uni.columns:
        raise SystemExit("CSV must contain 'symbol' column.")
    bases = [normalize_token(x) for x in uni["symbol"].dropna().unique().tolist()]

    for raw in bases:
        mkt = resolve_market(ex, raw, fmt_bias=args.ccxt_symbol_format)
        if not mkt:
            print(f"[SKIP] {raw} — no matching market on {args.exchange}")
            continue
        try:
            df = fetch_ohlcv(ex, mkt, args.timeframe, args.limit)
            if df.empty:
                print(f"[WARN] {mkt} — no OHLCV")
                continue
            feats = compute_features(df, tf_seconds=timeframe_to_seconds(args.timeframe))
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
                    "rsi": float(r["rsi"]),
                    "stochastic": float(r["stochastic"]),
                    "mfi": float(r["mfi"]),
                    "overbought_index": float(r["overbought_index"]),
                    "atr_ratio": float(r["atr_ratio"]),
                    "gain_24h_before": float(r["gain_24h_before"]),
                    "dp6h": float(r["dp6h"]),
                    "dp12h": float(r["dp12h"]),
                    "quote_volume": float(r["quote_volume"]),
                    "qv_24h": float(r["qv_24h"]),
                    "vol_surge_mult": float(r["vol_surge_mult"]),
                })
            insert_or_replace_rows(args.output, rows)
            print(f"[OK] {raw} -> {mkt} tf={args.timeframe} rows={len(rows)}")
        except Exception as e:
            print(f"[ERR] {raw} -> {mkt} {e}", file=sys.stderr)

if __name__ == "__main__":
    main()


def timeframe_to_seconds(tf: str) -> int:
    """Return seconds represented by a CCXT timeframe string like '5m', '30m', '1h', '1d'."""
    tf = normalize_timeframe(tf)
    tf = tf.strip().lower()
    units = {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800, 'M': 2592000}
    try:
        val = int(tf[:-1])
        unit = tf[-1]
        return val * units.get(unit, 3600)
    except Exception:
        # fallback: default 1h
        return 3600
