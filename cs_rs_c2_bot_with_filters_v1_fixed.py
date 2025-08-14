#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cs_rs_c2_bot_with_filters_v1.py
# Cross-Sectional RS (C2, LONG-only) bot (paper/live).
# Modeled after short_top_gainers_bot_with_filters_v12.py with similar CLI.

""" 
python3 cs_rs_c2_bot_with_filters_v1.py \
  --config cs_rs_c2_v1.yaml \
  --env-file .env \
  --source ccxt --ccxt-exchange bingx --ccxt-symbol-format usdtm \
  --force-start-always --reset-state --debug \
  --quote USDT --max-universe 550 --top-n 4

"""

import os, sys, json, time, argparse, traceback
import datetime as dt
from typing import List, Dict, Any, Optional

import requests
import pandas as pd
import numpy as np

try:
    import yaml
except Exception:
    yaml = None

try:
    import ccxt  # optional
except Exception:
    ccxt = None

BASE_URL = "https://open-api.bingx.com"

def now_utc() -> dt.datetime:
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)

def log(msg: str, logfile: str = ""):
    line = f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')} UTC] {msg}"
    print(line, flush=True)
    if logfile:
        try:
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

def load_env_from_file(path: str):
    if not path or not os.path.exists(path):
        return
    for raw in open(path, "r", encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

def mask(val: str) -> str:
    if not val:
        return "<empty>"
    return val[:3] + "..." + val[-3:] if len(val) >= 7 else "***"

def make_session():
    s = requests.Session()
    try:
        from urllib3.util.retry import Retry
        from requests.adapters import HTTPAdapter
        try:
            retry = Retry(total=3, backoff_factor=0.3,
                          status_forcelist=[429,500,502,503,504],
                          allowed_methods=["GET","POST"],
                          raise_on_status=False)
        except TypeError:
            retry = Retry(total=3, backoff_factor=0.3,
                          status_forcelist=[429,500,502,503,504],
                          method_whitelist=["GET","POST"],
                          raise_on_status=False)
        ad = HTTPAdapter(max_retries=retry)
        s.mount("https://", ad); s.mount("http://", ad)
    except Exception:
        pass
    return s

SESSION = make_session()

def http_fetch_ticker(symbol: str) -> dict:
    r = SESSION.get(f"{BASE_URL}/openApi/swap/v2/quote/ticker",
                    params={"symbol": symbol}, timeout=(3,8))
    r.raise_for_status()
    data = r.json().get("data") or {}
    return data[0] if isinstance(data, list) and data else data

def http_fetch_klines(symbol: str, interval="1h", limit=150) -> list:
    r = SESSION.get(f"{BASE_URL}/openApi/swap/v3/quote/klines",
                    params={"symbol": symbol, "interval": interval, "limit": limit},
                    timeout=(5,12))
    r.raise_for_status()
    data = r.json().get("data") or r.json()
    return data if isinstance(data, list) else []

def http_place_order(api_key: str, secret: str, payload: Dict[str, Any]) -> dict:
    url = f"{BASE_URL}/openApi/swap/v2/trade/order"
    headers = {"X-BX-APIKEY": api_key}
    r = SESSION.post(url, headers=headers, json=payload, timeout=(5,12))
    try:
        r.raise_for_status()
    except Exception:
        return {"status": r.status_code, "text": r.text}
    try:
        return r.json()
    except Exception:
        return {"status": r.status_code, "text": r.text}


class CCXTFetcher:
    def __init__(self, exchange="bingx", symbol_format="usdtm", debug=False, logfile=""):
        if ccxt is None:
            raise RuntimeError("ccxt is not installed. Try: pip install 'ccxt<5'")
        self.debug = debug
        self.logfile = logfile
        if not hasattr(ccxt, exchange):
            raise RuntimeError(f"ccxt.{exchange} not found")
        self.ex = getattr(ccxt, exchange)({"enableRateLimit": True})
        self.symbol_format = symbol_format
        # Preload markets and build quick lookup for USDT swaps
        try:
            self.markets = self.ex.load_markets()
        except Exception as e:
            self.markets = {}
            log(f"[ccxt load_markets] {e}", self.logfile)
        self._swap_by_base = {}
        for m in self.markets.values():
            try:
                base = m.get("base")
                quote = m.get("quote")
                if not base or not quote:
                    continue
                if m.get("swap") and (quote == "USDT"):
                    # linear USDT-margined perp
                    self._swap_by_base[base] = m.get("symbol")  # e.g., 'BTC/USDT:USDT'
            except Exception:
                continue

    def resolve_symbol(self, s: str) -> str:
        """Return a CCXT symbol string for the requested format.

        For 'usdtm' prefer linear USDT swaps; else fall back to spot if requested."""
        base, quote = s.split("-")
        if self.symbol_format == "usdtm":
            sym = self._swap_by_base.get(base)
            if sym:
                return sym
            # as a fallback, try the exact 'BASE/USDT:USDT' if it exists
            candidate = f"{base}/USDT:USDT"
            if candidate in self.markets:
                return candidate
            # no perp found
            raise ccxt.BadSymbol(f"No USDT-margined swap for {base}")
        # spot mode
        return f"{base}/{quote}"

    def fetch_ticker_price(self, symbol: str) -> Optional[float]:
        try:
            ccxt_sym = self.resolve_symbol(symbol)
        except Exception as e:
            log(f"[ccxt price] {symbol}: {e}", self.logfile)
            return None
        try:
            t = self.ex.fetch_ticker(ccxt_sym)
            for k in ("last", "close", "bid", "ask"):
                if k in t and t[k] is not None:
                    return float(t[k])
        except Exception as e:
            log(f"[ccxt price] {symbol}: {e}", self.logfile)
        return None

    def fetch_ohlcv_df(self, symbol: str, timeframe="1h", limit=150) -> Optional[pd.DataFrame]:
        try:
            ccxt_sym = self.resolve_symbol(symbol)
        except Exception as e:
            log(f"[ccxt ohlcv] {symbol}: {e}", self.logfile)
            return None
        try:
            data = self.ex.fetch_ohlcv(ccxt_sym, timeframe=timeframe, limit=limit)
        except Exception as e:
            log(f"[ccxt ohlcv] {symbol}: {e}", self.logfile)
            return None
        if not data:
            return None
        cols = ["ts","open","high","low","close","volume"]
        df = pd.DataFrame(data, columns=cols)
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("ts", inplace=True)
        return df

    def fetch_ticker_price(self, symbol: str) -> Optional[float]:
        ccxt_sym = self._map_symbol(symbol)
        try:
            t = self.ex.fetch_ticker(ccxt_sym)
            for k in ("last", "close", "ask", "bid"):
                if k in t and t[k] is not None:
                    return float(t[k])
        except Exception as e:
            log(f"[ccxt ticker] {symbol}: {e}", self.logfile)
        return None

def timeframe_hours(tf: str) -> float:
    tf = (tf or "").lower().strip()
    if tf.endswith("h"):
        try: return float(tf[:-1])
        except: return 1.0
    if tf.endswith("m"):
        try: return float(tf[:-1]) / 60.0
        except: return 1.0
    if tf.endswith("d"):
        try: return float(tf[:-1]) * 24.0
        except: return 24.0
    return 1.0

def compute_indicators_df(symbol: str, df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    h = df["high"].to_numpy(dtype="float64")
    l = df["low"].to_numpy(dtype="float64")
    c = df["close"].to_numpy(dtype="float64")
    v = df["volume"].to_numpy(dtype="float64")

    def atr_ratio_arr(high, low, close, period=14):
        import numpy as np, pandas as pd
        h = np.array(high, float); l = np.array(low, float); c = np.array(close, float)
        if len(c) < period+1: return np.full(len(c), np.nan)
        pc = np.r_[np.nan, c[:-1]]
        tr = np.vstack([h-l, np.abs(h-pc), np.abs(l-pc)])
        tr = np.nanmax(tr, axis=0)
        atr = pd.Series(tr).rolling(period, min_periods=period).mean().values
        return atr/np.maximum(c,1e-12)

    atr_arr = atr_ratio_arr(h, l, c, 14)
    qv_arr  = v * c

    step_h = max(0.00001, timeframe_hours(timeframe))
    win = max(1, int(round(24.0/step_h)))
    qv24_arr = pd.Series(qv_arr).rolling(win, min_periods=win).sum().to_numpy()
    avg1h = qv24_arr / np.maximum(win, 1.0)

    p6 = max(1, int(round(6.0/step_h)))
    p12 = max(1, int(round(12.0/step_h)))
    dp6 = pd.Series(c).pct_change(p6).to_numpy()
    dp12 = pd.Series(c).pct_change(p12).to_numpy()

    m = min(len(df), len(atr_arr), len(qv_arr), len(qv24_arr), len(dp6), len(dp12), len(avg1h))
    if m <= 0: return df.iloc[0:0].copy()
    if len(df) != m: df = df.iloc[-m:].copy()

    df["atr_ratio"]    = atr_arr[-m:]
    df["quote_volume"] = qv_arr[-m:]
    df["qv_24h"]       = qv24_arr[-m:]
    df["dp6h"]         = dp6[-m:]
    df["dp12h"]        = dp12[-m:]
    df["vol_surge_mult"] = (df["quote_volume"] / np.maximum(avg1h[-m:], 1e-12)).replace([np.inf, -np.inf], np.nan)
    return df

def load_state(path: str) -> Dict[str, Any]:
    if os.path.exists(path):
        try:
            return json.load(open(path, "r", encoding="utf-8"))
        except Exception:
            pass
    return {"open_positions": {}, "last_open_time_by_sym": {}, "_force_used": False}

def save_state(path: str, st: Dict[str, Any]):
    try:
        json.dump(st, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"[state save] {e}")

def append_trade_csv(csv_path: str, row: Dict[str, Any]):
    cols = ["ts_utc","symbol","side","qty","price","note","mode"]
    exists = os.path.exists(csv_path)
    import csv
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k) for k in cols})


