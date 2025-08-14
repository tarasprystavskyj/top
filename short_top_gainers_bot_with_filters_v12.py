#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, math, time, json, argparse, re
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import yaml
import ccxt
import pandas as pd
import numpy as np

# ---------- utils ----------
def now_utc() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg: str, logfile: str = ""):
    line = f"[{now_utc()}] {msg}"
    print(line, flush=True)
    if logfile:
        try:
            with open(logfile, "a", encoding="utf-8") as f: f.write(line + "\n")
        except Exception:
            pass

def mask(val: str) -> str:
    if not val: return "<empty>"
    return val[:3] + "..." + val[-3:] if len(val) >= 7 else "***"

def load_env_from_file(path: str):
    if not path or not os.path.exists(path): return
    for ln in open(path, "r", encoding="utf-8"):
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln: continue
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

RATE_MS   = int(os.getenv("BINGX_RATE_MS", "350"))
def sleep_ms(ms:int): time.sleep(max(0, ms)/1000.0)

# ---------- indicators ----------
def rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(up, index=close.index).ewm(alpha=1/period, adjust=False).mean()
    roll_down = pd.Series(down, index=close.index).ewm(alpha=1/period, adjust=False).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)

def stoch_k(df: pd.DataFrame, length: int = 14, smooth: int = 3) -> pd.Series:
    low_min = df['low'].rolling(length, min_periods=1).min()
    high_max = df['high'].rolling(length, min_periods=1).max()
    denom = (high_max - low_min).replace(0, np.nan)
    k = 100 * (df['close'] - low_min) / denom
    return k.rolling(smooth, min_periods=1).mean().fillna(50.0)

def mfi_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tp = (df['high'] + df['low'] + df['close']) / 3.0
    rmf = tp * df['volume']
    sign = np.sign(tp.diff().fillna(0))
    pos = (rmf * (sign >= 0)).rolling(period, min_periods=1).sum()
    neg = (rmf * (sign <  0)).rolling(period, min_periods=1).sum().replace(0, np.nan)
    mfi = 100 - (100 / (1 + (pos / neg)))
    return mfi.fillna(50.0)

def overbought_index(rsi: float, stoch: float, mfi: float, close: float, high: float, w: Dict[str,float]) -> float:
    r = max(0.0, min(100.0, rsi))
    s = max(0.0, min(100.0, stoch))
    m = max(0.0, min(100.0, mfi))
    hcp = 100.0 * ((close / max(high, 1e-12)) if high > 0 else 0.0)
    return w.get('w_rsi',0)*r + w.get('w_stoch',0)*s + w.get('w_mfi',0)*m + w.get('w_hcp',0)*hcp

