
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
short_top_gainers_bot_with_filters_v12.py
v12.17 (2025-08-10)

- FIX: BingX Hedge mode → remove `reduceOnly` from orders (109400 error).
- Safe close: BUY with positionSide=SHORT (hedge), no reduceOnly.
- Still: all signed params go in query; empty body.
"""

VERSION = "12.17"

import os, sys, json, argparse, re, traceback, time, hmac, hashlib
import datetime as dt
from typing import List, Dict, Any, Optional, Tuple
import requests, pandas as pd, numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

try: import yaml
except Exception: yaml = None
try: import ccxt
except Exception: ccxt = None

def now_utc():
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)

def log(msg, logfile=""):
    line=f"[{now_utc():%Y-%m-%d %H:%M:%S} UTC] {msg}"
    print(line, flush=True)
    if logfile:
        try: open(logfile,"a",encoding="utf-8").write(line+"\n")
        except Exception: pass

def _strip_quotes(v: str) -> str:
    v = v.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    return v

def load_env_from_file(path: Optional[str]):
    if not path: return
    if not os.path.exists(path): return
    for raw in open(path,"r",encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k,v = line.split("=",1)
        os.environ[k.strip()] = _strip_quotes(v.strip())

def mask(val: str) -> str:
    if not val: return "<empty>"
    return val[:3]+"..."+val[-3:] if len(val)>=7 else "***"

# Telegram (minimal; can be disabled with --no-telegram)
def _tg_token_normalized() -> str:
    t = (os.getenv("TELEGRAM_TOKEN") or "").strip()
    t = _strip_quotes(t)
    if t.lower().startswith("bot"): t = t[3:].strip()
    return t

def tg_enabled(args=None) -> bool:
    if args and getattr(args,"no_telegram",False): return False
    token = _tg_token_normalized()
    chat = (os.getenv("TELEGRAM_CHAT") or "").strip()
    return bool(token) and bool(chat)

def tg_send(text: str, args=None):
    if not tg_enabled(args): return
    token = _tg_token_normalized()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chat = (os.getenv("TELEGRAM_CHAT") or "").strip()
    try:
        r = requests.post(url, data={"chat_id": chat, "text": text}, timeout=10)
        if r.status_code != 200:
            try: js=r.json(); code=js.get("error_code"); desc=js.get("description","")
            except Exception: js=None; code=r.status_code; desc=r.text
            log(f"[Telegram] error {code}: {desc}", args.logfile if args else "")
    except Exception as e:
        log(f"[Telegram] exception: {e}", args.logfile if args else "")

# HTTP session
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
    except Exception: pass
    return s
SESSION = make_session()

def base_url(demo: bool=False):
    return "https://open-api-vst.bingx.com" if demo else "https://open-api.bingx.com"

# --- Public HTTP ---
def http_fetch_ticker(symbol: str, demo=False) -> dict:
    r = SESSION.get(f"{base_url(demo)}/openApi/swap/v2/quote/ticker",
                    params={"symbol": symbol}, timeout=(3,8))
    r.raise_for_status()
    data = r.json().get("data") or {}
    return data[0] if isinstance(data, list) and data else data

def http_fetch_klines(symbol: str, interval="1h", limit=150, demo=False) -> list:
    r = SESSION.get(f"{base_url(demo)}/openApi/swap/v3/quote/klines",
                    params={"symbol": symbol, "interval": interval, "limit": limit},
                    timeout=(5,12))
    r.raise_for_status()
    data = r.json().get("data") or r.json()
    return data if isinstance(data, list) else []

def http_discover_usdt_perps(quote="USDT", demo=False) -> List[str]:
    candidates=[
        "/openApi/swap/v3/quote/contracts","/openApi/swap/v2/quote/contracts",
        "/openApi/swap/v3/market/getAllContracts","/openApi/swap/v2/market/getAllContracts",
        "/openApi/swap/v2/quote/symbols",
    ]
    out=[]
    for path in candidates:
        try:
            r=SESSION.get(f"{base_url(demo)}{path}",timeout=(5,12))
            if r.status_code!=200: continue
            js=r.json(); data=js.get("data",js)
            if isinstance(data,dict) and "contracts" in data: data=data["contracts"]
            if not isinstance(data,list): continue
            for it in data:
                sym=None; base=None; q=None
                if isinstance(it,dict):
                    sym=it.get("symbol") or it.get("contractName")
                    base=(it.get("base") or it.get("baseAsset") or "").upper()
                    q=(it.get("quote") or it.get("quoteAsset") or "").upper()
                elif isinstance(it,list) and it and isinstance(it[0],str):
                    sym=it[0]
                if base and q:
                    if q!=quote.upper(): continue
                    out.append(f"{base}-{q}")
                elif isinstance(sym,str):
                    s=sym.replace("/","-").replace("_","-").upper()
                    s=s.split(":")[0]
                    if s.endswith(f"-{quote.upper()}"): out.append(s)
        except Exception: continue
    return sorted(set(out))

# --- CCXT fetcher (data only) ---
class CCXTFetcher:
    def __init__(self, exchange="bingx", symbol_format="usdtm", debug=False, logfile=""):
        if ccxt is None: raise RuntimeError("ccxt is not installed. Try: pip install 'ccxt<5'")
        self.debug=debug; self.logfile=logfile
        if not hasattr(ccxt, exchange): raise RuntimeError(f"ccxt.{exchange} not found")
        self.ex=getattr(ccxt, exchange)({"enableRateLimit": True})
        self.symbol_format=symbol_format
    def _map_symbol(self,s:str)->str:
        base,quote=s.split("-")
        return f"{base}/{quote}" if self.symbol_format=="spot" else f"{base}/{quote}:USDT"
    def fetch_ohlcv_df(self, symbol: str, timeframe="1h", limit=150) -> Optional[pd.DataFrame]:
        ccxt_sym=self._map_symbol(symbol)
        try: data=self.ex.fetch_ohlcv(ccxt_sym,timeframe=timeframe,limit=limit)
        except Exception as e:
            log(f"[ccxt ohlcv] {symbol}: {e}", self.logfile)
            if self.debug: traceback.print_exc()
            return None
        if not data: return None
        rows=[{"ts":pd.to_datetime(int(ts),unit="ms",utc=True),
               "open":float(o),"high":float(h),"low":float(l),"close":float(c),"volume":float(v)} 
               for ts,o,h,l,c,v in data]
        return pd.DataFrame(rows).drop_duplicates("ts").sort_values("ts").set_index("ts")
    def fetch_ticker(self, symbol: str)->Optional[dict]:
        ccxt_sym=self._map_symbol(symbol)
        try: return self.ex.fetch_ticker(ccxt_sym)
        except Exception as e: log(f"[ccxt ticker] {symbol}: {e}", self.logfile); return None
    def fetch_ticker_price(self,symbol:str)->Optional[float]:
        t=self.fetch_ticker(symbol)
        if not t: return None
        for k in ("last","close","ask","bid"):
            if k in t and k in t and t[k] is not None: return float(t[k])
        return None
    def discover_usdt_perps(self, quote="USDT")->List[str]:
        try: markets=self.ex.load_markets()
        except Exception as e: log(f"[ccxt markets] {e}", self.logfile); return []
        out=[]
        for m in markets.values():
            if not (m.get("contract") or m.get("swap")): continue
            if not m.get("linear"): continue
            if (m.get("quote") or "").upper()!=quote.upper(): continue
            base=(m.get("base") or "").upper()
            if base: out.append(f"{base}-{quote.upper()}")
        return sorted(set(out))

# --- Indicators ---
def timeframe_hours(tf: str) -> float:
    tf=(tf or "").lower().strip()
    if tf.endswith("h"):
        try: return float(tf[:-1])
        except: return 1.0
    if tf.endswith("m"):
        try: return float(tf[:-1])/60.0
        except: return 1.0
    if tf.endswith("d"):
        try: return float(tf[:-1])*24.0
        except: return 24.0
    return 1.0

def rsi_wilder(close: np.ndarray, period: int = 14) -> np.ndarray:
    c=np.asarray(close,dtype=float); n=c.size
    out=np.full(n,np.nan,dtype=float)
    if n<period+1: return out
    diff=np.diff(c)
    up=np.where(diff>0,diff,0.0); dn=np.where(diff<0,-diff,0.0)
    avg_gain=np.empty(n,dtype=float); avg_loss=np.empty(n,dtype=float)
    avg_gain[:]=np.nan; avg_loss[:]=np.nan
    avg_gain[period]=up[:period].mean(); avg_loss[period]=dn[:period].mean()
    for i in range(period+1,n):
        avg_gain[i]=(avg_gain[i-1]*(period-1)+up[i-1])/period
        avg_loss[i]=(avg_loss[i-1]*(period-1)+dn[i-1])/period
    rs=np.divide(avg_gain,avg_loss,out=np.full(n,np.nan),where=avg_loss!=0)
    rsi=100.0-(100.0/(1.0+rs))
    rsi=np.where(avg_loss==0,100.0,rsi)
    out[period:]=rsi[period:]; return out

def stoch_k(high, low, close, k=14):
    high=pd.Series(high,dtype=float); low=pd.Series(low,dtype=float); close=pd.Series(close,dtype=float)
    ll=low.rolling(k,min_periods=k).min(); hh=high.rolling(k,min_periods=k).max()
    return ((close-ll)/np.maximum(hh-ll,1e-12)*100.0).values

def mfi(high, low, close, volume, period=14):
    tp=(np.array(high)+np.array(low)+np.array(close))/3.0
    mf=tp*np.array(volume)
    pmf=np.zeros_like(tp); nmf=np.zeros_like(tp)
    pmf[1:]=np.where(tp[1:]>tp[:-1],mf[1:],0.0)
    nmf[1:]=np.where(tp[1:]<tp[:-1],mf[1:],0.0)
    rp=pd.Series(pmf).rolling(period,min_periods=period).sum().values
    rn=pd.Series(nmf).rolling(period,min_periods=period).sum().values
    mfr=rp/np.maximum(rn,1e-12)
    return 100.0-(100.0/(1.0+mfr))

def compute_indicators_df(symbol: str, df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    h=df["high"].to_numpy(dtype="float64"); l=df["low"].to_numpy(dtype="float64")
    c=df["close"].to_numpy(dtype="float64"); v=df["volume"].to_numpy(dtype="float64")
    def atr_ratio_arr(high,low,close,period=14):
        h=np.array(high,float); l=np.array(low,float); c=np.array(close,float)
        if len(c)<period+1: return np.full(len(c),np.nan)
        pc=np.r_[np.nan,c[:-1]]
        tr=np.vstack([h-l,np.abs(h-pc),np.abs(l-pc)]); tr=np.nanmax(tr,axis=0)
        atr=pd.Series(tr).rolling(period,min_periods=period).mean().values
        return atr/np.maximum(c,1e-12)
    rsi_arr=rsi_wilder(c,14); stoch_arr=stoch_k(h,l,c,14); mfi_arr=mfi(h,l,c,v,14); atr_arr=atr_ratio_arr(h,l,c,14)
    qv_arr=v*c
    step_h=max(0.00001,timeframe_hours(timeframe)); win=max(1,int(round(24.0/step_h)))
    qv24_arr=pd.Series(qv_arr).rolling(win,min_periods=win).sum().to_numpy()
    high24=pd.Series(h).rolling(win,min_periods=win).max().to_numpy()
    hcp_arr=(c/np.maximum(high24,1e-12)).clip(0,10)*100.0
    m=min(len(df),len(rsi_arr),len(stoch_arr),len(mfi_arr),len(atr_arr),len(qv_arr),len(qv24_arr),len(hcp_arr))
    if m<=0: return df.iloc[0:0].copy()
    if len(df)!=m: df=df.iloc[-m:].copy()
    df["rsi"]=rsi_arr[-m:]; df["stoch"]=stoch_arr[-m:]; df["mfi"]=mfi_arr[-m:]; df["atr_ratio"]=atr_arr[-m:]
    df["quote_volume"]=qv_arr[-m:]; df["qv_24h"]=qv24_arr[-m:]; df["highclose_pct"]=hcp_arr[-m:]
    return df

# CSV/State
def load_state(path:str)->Dict[str,Any]:
    if os.path.exists(path):
        try: return json.load(open(path,"r",encoding="utf-8"))
        except Exception: pass
    return {"open_positions": {}, "last_open_time_by_sym": {}, "_force_used": False}
def save_state(path:str, st:Dict[str,Any]):
    try: json.dump(st, open(path,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as e: log(f"[state save] {e}")
def append_trade_csv(csv_path:str, row:Dict[str,Any]):
    import csv
    cols=["ts_utc","symbol","side","qty","price","note","mode"]
    exists=os.path.exists(csv_path)
    with open(csv_path,"a",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=cols)
        if not exists: w.writeheader()
        w.writerow({k: row.get(k) for k in cols})

# Cache
def load_symbols_from_cache(cache_path:str, ttl_sec:int=0)->List[str]:
    try:
        if cache_path and os.path.exists(cache_path):
            data=json.load(open(cache_path,"r",encoding="utf-8"))
            if ttl_sec and "ts" in data:
                try:
                    ts=dt.datetime.fromisoformat(data["ts"].replace("Z","+00:00"))
                    if now_utc()-ts>dt.timedelta(seconds=int(ttl_sec)): return []
                except Exception: pass
            syms=data.get("symbols") or []
            if isinstance(syms,list): return [str(s) for s in syms]
    except Exception as e: log(f"[symbols cache] read error: {e}")
    return []
def save_symbols_to_cache(cache_path:str, symbols:List[str]):
    try:
        json.dump({"ts": now_utc().isoformat(), "symbols": symbols},
                  open(cache_path,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
        log(f"[symbols cache] saved {len(symbols)} symbols → {cache_path}")
    except Exception as e: log(f"[symbols cache] write error: {e}")

# Universe helpers
def read_symbols_file(path:str)->List[str]:
    if not path or not os.path.exists(path): return []
    out=[]
    for raw in open(path,"r",encoding="utf-8"):
        line=raw.strip()
        if not line or line.startswith("#"): continue
        out.append(line.split()[0].upper())
    return out
def apply_symbol_filters(symbols:List[str], include:List[str], exclude:List[str],
                         exclude_multipliers:bool, debug=False, logfile="")->List[str]:
    rx_inc=[re.compile(pat,re.I) for pat in include if str(pat).strip()]
    rx_exc=[re.compile(pat,re.I) for pat in exclude if str(pat).strip()]
    def pass_inc(s): return True if not rx_inc else any(r.search(s) for r in rx_inc)
    def pass_exc(s): return any(r.search(s) for r in rx_exc)
    out=[]
    for s in symbols:
        if exclude_multipliers and re.match(r"^\d", s):
            if debug: log(f"[universe] drop(multiplier): {s}", logfile); continue
        if not pass_inc(s):
            if debug: log(f"[universe] drop(no-include): {s}", logfile); continue
        if pass_exc(s):
            if debug: log(f"[universe] drop(exclude): {s}", logfile); continue
        out.append(s)
    return out

# Liquidity presort
def extract_qv24_from_ticker_dict(t:dict)->Optional[float]:
    if not isinstance(t,dict): return None
    for k in ("quoteVolume24h","quoteVolume","amount24h","amount","turnover24h","turnover"):
        v=t.get(k)
        if v is not None:
            try: return float(v)
            except: pass
    base_vol=None
    for k in ("volume24h","volume","baseVolume","baseVolume24h"):
        v=t.get(k)
        if v is not None:
            try: base_vol=float(v); break
            except: pass
    last=None
    for k in ("lastPrice","price","last","close","ask","bid"):
        v=t.get(k)
        if v is not None:
            try: last=float(v); break
            except: pass
    if base_vol is not None and last is not None: return base_vol*last
    return None
def presort_universe_by_qv24(universe:List[str], args)->List[str]:
    if not universe: return universe
    scores={}; workers=max(1,int(args.workers))
    if args.source=="ccxt" and args.ccxt_fetcher is not None:
        def task_ccxt(sym):
            t=args.ccxt_fetcher.fetch_ticker(sym)
            qv=extract_qv24_from_ticker_dict(t or {})
            return sym,(qv if qv is not None else 0.0)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sym,val in ex.map(task_ccxt,universe): scores[sym]=val
    else:
        def task_http(sym):
            try: t=http_fetch_ticker(sym, demo=args.bingx_demo)
            except Exception: t={}
            qv=extract_qv24_from_ticker_dict(t or {})
            return sym,(qv if qv is not None else 0.0)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sym,val in ex.map(task_http,universe): scores[sym]=val
    ordered=sorted(universe,key=lambda s: scores.get(s,0.0),reverse=True)
    topn=int(args.presort_top) if int(args.presort_top)>0 else len(ordered)
    kept=ordered[:topn]
    log(f"[presort] method=qv24 workers={workers} kept={len(kept)}/{len(universe)}", args.logfile)
    return kept

# Selection
def fetch_ohlcv_df_http(symbol: str, limit: int = 150, timeframe: str = "1h", demo=False) -> Optional[pd.DataFrame]:
    try:
        raw=http_fetch_klines(symbol, interval=timeframe, limit=limit, demo=demo)
        if not raw: return None
        rows=[]
        for it in raw:
            t = pd.to_datetime(int(it.get("time") or it.get("t") or it[0]), unit="ms", utc=True)
            o = float(it.get("open")  or it.get("o") or it[1])
            h = float(it.get("high")  or it.get("h") or it[2])
            l = float(it.get("low")   or it.get("l") or it[3])
            c = float(it.get("close") or it.get("c") or it[4])
            v = float(it.get("volume")or it.get("v") or it[5])
            rows.append({"ts": t, "open": o, "high": h, "low": l, "close": c, "volume": v})
        return pd.DataFrame(rows).drop_duplicates("ts").sort_values("ts").set_index("ts")
    except Exception: return None

def select_candidates(universe: List[str], args) -> Tuple[List[tuple], Dict[str,int]]:
    stats={"scanned":0,"short_df":0,"liq_fail":0,"atr_fail":0,"highs_fail":0,"obw_fail":0,"no_price":0}
    out=[]; workers=max(1,int(args.workers))
    def process_symbol(s:str):
        try:
            if args.source=="ccxt":
                df=args.ccxt_fetcher.fetch_ohlcv_df(s,timeframe=args.timeframe,limit=args.limit_klines)
            else:
                df=fetch_ohlcv_df_http(s,limit=args.limit_klines,timeframe=args.timeframe,demo=args.bingx_demo)
            if df is None or len(df)<40: return ("short_df",s,None)
            idf=compute_indicators_df(s,df,args.timeframe); r=idf.iloc[-1]
            qv1=float(r.get("quote_volume",0.0)) if pd.notna(r.get("quote_volume",np.nan)) else 0.0
            qv24=float(r.get("qv_24h",0.0)) if pd.notna(r.get("qv_24h",np.nan)) else 0.0
            if not (qv24>=args.min_qv_24h and qv1>=args.min_qv_1h): return ("liq_fail",s,(qv24,qv1))
            atrr=float(r.get("atr_ratio",np.nan)) if pd.notna(r.get("atr_ratio",np.nan)) else None
            if not (atrr and atrr>0 and atrr<=args.max_atr_ratio): return ("atr_fail",s,atrr)
            cnt=int(pd.notna(r.get("rsi")) and r["rsi"]>=args.min_rsi) \
                +int(pd.notna(r.get("stoch")) and r["stoch"]>=args.min_stoch) \
                +int(pd.notna(r.get("mfi")) and r["mfi"]>=args.min_mfi)
            if cnt<args.require_at_least_n_high: return ("highs_fail",s,cnt)
            w_rsi,w_stoch,w_mfi,w_hcp=args.w_rsi,args.w_stoch,args.w_mfi,args.w_hcp
            def nz(x):
                try: return 0.0 if pd.isna(x) else float(x)
                except: return float(x) if x is not None else 0.0
            denom=max(1e-9,w_rsi+w_stoch+w_mfi+w_hcp)
            obw=(w_rsi*nz(r["rsi"])+w_stoch*nz(r["stoch"])+w_mfi*nz(r["mfi"])+w_hcp*nz(r.get("highclose_pct",0)))/denom
            if obw<args.min_ob: return ("obw_fail",s,obw)
            if args.source=="ccxt": px=args.ccxt_fetcher.fetch_ticker_price(s)
            else:
                tick=http_fetch_ticker(s, demo=args.bingx_demo); px=None
                for k in ("lastPrice","price","last","close"):
                    if k in tick and tick[k] is not None:
                        try: px=float(tick[k]); break
                        except: pass
            if not px: return ("no_price",s,None)
            return ("ok",s,(float(px),float(obw),float(atrr)))
        except Exception as e:
            return ("error",s,str(e))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures={ex.submit(process_symbol,s):s for s in universe}
        for fut in as_completed(futures):
            res,sym,extra=fut.result(); stats["scanned"]+=1
            if res=="ok":
                px,obw,atr=extra; out.append((sym,px,obw,atr))
                if args.debug: log(f"[dbg] {sym}: PASS obw={obw:.2f}, atr={atr:.3f}", args.logfile)
            elif res in stats:
                stats[res]+=1
                if args.debug: log(f"[dbg] {sym}: {res} {extra}", args.logfile)
            else:
                if args.debug: log(f"[dbg] {sym}: error {extra}", args.logfile)
    out.sort(key=lambda x: x[2], reverse=True)
    n=int(getattr(args,"top_n",3))
    return out[:n], stats

# --- BingX signing + private requests ---
def _query_string(params: Dict[str, Any]) -> str:
    items = sorted((k, "" if v is None else str(v)) for k,v in params.items())
    return "&".join(f"{k}={v}" for k,v in items)

def _sign_bingx(secret: str, qs: str) -> str:
    return hmac.new(secret.encode('utf-8'), qs.encode('utf-8'), hashlib.sha256).hexdigest()

def _headers_bingx(api_key: str, source_key: Optional[str]=None) -> Dict[str,str]:
    h={"X-BX-APIKEY": api_key}
    if source_key: h["X-SOURCE-KEY"]=source_key
    return h

def http_signed_request(path: str, method: str, payload: Dict[str, Any], args) -> dict:
    api_key=os.getenv("BINGX_KEY",""); secret=_strip_quotes(os.getenv("BINGX_SECRET",""))
    if not api_key or not secret:
        return {"error": "missing_keys"}
    ts=int(time.time()*1000)
    params=dict(payload)
    params.setdefault("timestamp", ts)
    params.setdefault("recvWindow", int(args.recv_window))
    qs=_query_string(params)
    sig=_sign_bingx(secret, qs)
    url=f"{('https://open-api-vst.bingx.com' if args.bingx_demo else 'https://open-api.bingx.com')}{path}?{qs}&signature={sig}"
    if args.debug:
        log(f"[sign] POST {path} ? {qs}&signature=***", args.logfile)
    try:
        r=SESSION.post(url, headers=_headers_bingx(api_key, os.getenv("BINGX_SOURCE_KEY")), timeout=(5,15))
        js=r.json(); js["_status"]=r.status_code
        return js
    except Exception as e:
        return {"error":"exception","message":str(e)}

def http_place_order(payload:Dict[str,Any], args)->dict:
    return http_signed_request("/openApi/swap/v2/trade/order","POST",payload,args)

# --- Position management ---
def compute_qty_usdt(price:float,args)->float:
    target=float(args.position_notional)
    cap=float(args.max_notional_frac)*float(args.initial_equity)
    notional=min(target,cap)
    qty=round(notional/max(1e-12,price),6)
    return max(qty,0.0)

def open_short_live(symbol:str, price:float, args)->Optional[dict]:
    qty=compute_qty_usdt(price,args)
    if qty<=0:
        log(f"[OPEN LIVE] qty<=0 for {symbol}"); 
        return None
    payload={
        "symbol":symbol,
        "side":"SELL",
        "type":"MARKET",
        "quantity":qty,
        "positionSide":"SHORT",
        # no reduceOnly in hedge mode
    }
    resp=http_place_order(payload,args)
    return resp

def close_short_live(symbol:str, qty:float, args)->Optional[dict]:
    payload={
        "symbol":symbol,
        "side":"BUY",
        "type":"MARKET",
        "quantity":round(float(qty),6),
        "positionSide":"SHORT",
        # no reduceOnly
    }
    return http_place_order(payload,args)

# --- Run / Close / Cooldown ---
def run_once(args, state:Dict[str,Any])->int:
    universe=load_universe(args)
    if not universe:
        log("[universe] empty - nothing to scan", args.logfile); return 0
    if args.presort_by=="qv24": universe=presort_universe_by_qv24(universe,args)
    cands,stats=select_candidates(universe,args)
    opened=0
    for (sym,px,obw,atr) in cands:
        last_ts=state["last_open_time_by_sym"].get(sym)
        if last_ts:
            try:
                last_dt=dt.datetime.fromisoformat(last_ts)
                if now_utc()-last_dt<dt.timedelta(days=float(args.cooldown_days)):
                    log(f"[cooldown] skip {sym}", args.logfile); continue
            except Exception: pass
        entry_px=px*(1-float(args.slippage_per_side))
        qty=compute_qty_usdt(entry_px,args)
        if args.papertrade:
            state["open_positions"][sym]={"qty":qty,"entry":entry_px,"opened_at":now_utc().isoformat()}
            opened+=1
            log(f"📉 [PAPER] OPEN_SHORT {sym} | price={entry_px:.6f} OB={obw:.2f}", args.logfile)
            tg_send(f"📉 OPEN_SHORT {sym} @ {entry_px:.6f} (paper)", args)
        else:
            resp=open_short_live(sym,entry_px,args)
            ok = isinstance(resp,dict) and resp.get("code") in (0, "0") and not resp.get("error")
            if ok:
                state["open_positions"][sym]={"qty":qty,"entry":entry_px,"opened_at":now_utc().isoformat(),"live_resp":resp}
                opened+=1
                log(f"📉 [LIVE] OPEN_SHORT {sym} | price={entry_px:.6f} OB={obw:.2f}", args.logfile)
                tg_send(f"📉 OPEN_SHORT {sym} @ {entry_px:.6f} (live)", args)
            else:
                log(f"[LIVE] OPEN_SHORT FAILED {sym} resp={resp}", args.logfile)
                tg_send(f"⚠️ OPEN_SHORT FAILED {sym} → {resp}", args)
                continue
        state["last_open_time_by_sym"][sym]=now_utc().isoformat()
        append_trade_csv(args.trades_csv,{"ts_utc":now_utc().isoformat(),"symbol":sym,"side":"OPEN_SHORT",
                                          "qty":qty,"price":entry_px,"note":f"obw={obw:.2f}",
                                          "mode":"PAPER" if args.papertrade else "LIVE"})
    save_state(args.state_path,state)
    kept=len(cands); scanned=stats["scanned"]
    log(f"[select] kept={kept} / scanned={scanned} | opened={opened} | reasons short_df={stats['short_df']}, "
        f"liq_fail={stats['liq_fail']}, atr_fail={stats['atr_fail']}, highs_fail={stats['highs_fail']}, "
        f"obw_fail={stats['obw_fail']}, no_price={stats['no_price']} | workers={args.workers}", args.logfile)
    return opened

def close_expired_positions(args, state:Dict[str,Any]):
    hold=dt.timedelta(hours=float(args.hold_hours))
    to_close=[]
    for sym,info in list(state["open_positions"].items()):
        opened_at=dt.datetime.fromisoformat(info.get("opened_at"))
        if now_utc()-opened_at>=hold: to_close.append(sym)
    for sym in to_close:
        info=state["open_positions"].pop(sym,None)
        if not info: continue
        qty=info.get("qty",0.0)
        if args.papertrade:
            log(f"✅ [PAPER] CLOSE_SHORT {sym} qty={qty}", args.logfile); tg_send(f"✅ CLOSE_SHORT {sym} (paper)", args)
        else:
            resp=close_short_live(sym,qty,args)
            log(f"✅ [LIVE] CLOSE_SHORT {sym} qty={qty} resp={resp}", args.logfile); tg_send(f"✅ CLOSE_SHORT {sym} (live)", args)
        append_trade_csv(args.trades_csv,{"ts_utc":now_utc().isoformat(),"symbol":sym,"side":"CLOSE_SHORT",
                                          "qty":qty,"price":np.nan,"note":"hold exit",
                                          "mode":"PAPER" if args.papertrade else "LIVE"})
    if to_close: save_state(args.state_path,state)

# Universe loader
def load_universe(args)->List[str]:
    if args.symbols:
        syms=[s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
        if args.debug: log(f"[dbg] symbols from args: {syms}", args.logfile)
        return syms
    if args.symbols_file:
        file_syms=read_symbols_file(args.symbols_file)
        if file_syms:
            syms=apply_symbol_filters(file_syms,args.symbols_include,args.symbols_exclude,
                                      exclude_multipliers=not args.no_exclude_multipliers,
                                      debug=args.debug, logfile=args.logfile)
            if args.max_universe>0: syms=syms[:int(args.max_universe)]
            log(f"[universe] loaded {len(syms)} symbols from file {args.symbols_file}", args.logfile)
            return syms
    if args.symbols_cache and not args.refresh_symbols_cache:
        cached=load_symbols_from_cache(args.symbols_cache, ttl_sec=int(args.symbols_cache_ttl_sec))
        if cached:
            syms=apply_symbol_filters(cached,args.symbols_include,args.symbols_exclude,
                                      exclude_multipliers=not args.no_exclude_multipliers,
                                      debug=args.debug, logfile=args.logfile)
            if args.max_universe>0: syms=syms[:int(args.max_universe)]
            log(f"[universe] loaded {len(syms)} symbols from cache", args.logfile)
            return syms
    if args.source=="ccxt" and args.ccxt_fetcher is not None:
        discovered=args.ccxt_fetcher.discover_usdt_perps(quote=args.quote.upper()); origin="CCXT"
    else:
        discovered=http_discover_usdt_perps(quote=args.quote.upper(), demo=args.bingx_demo); origin="HTTP"
    syms=apply_symbol_filters(discovered,args.symbols_include,args.symbols_exclude,
                              exclude_multipliers=not args.no_exclude_multipliers,
                              debug=args.debug, logfile=args.logfile)
    if args.max_universe>0: syms=syms[:int(args.max_universe)]
    if args.symbols_cache and syms: save_symbols_to_cache(args.symbols_cache, syms)
    log(f"[universe] discovered {len(syms)} symbols via {origin}", args.logfile)
    return syms

# CLI / YAML
def build_arg_parser():
    p=argparse.ArgumentParser()
    p.add_argument("--env-file","--env","-E", dest="env_file", default=".env")
    p.add_argument("--api-key",default=None)
    p.add_argument("--api-secret",default=None)
    p.add_argument("--config",help="YAML config (optional)")
    p.add_argument("--source",default="ccxt",choices=["http","ccxt"])
    p.add_argument("--ccxt-exchange",default="bingx")
    p.add_argument("--ccxt-symbol-format",default="usdtm",choices=["spot","usdtm"])
    p.add_argument("--symbols",default="",help="Comma list; empty means auto")
    p.add_argument("--symbols-file",default="",help="Path to whitelist file, one SYMBOL per line (e.g., BTC-USDT)")
    p.add_argument("--symbols-cache",dest="symbols_cache",default="symbols_cache.json")
    p.add_argument("--refresh-symbols-cache",action="store_true")
    p.add_argument("--symbols-cache-ttl-sec",type=int,default=0,help="0 = no TTL, else refresh if older than TTL")
    p.add_argument("--symbols-include",nargs="*",default=[],help="regex patterns to include (space-separated)")
    p.add_argument("--symbols-exclude",nargs="*",default=[],help="regex patterns to exclude (space-separated)")
    p.add_argument("--no-exclude-multipliers",action="store_true",
                   help="Exclude tickers starting with a digit by default (set to include them).")
    p.add_argument("--quote",default="USDT")
    p.add_argument("--max-universe",type=int,default=100)
    p.add_argument("--papertrade",action="store_true")
    p.add_argument("--live",action="store_true")
    p.add_argument("--force-start",action="store_true")
    p.add_argument("--force-start-always",action="store_true")
    p.add_argument("--reset-state",action="store_true")
    p.add_argument("--debug",action="store_true")
    p.add_argument("--no-telegram",action="store_true",help="Disable Telegram notifications")
    p.add_argument("--timeframe",default="1h")
    p.add_argument("--limit-klines",type=int,default=150)
    p.add_argument("--top-n",type=int,default=6,dest="top_n")
    p.add_argument("--min-qv-24h",type=float,default=1_000_000)
    p.add_argument("--min-qv-1h",type=float,default=50_000)
    p.add_argument("--max-atr-ratio",type=float,default=0.05)
    p.add_argument("--min-rsi",type=float,default=60.0)
    p.add_argument("--min-stoch",type=float,default=50.0)
    p.add_argument("--min-mfi",type=float,default=50.0)
    p.add_argument("--require-at-least-n-high",type=int,default=2)
    p.add_argument("--min-ob",type=float,default=70.0)
    p.add_argument("--w-rsi",type=float,default=0.4)
    p.add_argument("--w-stoch",type=float,default=0.3)
    p.add_argument("--w-mfi",type=float,default=0.3)
    p.add_argument("--w-hcp",type=float,default=0.0)
    p.add_argument("--position-notional",type=float,default=5.0)
    p.add_argument("--max-notional-frac",type=float,default=0.5)
    p.add_argument("--initial-equity",type=float,default=18.0)
    p.add_argument("--slippage-per-side",type=float,default=0.0003)
    p.add_argument("--fee-rate",type=float,default=0.001)
    p.add_argument("--funding-rate-hour",type=float,default=0.00002)
    p.add_argument("--sl-trigger",default="last",choices=["last","mark"])
    p.add_argument("--sl-last-offset",type=float,default=0.01)
    p.add_argument("--sl-mark-offset",type=float,default=0.0005)
    p.add_argument("--hold-hours",type=float,default=45)
    p.add_argument("--cooldown-days",type=float,default=5)
    p.add_argument("--workers",type=int,default=8,help="Threads for I/O (tickers & OHLCV)")
    p.add_argument("--presort-by",choices=["none","qv24"],default="qv24")
    p.add_argument("--presort-top",type=int,default=200,help="Keep only top-N by presort metric before deep scan")
    p.add_argument("--logfile",default="v12.log")
    p.add_argument("--trades-csv",default="v12_trades.csv")
    p.add_argument("--state-path",default="v12_state.json")
    # BingX signing
    p.add_argument("--bingx-demo",action="store_true",help="Use demo domain open-api-vst.bingx.com")
    p.add_argument("--recv-window",type=int,default=5000,help="BingX recvWindow (ms) for signed requests")
    return p

def yaml_merge(args: argparse.Namespace, parser: argparse.ArgumentParser) -> argparse.Namespace:
    if not getattr(args,"config",None) or not yaml: return args
    path=args.config
    if not path or not os.path.exists(path): return args
    try: conf_raw=yaml.safe_load(open(path,"r",encoding="utf-8")) or {}
    except Exception as e: log(f"[warn] YAML parse failed: {e}", args.logfile); return args
    conf={(k.replace("-","_")):v for k,v in conf_raw.items()}
    defaults=parser.parse_args([]); applied=[]
    for k,v in conf.items():
        if hasattr(args,k) and getattr(args,k)==getattr(defaults,k):
            setattr(args,k,v); applied.append(k)
    if getattr(args,"debug",False) and applied:
        log(f"[yaml] applied: {sorted(applied)}", args.logfile)
    return args

def main():
    parser=build_arg_parser()
    args=parser.parse_args()

    # env file
    envf=(args.env_file or "").strip()
    if envf.lower() in ("","none","null"): envf=None
    load_env_from_file(envf)

    if args.api_key: os.environ["BINGX_KEY"]=args.api_key
    if args.api_secret: os.environ["BINGX_SECRET"]=args.api_secret

    args.ccxt_fetcher=None
    if args.source=="ccxt":
        try:
            args.ccxt_fetcher=CCXTFetcher(exchange=args.ccxt_exchange,
                                          symbol_format=args.ccxt_symbol_format,
                                          debug=args.debug, logfile=args.logfile)
        except Exception as e:
            log(f"[ccxt-init] {e} — fallback to HTTP", args.logfile); args.source="http"

    if args.config: args=yaml_merge(args, parser)

    if args.reset_state and os.path.exists(args.state_path):
        try: os.remove(args.state_path)
        except: pass

    state=load_state(args.state_path)

    log(f"Bot v{VERSION} starting…", args.logfile)
    log(f'API: key="{mask(os.getenv("BINGX_KEY",""))}", secret="{mask(os.getenv("BINGX_SECRET",""))}"', args.logfile)

    if args.force_start_always or args.force_start:
        log(f"[force-start] Running immediate scan & open... (workers={args.workers}, presort={args.presort_by}, top={args.presort_top})", args.logfile)
        opened=run_once(args, state); log(f"[force-start] opened={opened}", args.logfile)
    else:
        log("[hb] idle...", args.logfile)

    close_expired_positions(args, state)

if __name__=="__main__":
    try: main()
    except KeyboardInterrupt: log("Interrupted.")
    except Exception as e: log(f"Fatal: {e}"); traceback.print_exc()
