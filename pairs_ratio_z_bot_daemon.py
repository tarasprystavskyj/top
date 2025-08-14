#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pairs_ratio_z_bot_daemon_v2_oneshot.py
One-shot entries (max 1 per bar), max 1 open position, test notional 2.2$.
"""
import os, sys, time, math, random, argparse, json, signal
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
from filelock import FileLock
import pandas as pd
import numpy as np

try:
    import yaml
except Exception:
    yaml = None
try:
    import ccxt
except Exception:
    ccxt = None

RUN = True
API_LOCK = FileLock("/tmp/bingx_api.lock")
RATE_MS   = int(os.getenv("BINGX_RATE_MS", "350"))
JITTER_MS = int(os.getenv("BINGX_JITTER_MS", "150"))

def on_sig(sig, frame):
    global RUN
    RUN = False
for s in (signal.SIGINT, signal.SIGTERM):
    try: signal.signal(s, on_sig)
    except Exception: pass

def now_utc() -> str:
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

def _cloudflareish(msg: str) -> bool:
    s = (msg or "").lower()
    return ("cloudflare" in s) or ("cf-error" in s) or ("</html>" in s) or ("ddos" in s) or (" 5" == s[:2])

def polite_call(fn, *a, **kw):
    delay_ms = kw.pop("_delay_ms", RATE_MS)
    for attempt in range(7):
        try:
            with API_LOCK:
                out = fn(*a, **kw)
                time.sleep((delay_ms + random.randint(0, JITTER_MS)) / 1000.0)
                return out
        except Exception as e:
            if attempt >= 6: raise
            msg = str(e)
            if _cloudflareish(msg) or "unavailable" in msg or " 5" in msg[:2]:
                sleep_s = min(120, 6 * (attempt + 1))
            else:
                sleep_s = min(45, 2 ** attempt)
            print(f"[throttle] retry {attempt+1} after {sleep_s}s: {msg}")
            time.sleep(sleep_s)

class CCXTFetcher:
    def __init__(self, exchange="bingx", symbol_format="usdtm", debug=False, logfile=""):
        if ccxt is None:
            raise RuntimeError("ccxt is not installed. Try: pip install 'ccxt<5'")
        self.debug = debug; self.logfile = logfile
        if not hasattr(ccxt, exchange): raise RuntimeError(f"ccxt.{exchange} not found")
        self.ex = getattr(ccxt, exchange)({"enableRateLimit": True, "timeout": 20000})
        self.symbol_format = symbol_format
        try:
            self.markets = polite_call(self.ex.load_markets)
        except Exception as e:
            self.markets = {}; log(f"[ccxt load_markets] {e}", self.logfile)
        self._swap_by_base = {}
        for m in self.markets.values():
            try:
                if m.get("swap") and m.get("quote") == "USDT":
                    base = m.get("base")
                    if base: self._swap_by_base[base] = m["symbol"]
            except Exception:
                pass

    def resolve_symbol(self, s: str) -> str:
        base, quote = s.split("-")
        if self.symbol_format == "usdtm":
            sym = self._swap_by_base.get(base)
            if sym: return sym
            cand = f"{base}/USDT:USDT"
            if cand in self.markets: return cand
            raise ccxt.BadSymbol(f"No USDT-margined swap for {base}")
        return f"{base}/{quote}"

    def fetch_ticker_price(self, symbol: str) -> Optional[float]:
        try:
            ccxt_sym = self.resolve_symbol(symbol)
            t = polite_call(self.ex.fetch_ticker, ccxt_sym)
            for k in ("last","close","bid","ask"):
                if t.get(k) is not None: return float(t[k])
        except Exception as e:
            log(f"[ccxt price] {symbol}: {e}", self.logfile)
        return None

    def fetch_ohlcv_df(self, symbol: str, timeframe="1h", limit=150) -> Optional[pd.DataFrame]:
        try:
            ccxt_sym = self.resolve_symbol(symbol)
            data = polite_call(self.ex.fetch_ohlcv, ccxt_sym, timeframe=timeframe, limit=limit)
        except Exception as e:
            if _cloudflareish(str(e)):
                log(f"[ccxt ohlcv] {symbol}: Cloudflare/WAF, sleeping 60s & retry", self.logfile)
                time.sleep(60)
                try:
                    ccxt_sym = self.resolve_symbol(symbol)
                    data = polite_call(self.ex.fetch_ohlcv, ccxt_sym, timeframe=timeframe, limit=limit, _delay_ms=RATE_MS+200)
                except Exception as e2:
                    log(f"[ccxt ohlcv] {symbol}: {e2}", self.logfile); return None
            else:
                log(f"[ccxt ohlcv] {symbol}: {e}", self.logfile); return None
        if not data: return None
        df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("ts", inplace=True)
        return df

def compute_bar_features(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    v = df["volume"].astype(float).values
    n = len(df)
    if n < 30: return pd.DataFrame(index=df.index)
    tr = [h[0]-l[0]]
    for i in range(1, n):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    atr = pd.Series(tr).rolling(14, min_periods=1).mean().values
    atr_ratio = atr / np.maximum(c, 1e-12)
    qv_bar = c * v
    qv_24h = pd.Series(qv_bar).rolling(24, min_periods=1).sum().values
    avg_24 = pd.Series(qv_bar).rolling(24, min_periods=1).mean().values
    vol_surge_mult = np.where(avg_24>0, qv_bar/avg_24, 0.0)
    out = pd.DataFrame({
        "open":df["open"].values, "high":h, "low":l, "close":c,
        "atr_ratio":atr_ratio, "quote_volume":qv_bar, "qv_24h":qv_24h,
        "vol_surge_mult":vol_surge_mult
    }, index=df.index)
    return out

def zscore(series: pd.Series, window: int) -> pd.Series:
    mu = series.rolling(window, min_periods=window).mean()
    std = series.rolling(window, min_periods=window).std(ddof=1)
    return (series - mu) / std

def ratio_z_for_alt(alt_df: pd.DataFrame, btc_df: pd.DataFrame, window: int):
    if alt_df is None or btc_df is None or len(alt_df)==0 or len(btc_df)==0: return None
    a = alt_df["close"].astype(float)
    b = btc_df["close"].astype(float)
    idx = a.index.intersection(b.index)
    if len(idx) < window: return None
    ratio = (a.loc[idx] / b.loc[idx]).rename("ratio")
    z = zscore(ratio, window)
    z_last = z.iloc[-1]
    return float(ratio.iloc[-1]), (float(z_last) if pd.notna(z_last) else None), idx[-1]

def load_state(path: str) -> dict:
    if not path or not os.path.exists(path): return {}
    try: return json.loads(open(path,"r",encoding="utf-8").read())
    except Exception: return {}

def save_state(path: str, st: dict):
    try:
        tmp = path + ".tmp"
        with open(tmp,"w",encoding="utf-8") as f:
            json.dump(st,f,ensure_ascii=False,indent=2,default=str)
        os.replace(tmp,path)
    except Exception as e:
        log(f"[state] save failed: {e}")

def append_trade(csv_path: str, row: dict):
    hdr = not os.path.exists(csv_path)
    try:
        import csv
        with open(csv_path,"a",newline="",encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if hdr: w.writeheader()
            w.writerow(row)
    except Exception as e:
        log(f"[trades] write failed: {e}", None)

def round_to_step(value: float, step: float) -> float:
    if step is None or step <= 0:
        return float(round(value, 6))
    return math.floor(value / step) * step

def qty_for_notional(market: dict, notional: float, price: float) -> float:
    amt = max(0.0, notional / max(price, 1e-12))
    step = None
    try:
        step = market.get("precision", {}).get("amount", None)
        step = market.get("limits", {}).get("amount", {}).get("step", step)
    except Exception: pass
    q = round_to_step(amt, step if step else 0.0)
    min_amt = market.get("limits", {}).get("amount", {}).get("min", 0.0) or 0.0
    if q < min_amt: q = min_amt
    return float(q)

def build_universe(fetcher, symbol_format: str, max_universe: int, logfile: str) -> list:
    out = []
    for m in fetcher.markets.values():
        try:
            if m.get("swap") and m.get("quote") == "USDT":
                base = m.get("base")
                if base: out.append(f"{base}-USDT")
        except Exception: pass
    out = sorted(set(out))
    if max_universe > 0: out = out[: int(max_universe)]
    log(f"[universe] size={len(out)} (USDT swaps)", logfile)
    return out

def select_candidates(universe: list, args, fetcher, btc_symbol: str) -> list:
    recs = []; min_qv24 = float(args.min_qv_24h); min_qv1 = float(args.min_qv_1h)
    for s in universe:
        if s == btc_symbol: continue
        alt = fetcher.fetch_ohlcv_df(s, timeframe=args.timeframe, limit=args.limit_klines)
        if alt is None: 
            log(f"[filter] {s} -> SKIP df=None", args.logfile); continue
        btc = fetcher.fetch_ohlcv_df(btc_symbol, timeframe=args.timeframe, limit=args.limit_klines)
        if btc is None:
            log(f"[fatal] BTC df=None, abort selection", args.logfile); return []
        feat = compute_bar_features(alt)
        if len(feat) < max(40, int(args.z_window)+1):
            log(f"[filter] {s} -> SKIP short hist ({len(feat)})", args.logfile); continue
        r = feat.iloc[-1]
        qv1  = float(r.get("quote_volume", 0.0) or 0.0)
        qv24 = float(r.get("qv_24h", 0.0) or 0.0)
        if not (qv24 >= min_qv24 and qv1 >= min_qv1):
            log(f"[filter] {s} -> SKIP qv: qv1h={qv1:.0f} <{min_qv1:.0f} or qv24h={qv24:.0f} <{min_qv24:.0f}", args.logfile); 
            continue
        atr = float(r.get("atr_ratio", 0.0) or 0.0)
        if atr < float(args.min_atr_ratio) or atr > float(args.max_atr_ratio):
            log(f"[filter] {s} -> SKIP atr_ratio={atr:.4f} not in [{args.min_atr_ratio:.4f},{args.max_atr_ratio:.4f}]", args.logfile); 
            continue
        rz = ratio_z_for_alt(alt, btc, int(args.z_window))
        if not rz: 
            log(f"[filter] {s} -> SKIP ratio/z unavailable", args.logfile); continue
        ratio_last, z_last, ts = rz
        if z_last is None or not np.isfinite(z_last): 
            log(f"[filter] {s} -> SKIP z NaN", args.logfile); continue
        side = None
        if z_last >= float(args.z_entry): side = "SHORT"
        elif z_last <= -float(args.z_entry): side = "LONG"
        if side is None: continue
        px = fetcher.fetch_ticker_price(s)
        if not px: 
            log(f"[filter] {s} -> SKIP no ticker price", args.logfile); continue
        recs.append({"symbol": s, "price": float(px), "atr": atr, "z": float(z_last), "absz": abs(float(z_last)), "side": side})
    recs.sort(key=lambda x: x["absz"], reverse=True)
    return recs[: int(args.top_n)]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str, default="pairs_ratio_z_live_v2_oneshot.yaml")
    ap.add_argument("--env-file", type=str, default="")
    ap.add_argument("--mode", choices=["paper","live"], default=None, help="force run mode")
    ap.add_argument("--papertrade", type=str, choices=["true","false"], default=None, help="legacy override (string)")
    ap.add_argument("--ccxt-exchange", type=str, default="bingx")
    ap.add_argument("--ccxt-symbol-format", type=str, default="usdtm")
    ap.add_argument("--symbols", type=str, default="", help="optional whitelist e.g. 'SOL-USDT,ARB-USDT'")
    ap.add_argument("--timeframe", type=str, default="1h")
    ap.add_argument("--limit_klines", type=int, default=180)
    ap.add_argument("--top-n", type=int, default=1)
    ap.add_argument("--poll-sec", type=int, default=15)
    ap.add_argument("--bar-delay-sec", type=int, default=10)
    ap.add_argument("--place-brackets", type=str, choices=["true","false"], default=None)
    ap.add_argument("--debug", action="store_true", default=False)
    args_cli = ap.parse_args()

    load_env_from_file(args_cli.env_file)
    api_key = os.environ.get("BINGX_KEY", ""); api_sec = os.environ.get("BINGX_SECRET", "")
    log(f'API: key="{mask(api_key)}", secret="{mask(api_sec)}"')

    cfg = {}
    if args_cli.config and os.path.exists(args_cli.config):
        if yaml is None: raise RuntimeError("Please install pyyaml")
        with open(args_cli.config, "r", encoding="utf-8") as f: cfg = yaml.safe_load(f) or {}

    class Args: pass
    args = Args()
    defaults = {
        "btc_symbol":"BTC-USDT",
        "z_window":30, "z_entry":2.0, "z_exit":0.5,
        "min_qv_24h":200000, "min_qv_1h":10000,
        "min_atr_ratio":0.028, "max_atr_ratio":0.15,
        "sl_atr_mult":1.0, "tp_atr_mult":2.0, "max_hold_hours":96, "max_mae_atr_mult":1.5,
        "dynamic_sizing": False, "base_notional":2.2, "atr_target":0.03, "size_min_scale":1.0, "size_max_scale":1.0,
        "initial_equity":200.0, "position_notional":2.2, "max_notional_frac":0.02,
        "fee_rate":0.001, "funding_rate_hour":0.00002, "slippage_per_side":0.0003, "tick_pct":0.0001,
        "logfile":"pairs_ratio_bot_v2_oneshot.log", "trades_csv":"pairs_ratio_trades_v2_oneshot.csv", "state_path":"pairs_ratio_state_v2_oneshot.json",
        "ccxt_exchange": args_cli.ccxt_exchange, "ccxt_symbol_format": args_cli.ccxt_symbol_format,
        "symbols": args_cli.symbols, "timeframe": args_cli.timeframe, "limit_klines": args_cli.limit_klines,
        "top_n": 1, "poll_sec": args_cli.poll_sec, "bar_delay_sec": args_cli.bar_delay_sec,
        "place_brackets": True, "papertrade": True, "debug": args_cli.debug,
        "max_open_positions_total": 1, "max_new_positions_per_bar": 1
    }
    merged = defaults.copy()
    merged.update({k:v for k,v in cfg.items() if v is not None})
    if args_cli.mode == "live": merged["papertrade"] = False
    elif args_cli.mode == "paper": merged["papertrade"] = True
    elif args_cli.papertrade is not None:
        merged["papertrade"] = (args_cli.papertrade.lower()=="true")
    if args_cli.place_brackets is not None:
        merged["place_brackets"] = (args_cli.place_brackets.lower()=="true")
    for k,v in merged.items(): setattr(args, k, v)

    if ccxt is None:
        log("[fatal] ccxt is not installed. Please: pip install 'ccxt<5'"); return
    fetcher = CCXTFetcher(exchange=args.ccxt_exchange, symbol_format=args.ccxt_symbol_format, debug=args.debug, logfile=args.logfile)
    if not args.papertrade and os.environ.get("BINGX_KEY") and os.environ.get("BINGX_SECRET"):
        fetcher.ex.apiKey = os.environ["BINGX_KEY"]
        fetcher.ex.secret = os.environ["BINGX_SECRET"]

    st = load_state(args.state_path)
    if "positions" not in st: st["positions"] = {}

    if args.symbols:
        req = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
        universe = []
        for s in req:
            try: fetcher.resolve_symbol(s); universe.append(s)
            except Exception as e: log(f"[whitelist] {s} -> SKIP: {e}", args.logfile)
    else:
        universe = build_universe(fetcher, args.ccxt_symbol_format, 550, args.logfile)

    def seconds_per_bar(tf: str) -> int:
        return {"1h":3600,"2h":7200,"4h":14400,"30m":1800}.get(tf, 3600)
    bar_sec = seconds_per_bar(args.timeframe)
    last_bar_close = None
    log(f"[daemon] started in {'PAPER' if args.papertrade else 'LIVE'} mode. One-shot entries.", args.logfile)

    while RUN:
        try:
            now = datetime.utcnow().replace(tzinfo=timezone.utc)
            epoch = datetime(1970,1,1,tzinfo=timezone.utc)
            since = int((now - epoch).total_seconds())
            curr_close = (since // bar_sec) * bar_sec
            just_closed = (last_bar_close is None or curr_close > last_bar_close)

            # manage exits
            to_close = []
            for sym, pos in list(st["positions"].items()):
                last = fetcher.fetch_ticker_price(sym)
                if last is None: continue
                entry = pos["entry_price"]; atr_e = pos["atr_entry"]
                alt = fetcher.fetch_ohlcv_df(sym, timeframe=args.timeframe, limit=args.limit_klines)
                btc = fetcher.fetch_ohlcv_df(args.btc_symbol, timeframe=args.timeframe, limit=args.limit_klines)
                exit_by_z = False
                if alt is not None and btc is not None:
                    rz = ratio_z_for_alt(alt, btc, int(args.z_window))
                    if rz:
                        _, z_last, _ = rz
                        if z_last is not None and abs(z_last) <= float(args.z_exit):
                            exit_by_z = True
                sl = entry * (1 - args.sl_atr_mult * atr_e) if pos["side"]=="LONG" else entry * (1 + args.sl_atr_mult * atr_e)
                tp = entry * (1 + args.tp_atr_mult * atr_e) if pos["side"]=="LONG" else entry * (1 - args.tp_atr_mult * atr_e)
                reason=None; exit_px=None
                if exit_by_z: reason="Z_EXIT"; exit_px=last
                if reason is None:
                    if pos["side"]=="LONG" and last <= sl: reason="SL"; exit_px=sl
                    elif pos["side"]=="SHORT" and last >= sl: reason="SL"; exit_px=sl
                    elif pos["side"]=="LONG" and last >= tp: reason="TP"; exit_px=tp
                    elif pos["side"]=="SHORT" and last <= tp: reason="TP"; exit_px=tp
                if reason is None:
                    try:
                        et = datetime.fromisoformat(pos["entry_time"].replace("Z","+00:00"))
                    except Exception:
                        et = now
                    held_h = (now - et).total_seconds()/3600.0
                    if held_h >= args.max_hold_hours:
                        reason="TIME"; exit_px=last
                if reason:
                    pnl_like = (exit_px - pos["entry_price"]) / max(pos["entry_price"],1e-12)
                    pnl_like = pnl_like if pos["side"]=="LONG" else -pnl_like
                    pnl_notional = pos["notional"] * (pnl_like - 2*args.fee_rate - 2*args.slippage_per_side)
                    log(f"[close] {sym} {reason} @ {exit_px:.6g} pnl≈{pnl_notional:.2f} (side {pos['side']})", args.logfile)
                    if not args.papertrade:
                        try:
                            ccxt_sym = fetcher.resolve_symbol(sym)
                            mkt = fetcher.markets.get(ccxt_sym, {})
                            qty = qty_for_notional(mkt, pos["notional"], max(last,1e-12))
                            side_close = "sell" if pos["side"]=="LONG" else "buy"
                            params = {"reduceOnly": True}
                            polite_call(fetcher.ex.create_order, ccxt_sym, "market", side_close, qty, None, params)
                        except Exception as e:
                            log(f"[live close] {sym}: {e}", args.logfile)
                    append_trade(args.trades_csv, {
                        "ts": now_utc(), "symbol": sym, "side": "CLOSE", "reason": reason,
                        "entry_price": pos["entry_price"], "exit_price": exit_px, "notional": pos["notional"]
                    })
                    del st["positions"][sym]
                    save_state(args.state_path, st)

            # entries one-shot
            if just_closed and ((since - curr_close) >= args.bar_delay_sec):
                last_bar_close = curr_close
                log("[bar] closed -> select & one-shot enter", args.logfile)

                if len(st["positions"]) >= int(args.max_open_positions_total):
                    log("[cap] positions at max; skip entries this bar", args.logfile)
                else:
                    gross_open = sum(p["notional"] for p in st["positions"].values())
                    max_gross = args.initial_equity * args.max_notional_frac
                    cap_left = max(0.0, max_gross - gross_open)
                    if cap_left < args.position_notional:
                        log(f"[cap] not enough cap_left={cap_left:.2f} for notional={args.position_notional:.2f}", args.logfile)
                    else:
                        if args.symbols:
                            universe = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
                        else:
                            universe = build_universe(fetcher, args.ccxt_symbol_format, 550, args.logfile)

                        cands = select_candidates(universe, args, fetcher, args.btc_symbol)
                        if cands:
                            r = cands[0]
                            sym, side, atr_e = r["symbol"], r["side"], float(r["atr"])
                            if sym not in st["positions"]:
                                entry_px = fetcher.fetch_ticker_price(sym) or r["price"]
                                if entry_px:
                                    notional = float(args.position_notional)
                                    if bool(args.dynamic_sizing):
                                        scale = float(args.atr_target) / max(atr_e, 1e-6)
                                        scale = max(float(args.size_min_scale), min(float(args.size_max_scale), scale))
                                        notional = float(args.base_notional) * scale
                                    sl = entry_px * (1 - args.sl_atr_mult * atr_e) if side=="LONG" else entry_px * (1 + args.sl_atr_mult * atr_e)
                                    tp = entry_px * (1 + args.tp_atr_mult * atr_e) if side=="LONG" else entry_px * (1 - args.tp_atr_mult * atr_e)

                                    if not args.papertrade and args.place_brackets:
                                        try:
                                            ccxt_sym = fetcher.resolve_symbol(sym)
                                            mkt = fetcher.markets.get(ccxt_sym, {})
                                            qty = qty_for_notional(mkt, notional, max(entry_px,1e-12))
                                            side_open = "sell" if side.upper()=="SHORT" else "buy"
                                            params = {"reduceOnly": False}
                                            od = polite_call(fetcher.ex.create_order, ccxt_sym, "market", side_open, qty, None, params)
                                            log(f"[live OPEN] {sym} {side_open.upper()} qty={qty} id={od.get('id')}", args.logfile)
                                        except Exception as e:
                                            log(f"[live open] {sym}: {e}", args.logfile)
                                    else:
                                        log(f"[paper OPEN] {sym} {side} entry≈{entry_px:.6g} notional={notional:.2f}", args.logfile)

                                    st["positions"][sym] = {
                                        "entry_time": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat().replace("+00:00","Z"),
                                        "entry_price": float(entry_px), "atr_entry": float(atr_e),
                                        "notional": float(notional), "side": side
                                    }
                                    append_trade(args.trades_csv, {
                                        "ts": now_utc(), "symbol": sym, "side": "OPEN", "dir": side,
                                        "entry_price": entry_px, "notional": notional, "sl": sl, "tp": tp,
                                        "z": float(r["z"])
                                    })
                                    save_state(args.state_path, st)
                                else:
                                    log(f"[skip] {sym} no entry price", args.logfile)
                        # one-shot: do NOT iterate more than first candidate or retry within same bar

            time.sleep(max(1, int(args.poll_sec)))
        except Exception as e:
            log(f"[daemon err] {e}", args.logfile)
            time.sleep(2)

    log("[daemon] stopping...", args.logfile)

if __name__ == "__main__":
    main()