def compute_feats(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) < 30: return pd.DataFrame()
    c = df["close"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    v = df["volume"].astype(float).values
    n = len(df)
    tr = np.zeros(n); tr[0] = h[0]-l[0]
    for i in range(1,n):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    atr = pd.Series(tr).rolling(14, min_periods=1).mean().values
    atr_ratio = atr / np.maximum(c, 1e-12)
    def pct(a,b): return (c[b]-c[a]) / max(c[a],1e-12)
    dp6 = np.array([pct(max(0,i-6), i) for i in range(n)])
    dp12= np.array([pct(max(0,i-12),i) for i in range(n)])
    qv_bar = c * v
    qv_24h = pd.Series(qv_bar).rolling(24, min_periods=1).sum().values
    avg_24 = pd.Series(qv_bar).rolling(24, min_periods=1).mean().values
    volx = np.where(avg_24>0, qv_bar/avg_24, 0.0)
    rsi = rsi_series(df["close"])
    st_k = stoch_k(df)
    mfi = mfi_series(df)
    out = pd.DataFrame({
        "open":df["open"].values, "high":h, "low":l, "close":c,
        "atr_ratio":atr_ratio, "dp6h":dp6, "dp12h":dp12, "mom":dp6+dp12,
        "qv_24h":qv_24h, "quote_volume":qv_bar, "vol_surge_mult":volx,
        "rsi":rsi.values, "stoch_k":st_k.values, "mfi":mfi.values,
    }, index=df.index)
    return out

# ---------- CCXT ----------
class CCXTFetcher:
    def __init__(self, exchange="bingx", symbol_format="usdtm", debug=False, logfile=""):
        self.debug = debug; self.logfile = logfile
        self.ex = getattr(ccxt, exchange)({"enableRateLimit": True, "timeout": 20000})
        self.symbol_format = symbol_format
        try:
            self.markets = self.ex.load_markets()
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
            t = self.ex.fetch_ticker(ccxt_sym)
            for k in ("last","close","bid","ask"):
                if t.get(k) is not None: return float(t[k])
        except Exception as e:
            log(f"[ccxt price] {symbol}: {e}", self.logfile)
        return None

    def fetch_ohlcv_df(self, symbol: str, timeframe="1h", limit=150) -> Optional[pd.DataFrame]:
        try:
            ccxt_sym = self.resolve_symbol(symbol)
            data = self.ex.fetch_ohlcv(ccxt_sym, timeframe=timeframe, limit=limit)
        except Exception as e:
            log(f"[ccxt ohlcv] {symbol}: {e}", self.logfile); return None
        if not data: return None
        df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("ts", inplace=True)
        return df

# ---------- qty rules ----------
def infer_qty_rules(market: dict):
    step = None; min_qty = None
    try:
        step = market.get("limits",{}).get("amount",{}).get("step") or market.get("precision",{}).get("amount")
    except Exception: pass
    try:
        info = market.get("info") or {}
        if isinstance(info, dict):
            if info.get("minQty") is not None: min_qty = float(info["minQty"])
            if not step and info.get("stepSize") is not None: step = float(info["stepSize"])
    except Exception: pass
    return (float(step) if step else None, float(min_qty) if min_qty else None)

def round_to_step(value: float, step: Optional[float]) -> float:
    if not step or step <= 0: return float(round(value, 8))
    return math.floor(value/step)*step

def qty_for_notional(market: dict, notional: float, price: float):
    step, min_qty = infer_qty_rules(market)
    est = max(0.0, notional/max(price,1e-12))
    q = round_to_step(est, step)
    if min_qty: q = max(q, min_qty)
    min_notional_required = (min_qty or 0.0) * price
    return float(q), float(min_notional_required), step, min_qty

# ---------- IO ----------
def load_state(path: str) -> dict:
    if not path or not os.path.exists(path): return {}
    try: return json.loads(open(path,"r",encoding="utf-8").read())
    except Exception: return {}

def save_state(path: str, st: dict):
    try:
        tmp = path + ".tmp"
        with open(tmp,"w",encoding="utf-8") as f: json.dump(st,f,ensure_ascii=False,indent=2,default=str)
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
        log(f"[trades] write failed: {e}")

# ---------- selection ----------
def build_universe(args) -> List[str]:
    if args.symbols:
        requested = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
        out = []
        for s in requested:
            try:
                args.ccxt_fetcher.resolve_symbol(s); out.append(s)
            except Exception as e:
                log(f"[filter] {s} -> SKIP (no USDT swap): {e}", args.logfile)
        return out
    out = []
    for m in args.ccxt_fetcher.markets.values():
        try:
            if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True):
                base = m.get("base")
                if base: out.append(f"{base}-USDT")
        except Exception: pass
    out = sorted(set(out))
    if args.max_universe > 0: out = out[: int(args.max_universe)]
    log(f"[universe] size={len(out)} (USDT swaps)", args.logfile); return out