def load_universe(args) -> List[str]:
    if args.symbols:
        syms = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
        if args.debug:
            log(f"[dbg] symbols from args: {syms}", args.logfile)
        return syms
    if args.source == "ccxt" and args.ccxt_fetcher is not None:
        try:
            markets = args.ccxt_fetcher.markets or args.ccxt_fetcher.ex.load_markets()
            out = []
            for m in markets.values():
                try:
                    # Keep only linear USDT-margined swaps (perps)
                    if not (m.get("swap") and m.get("quote") == "USDT"):
                        continue
                    base = m.get("base")
                    if not base:
                        continue
                    out.append(f"{base}-USDT")
                except Exception:
                    continue
            out = sorted(set(out))
            if args.max_universe > 0:
                out = out[: int(args.max_universe)]
            if args.debug:
                log(f"[dbg] universe size={len(out)} (USDT swaps)", args.logfile)
            return out
        except Exception as e:
            log(f"[ccxt markets] {e}", args.logfile)
    return []

def fetch_ohlcv_df_http(symbol: str, limit=150, timeframe="1h") -> Optional[pd.DataFrame]:
    raw = http_fetch_klines(symbol, interval=timeframe, limit=limit)
    rows = []
    for it in raw:
        if isinstance(it, dict):
            ts = it.get("time"); o=it.get("open"); h=it.get("high"); l=it.get("low"); c=it.get("close"); v=it.get("volume")
        else:
            try: ts,o,h,l,c,v = it[:6]
            except: continue
        if None in (ts,o,h,l,c,v): continue
        t = pd.to_datetime(int(ts), unit="ms", utc=True) if isinstance(ts,(int,float,str)) else pd.to_datetime(ts, utc=True)
        rows.append({"ts": t, "open": float(o), "high": float(h),
                     "low": float(l), "close": float(c), "volume": float(v)})
    if not rows: return None
    df = (pd.DataFrame(rows).drop_duplicates("ts").sort_values("ts").set_index("ts"))
    if len(df) < 40: return None
    return df

