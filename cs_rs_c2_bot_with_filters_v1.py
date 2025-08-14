#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cs_rs_c2_bot_with_filters_v1_fixed.py — C2-fixed r4
- CCXT-only data path (no HTTP fallback)
- Strict USDT-perp universe (linear swaps only)
- Symbol resolver + back-compat shim _map_symbol()
- Verbose per-symbol SKIP reasons, breadth, and final selection
- Optional --debug-universe to log why a market is excluded from the universe
"""
import os, argparse, json, math, traceback
from datetime import datetime
from typing import List, Optional
import pandas as pd
import numpy as np

try:
    import yaml
except Exception:
    yaml = None

try:
    import ccxt  # keep <5 in your env
except Exception:
    ccxt = None

# -------- utils ----------
def now_utc():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg: str, logfile: str = ""):
    line = f"[{now_utc()}] {msg}"
    print(line, flush=True)
    if logfile:
        try:
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

def load_env_from_file(path: str):
    if not path or not os.path.exists(path): return
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln: continue
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

def mask(val: str) -> str:
    if not val: return "<empty>"
    return val[:3] + "..." + val[-3:] if len(val) >= 7 else "***"

# -------- CCXT fetcher ----------
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
        try:
            self.markets = self.ex.load_markets()
        except Exception as e:
            self.markets = {}
            log(f"[ccxt load_markets] {e}", self.logfile)
        # Build map of linear USDT swaps by BASE
        self._swap_by_base = {}
        for m in self.markets.values():
            try:
                if m.get("swap") and m.get("quote") == "USDT":
                    base = m.get("base")
                    if base:
                        self._swap_by_base[base] = m.get("symbol")  # e.g. 'BTC/USDT:USDT'
            except Exception:
                pass

    def resolve_symbol(self, s: str) -> str:
        """Return CCXT symbol string for a BASE-QUOTE like 'BTC-USDT'. For usdtm, ensure a linear USDT swap exists."""
        base, quote = s.split("-")
        if self.symbol_format == "usdtm":
            sym = self._swap_by_base.get(base)
            if sym:
                return sym
            cand = f"{base}/USDT:USDT"
            if cand in self.markets:
                return cand
            raise ccxt.BadSymbol(f"No USDT-margined swap for {base}")
        return f"{base}/{quote}"

    # Backward-compat shim (so even old call-sites won't crash)
    def _map_symbol(self, s: str) -> str:
        return self.resolve_symbol(s)

    def fetch_ticker_price(self, symbol: str) -> Optional[float]:
        """Ticker price via CCXT only; never HTTP."""
        try:
            ccxt_sym = self.resolve_symbol(symbol)
        except Exception as e:
            log(f"[ccxt price] {symbol}: {e}", self.logfile)
            return None
        try:
            t = self.ex.fetch_ticker(ccxt_sym)
            for k in ("last","close","bid","ask"):
                v = t.get(k)
                if v is not None:
                    return float(v)
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
        df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("ts", inplace=True)
        return df

# -------- indicators ----------
def compute_indicators_df(symbol: str, df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    o = df["open"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values
    v = df["volume"].astype(float).values
    n = len(df)
    if n < 30:
        return pd.DataFrame()
    # ATR(14) %
    tr = np.zeros(n); tr[0] = h[0]-l[0]
    for i in range(1, n):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    atr = pd.Series(tr).rolling(14, min_periods=1).mean().values
    atr_ratio = atr / np.maximum(c, 1e-12)
    # dp6h, dp12h
    def pct(a, b): return (c[b]-c[a]) / max(c[a], 1e-12)
    dp6 = np.zeros(n); dp12 = np.zeros(n)
    for i in range(n):
        dp6[i]  = pct(max(0, i-6),  i)
        dp12[i] = pct(max(0, i-12), i)
    # quote volumes
    qv_bar = c * v
    qv_24h = pd.Series(qv_bar).rolling(24, min_periods=1).sum().values
    avg_24 = pd.Series(qv_bar).rolling(24, min_periods=1).mean().values
    vol_surge_mult = np.where(avg_24>0, qv_bar/avg_24, 0.0)
    out = pd.DataFrame({
        "open": df["open"].values, "high": df["high"].values, "low": df["low"].values, "close": df["close"].values,
        "atr_ratio": atr_ratio, "dp6h": dp6, "dp12h": dp12,
        "qv_24h": qv_24h, "quote_volume": qv_bar, "vol_surge_mult": vol_surge_mult,
    }, index=df.index)
    return out

# -------- data router ----------
def fetch_df(symbol: str, args) -> Optional[pd.DataFrame]:
    return args.ccxt_fetcher.fetch_ohlcv_df(symbol, timeframe=args.timeframe, limit=args.limit_klines)

# -------- selection ----------
def select_candidates(universe: List[str], args) -> List[dict]:
    log(f"[SELECT_THRESH] qv1h>={args.min_qv_1h:.0f}; qv24h>={args.min_qv_24h:.0f}; r%>={args.min_atr_ratio:.4f}; mom>={args.min_momentum_sum:.4f}; volx>={args.min_vol_surge_mult:.2f}; breadth>={args.min_breadth:.2f}", args.logfile)
    recs = []
    min_qv24 = float(args.min_qv_24h)
    min_qv1  = float(args.min_qv_1h)
    for s in universe:
        try:
            df = fetch_df(s, args)
            if df is None: log(f"[filter] {s} -> SKIP df=None", args.logfile); continue
            idf = compute_indicators_df(s, df, args.timeframe)
            if len(idf) < 40: log(f"[filter] {s} -> SKIP indicators too short ({len(idf)})", args.logfile); continue
            r = idf.iloc[-1]
            qv1  = float(r.get("quote_volume", 0.0) or 0.0)
            qv24 = float(r.get("qv_24h", 0.0) or 0.0)
            if not (qv24 >= min_qv24 and qv1 >= min_qv1):
                log(f"[filter] {s} -> SKIP qv: qv1h={qv1:.0f} (min {min_qv1:.0f}), qv24h={qv24:.0f} (min {min_qv24:.0f})", args.logfile); continue
            atr  = float(r.get("atr_ratio", 0.0) or 0.0)
            dp6  = float(r.get("dp6h", 0.0) or 0.0)
            dp12 = float(r.get("dp12h", 0.0) or 0.0)
            mom  = dp6 + dp12
            volm = float(r.get("vol_surge_mult", 0.0) or 0.0)
            px = args.ccxt_fetcher.fetch_ticker_price(s)
            if not px: log(f"[filter] {s} -> SKIP no ticker price available", args.logfile); continue
            log(f"[select] {s} ok | px={px:.6g} r%={atr:.4f} dp6h={dp6:.4f} dp12h={dp12:.4f} mom={mom:.4f} volx={volm:.2f} qv1h={qv1:.0f} qv24h={qv24:.0f}", args.logfile)
            recs.append({"symbol": s, "price": float(px), "atr": atr, "mom": mom, "vol_mult": volm, "qv1": qv1, "qv24": qv24})
        except Exception as e:
            log(f"[select] {s}: {e}", args.logfile)
            if args.debug: traceback.print_exc()

    atr_pass = []
    for r in recs:
        if r["atr"] >= args.min_atr_ratio: atr_pass.append(r)
        else: log(f"[filter] {r['symbol']} -> SKIP r%: {r['atr']:.4f} < min {args.min_atr_ratio:.4f}", args.logfile)

    total = len(atr_pass)
    positives = sum(1 for r in atr_pass if r["mom"] > 0)
    breadth = (positives / total) if total>0 else 0.0
    log(f"[breadth] {positives}/{total} = {breadth:.2f} (min {args.min_breadth:.2f})", args.logfile)

    final_valids = []
    for r in atr_pass:
        reasons = []
        if r["mom"] < args.min_momentum_sum: reasons.append(f"mom {r['mom']:.4f} < {args.min_momentum_sum:.4f}")
        if r["vol_mult"] < args.min_vol_surge_mult: reasons.append(f"volx {r['vol_mult']:.2f} < {args.min_vol_surge_mult:.2f}")
        if breadth < args.min_breadth: reasons.append(f"breadth {breadth:.2f} < {args.min_breadth:.2f}")
        if reasons: log(f"[filter] {r['symbol']} -> SKIP final: " + "; ".join(reasons), args.logfile)
        else: final_valids.append(r)

    final_valids.sort(key=lambda x: x["mom"], reverse=True)
    chosen = final_valids[: int(args.top_n)]
    log(f"[SELECT] Final candidates ({len(chosen)}): {[r['symbol'] for r in chosen]}", args.logfile)
    return chosen

# -------- universe ----------
def build_universe(args) -> List[str]:
    out = []
    if args.symbols:
        requested = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
        for s in requested:
            try:
                args.ccxt_fetcher.resolve_symbol(s)  # raises if no swap
                out.append(s)
            except Exception as e:
                log(f"[filter] {s} -> SKIP (no USDT swap): {e}", args.logfile)
    else:
        for m in args.ccxt_fetcher.markets.values():
            try:
                if m.get("swap") and m.get("quote") == "USDT":
                    base = m.get("base")
                    if base: out.append(f"{base}-USDT")
                elif args.debug_universe:
                    base = m.get("base"); quote = m.get("quote"); swap = m.get("swap")
                    why = []
                    if not swap: why.append("not swap")
                    if quote != "USDT": why.append(f"quote={quote}")
                    if not base: why.append("no base")
                    log(f"[universe-skip] {m.get('symbol','?')} -> {', '.join(why)}", args.logfile)
            except Exception:
                pass
    out = sorted(set(out))
    if args.max_universe > 0: out = out[: int(args.max_universe)]
    log(f"[universe] size={len(out)} (USDT swaps)", args.logfile)
    return out

# -------- CLI / main ----------
def build_arg_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=str, default="cs_rs_c2_v1.yaml")
    p.add_argument("--env-file", type=str, default="")
    p.add_argument("--source", type=str, default="ccxt")
    p.add_argument("--ccxt-exchange", type=str, default="bingx")
    p.add_argument("--ccxt-symbol-format", type=str, default="usdtm")
    p.add_argument("--force-start-always", action="store_true", default=False)
    p.add_argument("--reset-state", action="store_true", default=False)
    p.add_argument("--debug", action="store_true", default=False)
    p.add_argument("--debug-universe", action="store_true", default=False)
    p.add_argument("--symbols", type=str, default="")
    p.add_argument("--quote", type=str, default="USDT")
    p.add_argument("--max-universe", type=int, default=550)
    p.add_argument("--top-n", type=int, default=4)
    return p

def main():
    args = build_arg_parser().parse_args()

    # Banner to confirm the exact file being executed
    try:
        import inspect, hashlib
        src = inspect.getsourcefile(lambda: None) or __file__
        with open(src, "rb") as f: sha = hashlib.sha1(f.read()).hexdigest()[:10]
        log(f"[build] file={src} sha={sha} version=C2-fixed r4", "")
    except Exception:
        pass

    load_env_from_file(args.env_file)
    api_key = os.environ.get("BINGX_KEY", ""); api_sec = os.environ.get("BINGX_SECRET", "")
    log(f'API: key="{mask(api_key)}", secret="{mask(api_sec)}"', "")

    cfg = {}
    if args.config and os.path.exists(args.config):
        if yaml is None: raise RuntimeError("Please install pyyaml")
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        log(f"[yaml] not found: {args.config}", "")

    # Defaults from your spec
    defaults = {
        "source": "ccxt",
        "ccxt_exchange": "bingx",
        "ccxt_symbol_format": "usdtm",
        "quote": "USDT",
        "timeframe": "1h",
        "limit_klines": 180,
        "max_universe": 550,
        "top_n": 4,
        "min_qv_24h": 200000,
        "min_qv_1h": 10000,
        "min_atr_ratio": 0.022,
        "min_momentum_sum": 0.12,
        "min_vol_surge_mult": 1.25,
        "min_breadth": 0.60,
        "sl_atr_mult": 1.4,
        "tp_atr_mult": 2.6,
        "max_hold_hours": 96,
        "max_mae_atr_mult": 1.6,
        "mom_flip_thresh": 0.02,
        "trail_start_atr": 1.2,
        "trail_dist_atr": 1.0,
        "position_notional": 20.0,
        "max_notional_frac": 0.5,
        "initial_equity": 200.0,
        "slippage_per_side": 0.0003,
        "fee_rate": 0.001,
        "funding_rate_hour": 0.00002,
        "logfile": "c2_bot.log",
        "trades_csv": "c2_trades.csv",
        "state_path": "c2_state.json",
        "papertrade": True,
        "force_start_always": True,
        "reset_state": True,
        "debug": True
    }
    # overlay YAML
    for k, v in (cfg or {}).items():
        defaults[k] = v
    # bind to args
    for k, v in defaults.items():
        setattr(args, k, v)
    log("[yaml] applied: " + str(sorted(list(defaults.keys()))), getattr(args, "logfile", ""))

    # Ensure CCXT available
    if ccxt is None:
        log("[fatal] ccxt is not installed. Please: pip install 'ccxt<5'", getattr(args, "logfile", ""))
        return

    # CCXT fetcher
    args.ccxt_fetcher = CCXTFetcher(exchange=args.ccxt_exchange, symbol_format=args.ccxt_symbol_format,
                                    debug=args.debug, logfile=getattr(args, "logfile", ""))

    # Build universe
    uni = build_universe(args)
    if args.symbols:
        log(f"[dbg] symbols from args: {uni}", args.logfile)

    # Select & (placeholder) open/close
    cands = select_candidates(uni, args)
    log(f"[open] opened={0}", args.logfile)
    log(f"[close] closed={0}", args.logfile)

if __name__ == "__main__":
    main()