def select_candidates(universe: List[str], args) -> List[Dict[str,Any]]:
    recs = []
    for s in universe:
        df = args.ccxt_fetcher.fetch_ohlcv_df(s, timeframe=args.timeframe, limit=args.limit_klines)
        if df is None:
            log(f"[filter] {s} -> SKIP df=None", args.logfile); continue
        feats = compute_feats(df)
        if len(feats) < 30:
            log(f"[filter] {s} -> SKIP too few feats={len(feats)}", args.logfile); continue
        r = feats.iloc[-1]
        qv1  = float(r.get("quote_volume", 0.0) or 0.0)
        qv24 = float(r.get("qv_24h", 0.0) or 0.0)
        if not (qv24 >= args.min_qv_24h and qv1 >= args.min_qv_1h):
            log(f"[filter] {s} -> SKIP qv: qv1h={qv1:.0f} (min {args.min_qv_1h:.0f}), qv24h={qv24:.0f} (min {args.min_qv_24h:.0f})", args.logfile); continue
        atr  = float(r.get("atr_ratio", 0.0) or 0.0)
        dp6  = float(r.get("dp6h", 0.0) or 0.0)
        dp12 = float(r.get("dp12h", 0.0) or 0.0)
        mom  = dp6 + dp12
        volx = float(r.get("vol_surge_mult", 0.0) or 0.0)
        rsi  = float(r.get("rsi", 50.0) or 50.0)
        st_k = float(r.get("stoch_k", 50.0) or 50.0)
        mfi  = float(r.get("mfi", 50.0) or 50.0)
        px = args.ccxt_fetcher.fetch_ticker_price(s)
        if not px:
            log(f"[filter] {s} -> SKIP no ticker price", args.logfile); continue
        w = {"w_rsi":args.w_rsi, "w_stoch":args.w_stoch, "w_mfi":args.w_mfi, "w_hcp":args.w_hcp}
        ob = overbought_index(rsi, st_k, mfi, float(r.get("close",px)), float(r.get("high",px)), w)
        log(f"[select] {s} ok | px={px:.6g} r%={atr:.4f} dp6h={dp6:.4f} dp12h={dp12:.4f} mom={mom:.4f} volx={volx:.2f} qv1h={qv1:.0f} qv24h={qv24:.0f}", args.logfile)
        recs.append({"symbol": s, "price": float(px), "atr": atr, "mom": mom, "volx": volx,
                     "qv1": qv1, "qv24": qv24, "rsi": rsi, "stoch_k": st_k, "mfi": mfi, "ob": ob})
    # ATR window
    atr_pass = []
    for r in recs:
        if r["atr"] < args.min_atr_ratio:
            log(f"[filter] {r['symbol']} -> SKIP r%: {r['atr']:.4f} < min {args.min_atr_ratio:.4f}", args.logfile); continue
        if args.max_atr_ratio > 0 and r["atr"] > args.max_atr_ratio:
            log(f"[filter] {r['symbol']} -> SKIP r%: {r['atr']:.4f} > max {args.max_atr_ratio:.4f}", args.logfile); continue
        atr_pass.append(r)
    total = len(atr_pass); positives = sum(1 for r in atr_pass if r["mom"] > 0)
    breadth = (positives / total) if total>0 else 0.0
    log(f"[breadth] {positives}/{total} = {breadth:.2f} (min {args.min_breadth:.2f})", args.logfile)

    final_valids = []
    for r in atr_pass:
        reasons = []
        if r["mom"] < args.min_momentum_sum: reasons.append(f"mom {r['mom']:.4f} < {args.min_momentum_sum:.4f}")
        if r["volx"] + 1e-6 < args.min_vol_surge_mult: reasons.append(f"volx {r['volx']:.2f} < {args.min_vol_surge_mult:.2f}")
        if breadth < args.min_breadth: reasons.append(f"breadth {breadth:.2f} < {args.min_breadth:.2f}")
        if args.side == "short":
            if args.min_ob > 0 and r["ob"] < args.min_ob: reasons.append(f"ob {r['ob']:.1f} < {args.min_ob:.1f}")
        else:
            # для LONG (якщо колись знадобиться): можна вимагати "oversold"
            pass
        if reasons: log(f"[filter] {r['symbol']} -> SKIP final: " + "; ".join(reasons), args.logfile)
        else: final_valids.append(r)

    final_valids.sort(key=lambda x: x["mom"], reverse=True)
    log(f"[SELECT] Final candidates ({len(final_valids)}): {[r['symbol'] for r in final_valids]}", args.logfile)
    return final_valids[: int(args.top_n)]