def fetch_df(symbol: str, args) -> Optional[pd.DataFrame]:
    if args.source == "ccxt" and args.ccxt_fetcher is not None:
        return args.ccxt_fetcher.fetch_ohlcv_df(symbol, timeframe=args.timeframe, limit=args.limit_klines)
    return fetch_ohlcv_df_http(symbol, limit=args.limit_klines, timeframe=args.timeframe)

def select_candidates(universe: List[str], args) -> List[dict]:
    recs = []
    for s in universe:
        try:
            df = fetch_df(s, args)
            if df is None: 
                log(f"[dbg] {s}: df=None", args.logfile); 
                continue
            idf = compute_indicators_df(s, df, args.timeframe)
            if len(idf) < 40: 
                log(f"[dbg] {s}: idf short {len(idf)}", args.logfile)
                continue
            r = idf.iloc[-1]
            qv1  = float(r.get("quote_volume", 0.0) or 0.0)
            qv24 = float(r.get("qv_24h", 0.0) or 0.0)
            if not (qv24 >= args.min_qv_24h and qv1 >= args.min_qv_1h):
                continue

            atr  = float(r.get("atr_ratio", 0.0) or 0.0)
            dp6  = float(r.get("dp6h", 0.0) or 0.0)
            dp12 = float(r.get("dp12h", 0.0) or 0.0)
            mom  = dp6 + dp12
            volm = float(r.get("vol_surge_mult", 0.0) or 0.0)

            px = None
            if args.source == "ccxt" and args.ccxt_fetcher is not None:
                px = args.ccxt_fetcher.fetch_ticker_price(s)
            if px is None:
                t = http_fetch_ticker(s)
                for k in ("lastPrice","price","last","close"):
                    if k in t and t[k] is not None:
                        try: px = float(t[k]); break
                        except: pass
            if not px: 
                continue

            recs.append({"symbol": s, "price": float(px), "atr": atr, "mom": mom, "vol_mult": volm, "qv1": qv1, "qv24": qv24})
        except Exception as e:
            log(f"[select] {s}: {e}", args.logfile)
            if args.debug: traceback.print_exc()

    atr_pass = [r for r in recs if r["atr"] >= args.min_atr_ratio]
    total = len(atr_pass)
    positives = sum(1 for r in atr_pass if r["mom"] > 0)
    breadth = (positives / total) if total>0 else 0.0
    log(f"[breadth] {positives}/{total} = {breadth:.2f}", args.logfile)

    valids = [r for r in atr_pass 
              if (r["mom"] >= args.min_momentum_sum and 
                  r["vol_mult"] >= args.min_vol_surge_mult and 
                  breadth >= args.min_breadth)]

    valids.sort(key=lambda x: x["mom"], reverse=True)
    return valids[: int(args.top_n)]

