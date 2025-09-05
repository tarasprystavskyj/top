#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, time, math, json, argparse, re
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import yaml
import pandas as pd
import numpy as np

try:
    import ccxt
except Exception:
    ccxt = None

from filelock import FileLock

# ---------------- utils ----------------
def now_utc_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg: str, logfile: str = ""):
    line = f"[{now_utc_str()}] {msg}"
    print(line, flush=True)
    if logfile:
        try:
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

def mask(s: str) -> str:
    if not s:
        return "<empty>"
    return s[:3] + "..." + s[-3:] if len(s) >= 7 else "***"

def load_env(path: str):
    if not path or not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

RATE_MS = int(os.getenv("BINGX_RATE_MS", "350"))
def sleep_ms(ms: int): time.sleep(max(0, ms)/1000.0)

# -------------- indicators --------------
def rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(up, index=close.index).ewm(alpha=1/period, adjust=False).mean()
    roll_down = pd.Series(down, index=close.index).ewm(alpha=1/period, adjust=False).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)

def compute_feats(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) < 30:
        return pd.DataFrame()
    c = df["close"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    v = df["volume"].astype(float).values
    n = len(df)

    tr = np.zeros(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    atr = pd.Series(tr).rolling(14, min_periods=1).mean().values
    atr_ratio = atr / np.maximum(c, 1e-12)

    def pct(a, b): return (c[b]-c[a]) / max(c[a], 1e-12)
    dp6 = np.array([pct(max(0, i-6), i) for i in range(n)])
    dp12 = np.array([pct(max(0, i-12), i) for i in range(n)])

    qv_bar = c * v
    qv_24h = pd.Series(qv_bar).rolling(24, min_periods=1).sum().values
    avg_24 = pd.Series(qv_bar).rolling(24, min_periods=1).mean().values
    volx = np.where(avg_24 > 0, qv_bar/avg_24, 0.0)

    out = pd.DataFrame({
        "open": df["open"].values, "high": h, "low": l, "close": c,
        "atr_ratio": atr_ratio, "dp6h": dp6, "dp12h": dp12, "mom": dp6+dp12,
        "qv_24h": qv_24h, "quote_volume": qv_bar, "vol_surge_mult": volx,
        "rsi": rsi_series(df["close"]).values
    }, index=df.index)
    return out

# -------------- CCXT wrapper --------------
class CCXTFetcher:
    def __init__(self, exchange="bingx", symbol_format="usdtm", debug=False, logfile=""):
        self.debug = debug
        self.logfile = logfile
        if not ccxt:
            raise RuntimeError("ccxt is not installed. pip install 'ccxt<5'")

        self.ex = getattr(ccxt, exchange)({"enableRateLimit": True, "timeout": 20000})
        self.symbol_format = symbol_format
        try:
            self.markets = self.ex.load_markets()
        except Exception as e:
            self.markets = {}
            log(f"[ccxt load_markets] {e}", self.logfile)

        self.by_base: Dict[str, str] = {}
        for m in self.markets.values():
            try:
                if m.get("swap") and m.get("quote") == "USDT":
                    b = m.get("base")
                    if b:
                        self.by_base[b] = m["symbol"]
            except Exception:
                pass

    def resolve_symbol(self, s: str) -> str:
        base, quote = s.split("-")
        if self.symbol_format == "usdtm":
            if base in self.by_base:
                return self.by_base[base]
            cand = f"{base}/USDT:USDT"
            if cand in self.markets:
                return cand
            raise ccxt.BadSymbol(f"No USDT-margined swap for {base}")
        return f"{base}/{quote}"

    def fetch_ticker_price(self, symbol: str) -> Optional[float]:
        try:
            ccxt_sym = self.resolve_symbol(symbol)
            t = self.ex.fetch_ticker(ccxt_sym)
            for k in ("last", "close", "bid", "ask"):
                if t.get(k) is not None:
                    return float(t[k])
        except Exception as e:
            log(f"[ccxt price] {symbol}: {e}", self.logfile)
        return None

    def fetch_ohlcv_df(self, symbol: str, timeframe="1h", limit=180) -> Optional[pd.DataFrame]:
        try:
            ccxt_sym = self.resolve_symbol(symbol)
            data = self.ex.fetch_ohlcv(ccxt_sym, timeframe=timeframe, limit=limit)
        except Exception as e:
            log(f"[ccxt ohlcv] {symbol}: {e}", self.logfile)
            return None
        if not data:
            return None
        df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("ts", inplace=True)
        return df

# -------------- qty rules --------------
def infer_qty_rules(market: dict):
    step = None
    min_qty = None
    try:
        step = market.get("limits", {}).get("amount", {}).get("step") or market.get("precision", {}).get("amount")
    except Exception:
        pass
    try:
        info = market.get("info") or {}
        if isinstance(info, dict):
            if info.get("minQty") is not None:
                min_qty = float(info["minQty"])
            if not step and info.get("stepSize") is not None:
                step = float(info["stepSize"])
    except Exception:
        pass
    return (float(step) if step else None, float(min_qty) if min_qty else None)

def round_to_step(value: float, step: Optional[float]) -> float:
    if not step or step <= 0:
        return float(round(value, 8))
    return math.floor(value / step) * step

def qty_for_notional(market: dict, notional: float, price: float):
    step, min_qty = infer_qty_rules(market)
    est = max(0.0, notional / max(price, 1e-12))
    q = round_to_step(est, step)
    if min_qty:
        q = max(q, min_qty)
    min_notional_required = (min_qty or 0.0) * price
    return float(q), float(min_notional_required), step, min_qty

# -------------- IO --------------
def load_state(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        return json.loads(open(path, "r", encoding="utf-8").read())
    except Exception:
        return {}

def save_state(path: str, st: dict):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        log(f"[state] save failed: {e}")

# -------------- selection (C2) --------------
def select_candidates(universe: List[str], args) -> List[Dict[str, Any]]:
    recs = []
    for s in universe:
        df = args.ccxt_fetcher.fetch_ohlcv_df(s, timeframe=args.timeframe, limit=args.limit_klines)
        if df is None:
            log(f"[filter] {s} -> SKIP df=None", args.logfile); continue
        feats = compute_feats(df)
        if len(feats) < 30:
            log(f"[filter] {s} -> SKIP feats={len(feats)}", args.logfile); continue

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

        px = args.ccxt_fetcher.fetch_ticker_price(s)
        if not px:
            log(f"[filter] {s} -> SKIP no price", args.logfile); continue

        log(f"[select] {s} ok | px={px:.6g} r%={atr:.4f} dp6h={dp6:.4f} dp12h={dp12:.4f} mom={mom:.4f} volx={volx:.2f} qv1h={qv1:.0f} qv24h={qv24:.0f}", args.logfile)

        recs.append({"symbol": s, "price": float(px), "atr": atr, "mom": mom, "volx": volx})
    # ATR window
    atr_pass = []
    for r in recs:
        if r["atr"] < args.min_atr_ratio:
            log(f"[filter] {r['symbol']} -> SKIP r%: {r['atr']:.4f} < {args.min_atr_ratio:.4f}", args.logfile); continue
        atr_pass.append(r)

    total = len(atr_pass); positives = sum(1 for r in atr_pass if r["mom"] > 0)
    breadth = (positives / total) if total > 0 else 0.0
    log(f"[breadth] {positives}/{total} = {breadth:.2f} (min {args.min_breadth:.2f})", args.logfile)

    finals = []
    for r in atr_pass:
        reasons = []
        if r["mom"] < args.min_momentum_sum: reasons.append(f"mom {r['mom']:.4f} < {args.min_momentum_sum:.4f}")
        if r["volx"] + 1e-6 < args.min_vol_surge_mult: reasons.append(f"volx {r['volx']:.2f} < {args.min_vol_surge_mult:.2f}")
        if breadth < args.min_breadth: reasons.append(f"breadth {breadth:.2f} < {args.min_breadth:.2f}")
        if reasons:
            log(f"[filter] {r['symbol']} -> SKIP final: " + "; ".join(reasons), args.logfile)
        else:
            finals.append(r)

    finals.sort(key=lambda x: x["mom"], reverse=True)
    log(f"[SELECT] Final candidates ({len(finals)}): {[r['symbol'] for r in finals]}", args.logfile)
    return finals[: int(args.top_n)]

# -------------- orders (LONG-only for C2) --------------
def place_open_long(fetcher: CCXTFetcher, sym: str, notional: float, price: float, position_mode: str):
    ccxt_sym = fetcher.resolve_symbol(sym)
    mkt = fetcher.markets.get(ccxt_sym, {})
    qty, min_notional_req, step, min_qty = qty_for_notional(mkt, notional, price)

    if min_notional_req > notional + 1e-9:
        return {"ok": False, "skip_reason": f"min_notional {min_notional_req:.6g} > cap {notional:.6g}", "qty_rules": {"min_qty": min_qty, "step": step}}

    params = {"reduceOnly": False}
    if position_mode == "hedge":
        params["positionSide"] = "LONG"

    try:
        od = fetcher.ex.create_order(ccxt_sym, "market", "buy", qty, None, params)
        sleep_ms(RATE_MS)
        return {"ok": True, "order": od, "qty": qty, "params": params}
    except Exception as e:
        msg = str(e)
        # мінімальна кількість
        if ("80001" in msg) or ("minimum order amount" in msg.lower()):
            # визначимо з тексту, якщо можливо
            m = re.search(r"minimum order amount is\s+([0-9]*\.?[0-9]+)\s*([A-Za-z0-9]+)", msg, re.I)
            req_qty = float(m.group(1)) if m else (min_qty or float("nan"))
            req_notional = req_qty * price if req_qty==req_qty else (min_notional_req or float("nan"))
            return {"ok": False, "skip_reason": f"exchange_min_qty {req_qty} -> notional≈{req_notional:.4f} > cap {notional:.4f}"}
        # positionSide у hedge (разова спроба з явним параметром)
        if ("109400" in msg) or ("PositionSide" in msg):
            params["positionSide"] = "LONG"
            try:
                od = fetcher.ex.create_order(ccxt_sym, "market", "buy", qty, None, params)
                sleep_ms(RATE_MS)
                return {"ok": True, "order": od, "qty": qty, "params": params, "retry": True}
            except Exception as e2:
                return {"ok": False, "error": str(e2), "qty": qty, "params": params}
        return {"ok": False, "error": msg, "qty": qty, "params": params}

def place_reduce_only(fetcher: CCXTFetcher, sym: str, side_close: str, qty: float, trigger_px: Optional[float], position_mode: str):
    ccxt_sym = fetcher.resolve_symbol(sym)
    params = {"reduceOnly": True}
    if position_mode == "hedge":
        params["positionSide"] = "LONG"
    if trigger_px is not None:
        # Залишаємо різні ключі — що сприйметься біржею
        for k in ("stopPrice", "triggerPrice", "takeProfitPrice", "stopLossPrice"):
            params[k] = trigger_px
    try:
        od = fetcher.ex.create_order(ccxt_sym, "market", side_close, qty, None, params)
        sleep_ms(RATE_MS)
        if not od or not od.get("id"):
            # ccxt didn't raise but response looks suspicious – log for debugging
            log(f"[live reduceOnly] {sym}: unexpected response {od}")
        return od
    except Exception as e:
        log(f"[live reduceOnly] {sym}: {e} params={params}")
        return None

# -------------- daemon loop --------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str, default="cs_rs_c2_v1.yaml")
    ap.add_argument("--env-file", type=str, default="")
    ap.add_argument("--source", type=str, default="ccxt")
    ap.add_argument("--ccxt-exchange", type=str, default="bingx")
    ap.add_argument("--ccxt-symbol-format", type=str, default="usdtm")
    ap.add_argument("--timeframe", type=str, default="1h")
    ap.add_argument("--limit_klines", type=int, default=180)
    ap.add_argument("--max-universe", type=int, default=550)
    ap.add_argument("--top-n", type=int, default=4)
    ap.add_argument("--poll-sec", type=int, default=15)
    ap.add_argument("--bar-delay-sec", type=int, default=10)
    ap.add_argument("--place-brackets", action="store_true", default=False)
    ap.add_argument("--position-mode", choices=["oneway","hedge"], default=os.getenv("BINGX_POSITION_MODE","hedge"))
    ap.add_argument("--position-notional", type=float, default=2.2)
    ap.add_argument("--debug", action="store_true", default=False)
    args = ap.parse_args()

    load_env(args.env_file)
    api_k = os.environ.get("BINGX_KEY",""); api_s = os.environ.get("BINGX_SECRET","")
    log(f'API: key="{mask(api_k)}", secret="{mask(api_s)}"')

    # YAML (мінімальний набір)
    cfg = {}
    if args.config and os.path.exists(args.config):
        cfg = yaml.safe_load(open(args.config, "r", encoding="utf-8")) or {}

    # Apply defaults for selection params
    args.min_qv_24h = float(cfg.get("min_qv_24h", 200000))
    args.min_qv_1h = float(cfg.get("min_qv_1h", 10000))
    args.min_atr_ratio = float(cfg.get("min_atr_ratio", 0.022))
    args.min_momentum_sum = float(cfg.get("min_momentum_sum", 0.12))
    args.min_vol_surge_mult = float(cfg.get("min_vol_surge_mult", 1.25))
    args.min_breadth = float(cfg.get("min_breadth", 0.60))
    args.sl_atr_mult = float(cfg.get("sl_atr_mult", 1.4))
    args.tp_atr_mult = float(cfg.get("tp_atr_mult", 2.6))
    args.logfile = cfg.get("logfile", "c2_bot.log")
    args.state_path = cfg.get("state_path", "c2_state.json")
    args.trades_csv = cfg.get("trades_csv", "c2_trades.csv")

    # CCXT
    fetcher = CCXTFetcher(exchange=args.ccxt_exchange, symbol_format=args.ccxt_symbol_format,
                          debug=args.debug, logfile=args.logfile)
    args.ccxt_fetcher = fetcher
    if api_k and api_s:
        fetcher.ex.apiKey = api_k
        fetcher.ex.secret = api_s

    # universe = всі USDT swap
    uni = []
    for m in fetcher.markets.values():
        try:
            if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True):
                base = m.get("base")
                if base:
                    uni.append(f"{base}-USDT")
        except Exception:
            pass
    uni = sorted(set(uni))
    if args.max_universe > 0:
        uni = uni[:int(args.max_universe)]
    log(f"[universe] size={len(uni)} (USDT swaps)")

    state = load_state(args.state_path)
    if "positions" not in state:
        state["positions"] = {}
        save_state(args.state_path, state)

    log(f"[daemon] started. polling exits every {args.poll_sec}s; entries at new bar close (+{args.bar_delay_sec}s)")

    last_bar_ts = None

    try:
        while True:
            # 1) перевірка нового бару
            now = datetime.utcnow().replace(tzinfo=timezone.utc)
            bar_close = now.replace(minute=0, second=0, microsecond=0)
            if (last_bar_ts is None or bar_close > last_bar_ts) and (now - bar_close).total_seconds() >= args.bar_delay_sec:
                last_bar_ts = bar_close
                log("[bar] new closed bar detected -> selecting & opening")
                cands = select_candidates(uni, args)

                opened = 0
                for r in cands:
                    sym = r["symbol"]
                    entry_px = fetcher.fetch_ticker_price(sym) or r["price"]
                    if not entry_px:
                        log(f"[entry] {sym} -> SKIP: no price", args.logfile); continue

                    ccxt_sym = fetcher.resolve_symbol(sym)
                    mkt = fetcher.markets.get(ccxt_sym, {})
                    _, min_notional_req, _, min_qty = qty_for_notional(mkt, args.position_notional, entry_px)
                    if min_notional_req > args.position_notional + 1e-9:
                        log(f"[entry] {sym} -> SKIP: min_notional {min_notional_req:.4f} > cap {args.position_notional:.4f} (min_qty={min_qty})"); continue

                    sl_px = entry_px - args.sl_atr_mult * r["atr"] if args.sl_atr_mult > 0 else None
                    tp_px = entry_px + args.tp_atr_mult * r["atr"] if args.tp_atr_mult > 0 else None

                    res = place_open_long(fetcher, sym, args.position_notional, entry_px, args.position_mode)
                    if not res.get("ok"):
                        if res.get("skip_reason"):
                            log(f"[live open] {sym}: SKIP -> {res['skip_reason']}")
                        else:
                            log(f"[live open] {sym}: FAIL -> {res.get('error')}")
                        continue

                    od = res["order"]; qty = res["qty"]
                    log(f"[live open] {sym} LONG qty≈{qty:.6g} @ {entry_px:.6g} sl={sl_px} tp={tp_px} id={od.get('id')} params={res.get('params')}")

                    if args.place_brackets and (sl_px or tp_px):
                        if sl_px:
                            sl_od = place_reduce_only(fetcher, sym, "sell", qty, sl_px, args.position_mode)
                            if not sl_od or not sl_od.get("id"):
                                log(f"[live SL] {sym} -> ERR qty={qty} px={sl_px} resp={sl_od}")
                            else:
                                log(f"[live SL] {sym} -> {sl_od.get('id')}")
                        if tp_px:
                            tp_od = place_reduce_only(fetcher, sym, "sell", qty, tp_px, args.position_mode)
                            if not tp_od or not tp_od.get("id"):
                                log(f"[live TP] {sym} -> ERR qty={qty} px={tp_px} resp={tp_od}")
                            else:
                                log(f"[live TP] {sym} -> {tp_od.get('id')}")

                    state = load_state(args.state_path)
                    state["positions"][sym] = {
                        "entry_time": now_utc_str(),
                        "entry_price": float(entry_px),
                        "atr_entry": float(r["atr"]),
                        "notional": float(args.position_notional),
                        "side": "long"
                    }
                    save_state(args.state_path, state)
                    opened += 1

                log(f"[open] now_positions={len(load_state(args.state_path).get('positions', {}))}")

            # 2) тут би логіка виходів/трейла/тайм-аута (за потреби)
            # залишено спрощено — бо у вас TP/SL ставляться скобками на біржі.

            time.sleep(args.poll_sec)
    except KeyboardInterrupt:
        log("[daemon] stopping...")

if __name__ == "__main__":
    main()