# ---------- LIVE orders (ONE attempt; hedge-aware; side-aware) ----------
def create_market_open_single(fetcher: CCXTFetcher, sym: str, *, side: str, notional_cap: float, price: float,
                              position_mode: str) -> Dict[str,Any]:
    """
    side: 'short' => open with SELL (+ positionSide SHORT in hedge),
          'long'  => open with BUY  (+ positionSide LONG  in hedge).
    ONE attempt; retry only for 109400 (positionSide).
    """
    ccxt_sym = fetcher.resolve_symbol(sym)
    mkt = fetcher.markets.get(ccxt_sym, {})
    qty, min_notional_req, step, min_qty = qty_for_notional(mkt, notional_cap, price)

    if min_notional_req > notional_cap + 1e-9:
        return {"ok": False, "skip_reason": f"min_notional {min_notional_req:.4f} > cap {notional_cap:.4f}",
                "qty_rules": {"min_qty":min_qty, "step":step}}

    order_side = "sell" if side == "short" else "buy"
    params = {"reduceOnly": False}
    if position_mode == "hedge":
        params["positionSide"] = "SHORT" if side == "short" else "LONG"

    try:
        od = fetcher.ex.create_order(ccxt_sym, "market", order_side, qty, None, params)
        sleep_ms(RATE_MS)
        return {"ok": True, "order": od, "qty": qty, "params": params}
    except Exception as e:
        msg = str(e)
        m = re.search(r"minimum order amount is\s+([0-9]*\.?[0-9]+)\s*([A-Za-z0-9]+)", msg, re.I)
        if ("80001" in msg) or ("minimum order amount" in msg.lower()):
            req_qty = float(m.group(1)) if m else (min_qty or float("nan"))
            req_notional = req_qty * price if req_qty==req_qty else (min_notional_req or float("nan"))
            return {"ok": False, "skip_reason": f"exchange_min_qty {req_qty} -> notional≈{req_notional:.4f} > cap {notional_cap:.4f}"}
        if ("109400" in msg) or ("PositionSide" in msg):
            # одна спроба з явним positionSide
            if position_mode == "hedge":
                params["positionSide"] = "SHORT" if side == "short" else "LONG"
            try:
                od = fetcher.ex.create_order(ccxt_sym, "market", order_side, qty, None, params)
                sleep_ms(RATE_MS)
                return {"ok": True, "order": od, "qty": qty, "params": params, "retry": True}
            except Exception as e2:
                return {"ok": False, "error": str(e2), "qty": qty, "params": params}
        return {"ok": False, "error": msg, "qty": qty, "params": params}

def place_brackets_single(fetcher: CCXTFetcher, sym: str, *, side: str, qty: float,
                          sl_px: Optional[float], tp_px: Optional[float], position_mode: str) -> Dict[str,Any]:
    """
    Для LONG: скобки = sell reduceOnly.
    Для SHORT: скобки = buy  reduceOnly.
    """
    ccxt_sym = fetcher.resolve_symbol(sym)
    info = {"sl_id": None, "tp_id": None}
    close_side = "buy" if side == "short" else "sell"
    pos_side   = "SHORT" if side == "short" else "LONG"

    if sl_px is not None:
        p = {"reduceOnly": True}
        if position_mode == "hedge": p["positionSide"] = pos_side
        for k in ("stopPrice","triggerPrice","stopLossPrice"): p[k] = sl_px
        try:
            sl_od = fetcher.ex.create_order(ccxt_sym, "market", close_side, qty, None, p)
            info["sl_id"] = sl_od.get("id")
        except Exception as e:
            log(f"[live SL] {sym}: {e}")
        sleep_ms(RATE_MS)

    if tp_px is not None:
        p = {"reduceOnly": True}
        if position_mode == "hedge": p["positionSide"] = pos_side
        for k in ("stopPrice","triggerPrice","takeProfitPrice"): p[k] = tp_px
        try:
            tp_od = fetcher.ex.create_order(ccxt_sym, "market", close_side, qty, None, p)
            info["tp_id"] = tp_od.get("id")
        except Exception as e:
            log(f"[live TP] {sym}: {e}")
        sleep_ms(RATE_MS)

    return info