def compute_qty_usdt(price: float, args) -> float:
    target = float(args.position_notional)
    cap = float(args.max_notional_frac) * float(args.initial_equity)
    notional = min(target, cap)
    qty = round(notional / max(1e-12, price), 6)
    return max(qty, 0.0)

def place_order_live(side: str, symbol: str, qty: float, args) -> Optional[dict]:
    api_key = os.getenv("BINGX_KEY","")
    secret  = os.getenv("BINGX_SECRET","")
    if not api_key or not secret:
        log("[LIVE] missing API keys — abort", args.logfile); return None
    payload = {
        "symbol": symbol,
        "side": "BUY" if side=="LONG" else "SELL",
        "positionSide": "LONG" if side=="LONG" else "SHORT",
        "type": "MARKET",
        "quantity": round(float(qty), 6),
        "reduceOnly": False,
        "tif": "IOC",
    }
    return http_place_order(api_key, secret, payload)

def close_order_live(side: str, symbol: str, qty: float, args) -> Optional[dict]:
    api_key = os.getenv("BINGX_KEY","")
    secret  = os.getenv("BINGX_SECRET","")
    if not api_key or not secret:
        log("[LIVE] missing API keys — abort", args.logfile); return None
    payload = {
        "symbol": symbol,
        "side": "SELL" if side=="LONG" else "BUY",
        "positionSide": "LONG" if side=="LONG" else "SHORT",
        "type": "MARKET",
        "quantity": round(float(qty), 6),
        "reduceOnly": True,
        "tif": "IOC",
    }
    return http_place_order(api_key, secret, payload)

def check_exit(symbol: str, info: Dict[str, Any], args) -> Optional[str]:
    try:
        px = None
        if args.source == "ccxt" and args.ccxt_fetcher is not None:
            px = args.ccxt_fetcher.fetch_ticker_price(symbol)
        if px is None:
            t = http_fetch_ticker(symbol)
            for k in ("lastPrice","price","last","close"):
                if k in t and t[k] is not None:
                    px = float(t[k]); break
        if px is None:
            return None

        side = info.get("side","LONG")
        entry = float(info.get("entry", 0.0))
        atr = float(info.get("atr", 0.0))
        mom_flip = float(args.mom_flip_thresh)
        max_mae = float(args.max_mae_atr_mult)

        elapsed_h = (now_utc() - dt.datetime.fromisoformat(info["opened_at"])).total_seconds() / 3600.0
        if elapsed_h >= float(args.max_hold_hours):
            return "time_exit"

        ret = (px - entry)/max(entry,1e-12) if side=="LONG" else (entry - px)/max(entry,1e-12)
        if ret < -max_mae*atr:
            return "mae_break"

        df = fetch_df(symbol, args)
        if df is not None:
            idf = compute_indicators_df(symbol, df, args.timeframe)
            if len(idf)>=13:
                r = idf.iloc[-1]
                mom_sum = float(r.get("dp6h",0.0) or 0.0) + float(r.get("dp12h",0.0) or 0.0)
                if side=="LONG" and mom_sum < mom_flip:
                    return "mom_flip"

        ts = float(args.trail_start_atr); td = float(args.trail_dist_atr)
        if atr>0 and ts>0:
            up = (px - entry)/max(entry,1e-12) if side=="LONG" else (entry - px)/max(entry,1e-12)
            if up >= ts*atr:
                new_stop = px*(1.0 - td*atr) if side=="LONG" else px*(1.0 + td*atr)
                if new_stop > info.get("stop", 0.0):
                    info["stop"] = new_stop
        stop = float(info.get("stop") or 0.0); take = float(info.get("take") or 0.0)
        if stop and px <= stop: return "hard_stop"
        if take and px >= take: return "take_profit"

    except Exception as e:
        log(f"[check_exit] {symbol}: {e}", args.logfile)
    return None

def run_open_scan(args, state: Dict[str, Any]) -> int:
    universe = load_universe(args)
    cands = select_candidates(universe, args)
    opened = 0
    for r in cands:
        sym, px, atr, mom, volm = r["symbol"], r["price"], r["atr"], r["mom"], r["vol_mult"]
        if sym in state["open_positions"]:
            continue
        qty = compute_qty_usdt(px*(1+args.slippage_per_side), args)
        stop = px*(1.0 - args.sl_atr_mult*atr) if args.sl_atr_mult>0 else 0.0
        take = px*(1.0 + args.tp_atr_mult*atr) if args.tp_atr_mult>0 else 0.0

        if args.papertrade:
            state["open_positions"][sym] = {
                "qty": qty, "entry": px, "side": "LONG",
                "opened_at": now_utc().isoformat(), "atr": atr, "mom_at_entry": mom,
                "stop": stop, "take": take
            }
            opened += 1
            log(f"🟢 [PAPER] OPEN_LONG {sym} px={px:.6f} mom={mom:.3f} atr={atr:.3f} volx={volm:.2f}", args.logfile)
        else:
            resp = place_order_live("LONG", sym, qty, args)
            state["open_positions"][sym] = {
                "qty": qty, "entry": px, "side": "LONG",
                "opened_at": now_utc().isoformat(), "atr": atr, "mom_at_entry": mom,
                "stop": stop, "take": take, "live_open": resp
            }
            opened += 1
            log(f"🟢 [LIVE] OPEN_LONG {sym} px={px:.6f} mom={mom:.3f} atr={atr:.3f} volx={volm:.2f}", args.logfile)
        append_trade_csv(args.trades_csv, {
            "ts_utc": now_utc().isoformat(), "symbol": sym, "side": "OPEN_LONG",
            "qty": qty, "price": px, "note": f"mom={mom:.3f} volx={volm:.2f}", "mode": "PAPER" if args.papertrade else "LIVE"
        })

    save_state(args.state_path, state)
    return opened

def run_close_scan(args, state: Dict[str, Any]) -> int:
    closed = 0
    for sym in list(state["open_positions"].keys()):
        info = state["open_positions"][sym]
        reason = check_exit(sym, info, args)
        if not reason: 
            continue
        qty = info.get("qty", 0.0)
        if args.papertrade:
            log(f"🔴 [PAPER] CLOSE_LONG {sym} reason={reason}", args.logfile)
        else:
            resp = close_order_live("LONG", sym, qty, args)
            log(f"🔴 [LIVE] CLOSE_LONG {sym} reason={reason} resp={resp}", args.logfile)
        append_trade_csv(args.trades_csv, {
            "ts_utc": now_utc().isoformat(), "symbol": sym, "side": "CLOSE_LONG",
            "qty": qty, "price": np.nan, "note": reason, "mode": "PAPER" if args.papertrade else "LIVE"
        })
        state["open_positions"].pop(sym, None)
        closed += 1

    if closed:
        save_state(args.state_path, state)
    return closed