# ---------- entry ----------
def entry_phase(cands: List[Dict[str,Any]], args):
    opened = []
    st = load_state(args.state_path)
    if "positions" not in st: st["positions"] = {}

    gross_open = sum(p["notional"] for p in st["positions"].values())
    max_gross  = args.initial_equity * args.max_notional_frac
    cap_left   = max(0.0, max_gross - gross_open)

    api_key = os.environ.get("BINGX_KEY",""); api_sec = os.environ.get("BINGX_SECRET","")
    live_cfg = bool(getattr(args,"live",False)) and not getattr(args,"papertrade",False)
    have_keys = bool(api_key) and bool(api_sec)
    live = live_cfg and have_keys
    if live:
        args.ccxt_fetcher.ex.apiKey = api_key
        args.ccxt_fetcher.ex.secret = api_sec

    position_mode = os.getenv("BINGX_POSITION_MODE", args.position_mode).lower()
    for r in cands:
        if cap_left < args.position_notional: break
        sym = r["symbol"]; entry_px = args.ccxt_fetcher.fetch_ticker_price(sym) or r["price"]
        if not entry_px:
            log(f"[entry] {sym} -> SKIP: no price", args.logfile); continue

        ccxt_sym = args.ccxt_fetcher.resolve_symbol(sym)
        mkt = args.ccxt_fetcher.markets.get(ccxt_sym, {})
        _, min_notional_req, _, min_qty = qty_for_notional(mkt, args.position_notional, entry_px)
        if min_notional_req > args.position_notional + 1e-9:
            log(f"[entry] {sym} -> SKIP: min_notional {min_notional_req:.4f} > cap {args.position_notional:.4f} (min_qty={min_qty})", args.logfile)
            continue

        # для SHORT SL вище ціни, TP нижче; але ми задаємо абсолютні ціни (ринкові 'market' з тригерними полями можуть ігноритись біржею;
        # залишено для симетрії з вашою реалізацією)
        if args.side == "short":
            sl_px = entry_px + (args.sl_atr_mult * r["atr"] if args.sl_atr_mult>0 else 0) if args.sl_atr_mult>0 else None
            tp_px = entry_px - (args.tp_atr_mult * r["atr"] if args.tp_atr_mult>0 else 0) if args.tp_atr_mult>0 else None
        else:
            sl_px = entry_px - args.sl_atr_mult * r["atr"] if args.sl_atr_mult>0 else None
            tp_px = entry_px + args.tp_atr_mult * r["atr"] if args.tp_atr_mult>0 else None

        if live:
            res = create_market_open_single(args.ccxt_fetcher, sym, side=args.side,
                                            notional_cap=args.position_notional, price=entry_px,
                                            position_mode=position_mode)
            if not res.get("ok"):
                if res.get("skip_reason"):
                    log(f"[live OPEN SKIP] {sym}: {res['skip_reason']}", args.logfile)
                else:
                    log(f"[live OPEN FAIL] {sym}: {res.get('error')}", args.logfile)
                continue
            od = res["order"]; qty = res["qty"]
            log(f"[live OPEN OK] {sym} side={args.side.upper()} qty≈{qty:.6g} @ {entry_px:.6g} sl={sl_px} tp={tp_px} params={res.get('params')} id={od.get('id')}", args.logfile)
            if args.place_brackets and (sl_px or tp_px):
                br = place_brackets_single(args.ccxt_fetcher, sym, side=args.side, qty=qty,
                                           sl_px=sl_px, tp_px=tp_px, position_mode=position_mode)
                log(f"[live BRACKETS] {sym} sl_id={br.get('sl_id')} tp_id={br.get('tp_id')}", args.logfile)
        else:
            log(f"[paper OPEN] {sym} side={args.side.upper()} entry≈{entry_px:.6g} sl≈{sl_px} tp≈{tp_px}", args.logfile)

        st["positions"][sym] = {
            "entry_time": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat().replace("+00:00","Z"),
            "entry_price": float(entry_px),
            "atr_entry": float(r["atr"]),
            "notional": float(args.position_notional),
            "trail_active": False, "trail_stop": None,
            "high_seen": float(entry_px), "low_seen": float(entry_px),
            "side": args.side,
        }
        append_trade(args.trades_csv, {
            "ts": now_utc(), "symbol": sym, "side": f"OPEN-{args.side.upper()}",
            "entry_price": entry_px, "notional": args.position_notional, "sl": sl_px, "tp": tp_px,
            "mode": "LIVE" if live else "PAPER"
        })
        save_state(args.state_path, st)
        opened.append(sym); cap_left -= args.position_notional

    log(f"[open] opened={len(opened)}", args.logfile)
    return opened

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str, default="short_top_gainers_v12.yaml")
    ap.add_argument("--env-file", type=str, default="")
    ap.add_argument("--source", type=str, default="ccxt")
    ap.add_argument("--ccxt-exchange", type=str, default="bingx")
    ap.add_argument("--ccxt-symbol-format", type=str, default="usdtm")
    ap.add_argument("--symbols", type=str, default="")
    ap.add_argument("--quote", type=str, default="USDT")
    ap.add_argument("--timeframe", type=str, default="1h")
    ap.add_argument("--limit_klines", type=int, default=180)
    ap.add_argument("--max-universe", type=int, default=550)
    ap.add_argument("--top-n", type=int, default=6)
    ap.add_argument("--debug", action="store_true", default=False)
    ap.add_argument("--force-start-always", action="store_true", default=True)
    ap.add_argument("--reset-state", action="store_true", default=False)
    # live/paper toggles
    ap.add_argument("--live", action="store_true", default=False)
    ap.add_argument("--papertrade", action="store_true", default=False)
    ap.add_argument("--place-brackets", action="store_true", default=False)
    ap.add_argument("--position-mode", choices=["oneway","hedge"], default=os.getenv("BINGX_POSITION_MODE","hedge"))
    ap.add_argument("--position-notional", type=float, default=2.2, help="max USDT per order (cap)")
    ap.add_argument("--side", choices=["short","long"], default="short")  # <— НАПРЯМОК
    args = ap.parse_args()

    load_env_from_file(args.env_file)
    api_key = os.environ.get("BINGX_KEY", ""); api_sec = os.environ.get("BINGX_SECRET", "")
    log(f'API: key="{mask(api_key)}", secret="{mask(api_sec)}"')

    cfg = {}
    if args.config and os.path.exists(args.config):
        cfg = yaml.safe_load(open(args.config,"r",encoding="utf-8")) or {}

    defaults = {
        "min_qv_24h": 200000, "min_qv_1h": 10000,
        "min_atr_ratio": 0.022, "max_atr_ratio": 0.03,
        "min_momentum_sum": 0.0, "min_vol_surge_mult": 0.98, "min_breadth": 0.00,
        "min_rsi": 0.0, "min_ob": 85.0,
        "w_rsi": 0.34, "w_stoch": 0.33, "w_mfi": 0.33, "w_hcp": 0.0,
        "sl_atr_mult": 0.0, "tp_atr_mult": 0.0,
        "hold_hours": 48,
        "position_notional": args.position_notional,
        "max_notional_frac": 0.5, "initial_equity": 200.0,
        "slippage_per_side": 0.0003, "fee_rate": 0.001, "funding_rate_hour": 0.00002,
        "tick_pct": 0.0001,
        "logfile": "tg_v12.log", "trades_csv": "tg_trades.csv", "state_path": "tg_state.json",
        "live": False, "papertrade": True
    }
    for k,v in (cfg or {}).items(): defaults[k] = v
    for k,v in defaults.items():
        if not hasattr(args, k): setattr(args, k, v)

    # вирішуємо режим
    live_cfg = bool(defaults.get("live", False)) and not bool(defaults.get("papertrade", True))
    have_keys = bool(api_key) and bool(api_sec)
    if args.papertrade: will_live = False
    elif args.live:     will_live = have_keys
    else:               will_live = live_cfg and have_keys
    log(f"[mode] {'LIVE' if will_live else 'PAPER'} | side={args.side.upper()} | timeframe={args.timeframe} top_n={args.top_n}", args.logfile)

    fetcher = CCXTFetcher(exchange=args.ccxt_exchange, symbol_format=args.ccxt_symbol_format,
                          debug=args.debug, logfile=getattr(args,"logfile",""))
    args.ccxt_fetcher = fetcher
    if will_live:
        fetcher.ex.apiKey = api_key; fetcher.ex.secret = api_sec

    if args.reset_state and os.path.exists(args.state_path):
        try: os.remove(args.state_path); log(f"[state] reset: removed {args.state_path}", args.logfile)
        except Exception: pass

    uni = build_universe(args)
    cands = select_candidates(uni if not args.symbols else [s.strip() for s in args.symbols.split(",") if s.strip()], args)
    opened = entry_phase(cands, args)
    log(f"[done] opened={len(opened)}", args.logfile)

if __name__ == "__main__":
    main()