def build_arg_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--env-file", default=".env")
    p.add_argument("--api-key", default=None)
    p.add_argument("--api-secret", default=None)
    p.add_argument("--config", help="YAML config (optional)")
    p.add_argument("--source", default="http", choices=["http","ccxt"])
    p.add_argument("--ccxt-exchange", default="bingx")
    p.add_argument("--ccxt-symbol-format", default="usdtm", choices=["spot","usdtm"])
    p.add_argument("--papertrade", action="store_true")
    p.add_argument("--live", action="store_true")
    p.add_argument("--force-start-always", action="store_true")
    p.add_argument("--reset-state", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--symbols", default="")
    p.add_argument("--quote", default="USDT")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--limit-klines", type=int, default=150)
    p.add_argument("--max-universe", type=int, default=550)
    p.add_argument("--top-n", type=int, default=4, dest="top_n")
    p.add_argument("--min-qv-24h", type=float, default=200000)
    p.add_argument("--min-qv-1h", type=float, default=10000)
    p.add_argument("--min-atr-ratio", type=float, default=0.022)
    p.add_argument("--min-momentum-sum", type=float, default=0.12)
    p.add_argument("--min-vol-surge-mult", type=float, default=1.25)
    p.add_argument("--min-breadth", type=float, default=0.60)
    p.add_argument("--sl-atr-mult", type=float, default=1.4)
    p.add_argument("--tp-atr-mult", type=float, default=2.6)
    p.add_argument("--max-hold-hours", type=float, default=96)
    p.add_argument("--max-mae-atr-mult", type=float, default=1.6)
    p.add_argument("--mom-flip-thresh", type=float, default=0.02)
    p.add_argument("--trail-start-atr", type=float, default=1.2)
    p.add_argument("--trail-dist-atr", type=float, default=1.0)
    p.add_argument("--position-notional", type=float, default=20.0)
    p.add_argument("--max-notional-frac", type=float, default=0.5)
    p.add_argument("--initial-equity", type=float, default=200.0)
    p.add_argument("--slippage-per-side", type=float, default=0.0003)
    p.add_argument("--fee-rate", type=float, default=0.001)
    p.add_argument("--funding-rate-hour", type=float, default=0.00002)
    p.add_argument("--logfile", default="c2_bot.log")
    p.add_argument("--trades-csv", default="c2_trades.csv")
    p.add_argument("--state-path", default="c2_state.json")
    return p

def yaml_merge(args: argparse.Namespace, parser: argparse.ArgumentParser) -> argparse.Namespace:
    if not getattr(args, "config", None) or not yaml:
        return args
    path = args.config
    if not path or not os.path.exists(path):
        return args
    try:
        conf = yaml.safe_load(open(path, "r", encoding="utf-8")) or {}
    except Exception as e:
        log(f"[warn] YAML parse failed: {e}", args.logfile)
        return args
    defaults = parser.parse_args([])
    applied = []
    for k, v in conf.items():
        ck = k.replace("-", "_")
        if hasattr(args, ck) and getattr(args, ck) == getattr(defaults, ck):
            setattr(args, ck, v)
            applied.append(ck)
    if getattr(args, "debug", False) and applied:
        log(f"[yaml] applied: {sorted(applied)}", args.logfile)
    return args

def main():
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.env_file: load_env_from_file(args.env_file)
    if args.api_key: os.environ["BINGX_KEY"] = args.api_key
    if args.api_secret: os.environ["BINGX_SECRET"] = args.api_secret
    args.ccxt_fetcher = None
    if args.source == "ccxt":
        try:
            args.ccxt_fetcher = CCXTFetcher(exchange=args.ccxt_exchange,
                                            symbol_format=args.ccxt_symbol_format,
                                            debug=args.debug, logfile=args.logfile)
        except Exception as e:
            log(f"[ccxt-init] {e} — fallback to HTTP", args.logfile)
            args.source = "http"
    if args.config: args = yaml_merge(args, parser)
    if args.reset_state and os.path.exists(args.state_path):
        try: os.remove(args.state_path)
        except: pass
    state = load_state(args.state_path)
    log(f'API: key="{mask(os.getenv("BINGX_KEY",""))}", secret="{mask(os.getenv("BINGX_SECRET",""))}"', args.logfile)
    if args.force_start_always:
        opened = run_open_scan(args, state)
        log(f"[open] opened={opened}", args.logfile)
    closed = run_close_scan(args, state)
    log(f"[close] closed={closed}", args.logfile)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted.")
    except Exception as e:
        log(f"Fatal: {e}")
        traceback.print_exc()
