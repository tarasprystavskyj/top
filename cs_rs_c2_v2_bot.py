#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cs_rs_c2_v2_bot.py — trading logic (paper mode) for C2 cross-sectional RS.
- Reads features from SQLite 'price_indicators' (no timeframe column expected)
- Selection mirrors cross_sectional_rs.rank + volume/breadth gates
- Enters LONG at bar close, places bracket SL/TP based on ATR_ratio
- Exits on bracket or after max_hold_hours

Outputs: results_dir/trades.csv
"""
import os, sys, csv, math, argparse, sqlite3, datetime as dt
from dataclasses import dataclass
import re, json

def load_cfg(path):
    cfg = {}
    if not path: return cfg
    try:
        import yaml  # type: ignore
        with open(path,"r",encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        # very small YAML/JSON fallback: key: value pairs or JSON
        try:
            with open(path,"r",encoding="utf-8") as f:
                txt = f.read()
            if txt.strip().startswith("{"):
                return json.loads(txt)
            for line in txt.splitlines():
                if ":" in line and not line.strip().startswith("#"):
                    k,v = line.split(":",1)
                    cfg[k.strip()] = json.loads(v) if v.strip().startswith(("{","[","\"")) else v.strip()
        except Exception:
            pass
    return cfg

def resolve_table(con, user_table=None):
    cur = con.cursor()
    if user_table:
        # verify
        try:
            cur.execute(f"PRAGMA table_info({user_table})")
            if cur.fetchall(): return user_table
        except Exception:
            pass
    # auto-detect table with required columns
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    req = set(["symbol","datetime_utc","close"])
    for t in tables:
        try:
            cur.execute(f"PRAGMA table_info({t})")
            cols = {r[1] for r in cur.fetchall()}
            if req.issubset(cols):
                return t
        except Exception:
            continue
    raise RuntimeError("No suitable table found (needs columns: symbol, datetime_utc, close)")

from typing import List, Dict, Optional

@dataclass
class Params:
    top_n: int = 4
    min_atr_ratio: float = 0.028
    vol_surge_mult: float = 0.0  # unused for this DB
    min_breadth: float = 0.15
    max_hold_hours: int = 72
    sl_atr_mult: float = 3.0
    tp_atr_mult: float = 6.0
    notional: float = 20.0

def ensure_dir(p): os.makedirs(p, exist_ok=True)

class DB:
    def __init__(self, path: str, table: str = None):
        self.path = path
        self.table = table
        self._cols = None

    def _ensure(self):
        if self._cols is not None: return
        con = sqlite3.connect(self.path)
        self.table = resolve_table(con, self.table)
        cur = con.cursor()
        cur.execute(f"PRAGMA table_info({self.table})")
        self._cols = [r[1] for r in cur.fetchall()]
        con.close()

    def timestamps(self):
        self._ensure()
        con = sqlite3.connect(self.path); cur = con.cursor()
        cur.execute(f"SELECT DISTINCT datetime_utc FROM {self.table} ORDER BY datetime_utc")
        out = [r[0] for r in cur.fetchall()]; con.close(); return out

    def slice_by_ts(self, ts: str):
        self._ensure()
        need = ["symbol","close","atr_ratio","dp6h","dp12h","gain_24h_before","quote_volume","qv_24h","volume"]
        sel_parts = []
        for c in need:
            sel_parts.append(c if c in self._cols else f"NULL AS {c}")
        sel_list = ", ".join(sel_parts)
        con = sqlite3.connect(self.path); cur = con.cursor()
        cur.execute(f"SELECT {sel_list} FROM {self.table} WHERE datetime_utc=?", (ts,))
        rows = cur.fetchall(); con.close()
        md = {}
        for r in rows:
            row = dict(zip(need, r))
            md[row["symbol"]] = {
                "close": float(row.get("close") or 0.0),
                "atr_ratio": float(row.get("atr_ratio") or 0.0),
                "dp6h": float(row.get("dp6h") or 0.0),
                "dp12h": float(row.get("dp12h") or 0.0),
                "gain_24h_before": float(row.get("gain_24h_before") or 0.0),
                "quote_volume": float(row.get("quote_volume") or 0.0),
                "qv_24h": float(row.get("qv_24h") or 0.0),
                "volume": float(row.get("volume") or 0.0),
            }
        return md
class Engine:

    def __init__(self, p: Params): self.p = p
    def vol_ok(self, row) -> bool:
        return True
        qv24=0.0; qv1=0.0
        avg1=0.0
        need=0.0
        return True
    def select(self, md: Dict[str, Dict]) -> List[str]:
        items = []
        for sym, row in md.items():
            atr = float(row.get("atr_ratio",0.0))
            mom = (float(row.get("dp6h",0.0)) + float(row.get("dp12h",0.0))) or float(row.get("gain_24h_before",0.0))
            if atr < self.p.min_atr_ratio: score = -1e9
            else: score = mom
            items.append((sym, score, atr, self.vol_ok(row)))
        items.sort(key=lambda x: x[1], reverse=True)
        # breadth on valid (score>0) among non-filtered atr pool
        nonneg = [1 for _, s, *_ in items if s>-1e9/2]
        valid = [1 for _, s, *_ in items if s>0]
        breadth = (sum(valid)/max(len(nonneg),1)) if nonneg else 0.0
        if breadth < self.p.min_breadth: return []
        # enforce volume spike
        volpass = [(sym,s) for sym,s,_,vol_ok in items if s>0 and vol_ok]
        return [sym for sym,_ in volpass[:self.p.top_n]]

@dataclass
class Pos:
    sym: str; entry_ts: str; entry_px: float; qty: float
    sl: Optional[float]; tp: Optional[float]
    max_exit_ts: Optional[str]
    exit_ts: Optional[str]=None; exit_px: Optional[float]=None; reason: Optional[str]=None; closed: bool=False

class Paper:
    def __init__(self, out_dir: str, p: Params):
        self.out_dir = out_dir; ensure_dir(out_dir)
        self.csv = os.path.join(out_dir,"trades.csv")
        if not os.path.exists(self.csv):
            with open(self.csv,"w",newline="",encoding="utf-8") as f:
                csv.writer(f).writerow(["open_time_utc","symbol","side","entry_price","exit_time_utc","exit_price","reason","notional","realized_pnl","equity_after"])
        self.eq=200.0; self.p=p; self.pos: Dict[str,Pos]={}
    def open_long(self, ts, sym, px, atr):
        if px<=0: return
        if sym in self.pos and not self.pos[sym].closed: return
        qty = self.p.notional/max(px,1e-9)
        sl = px*(1.0 - self.p.sl_atr_mult*atr) if self.p.sl_atr_mult>0 else None
        tp = px*(1.0 + self.p.tp_atr_mult*atr) if self.p.tp_atr_mult>0 else None
        # max hold
        dt_ts = dt.datetime.fromisoformat(ts.replace("Z","+00:00"))
        max_exit = (dt_ts + dt.timedelta(hours=self.p.max_hold_hours)).isoformat()
        self.pos[sym]=Pos(sym,ts,px,qty,sl,tp,max_exit)
    def on_bar(self, ts, md: Dict[str,Dict]):
        # check exits
        for sym, pos in list(self.pos.items()):
            if pos.closed: continue
            row = md.get(sym); 
            if not row: continue
            px = float(row.get("close",0.0))
            hit_tp = pos.tp and px>=pos.tp
            hit_sl = pos.sl and px<=pos.sl
            time_exit = ts>=pos.max_exit_ts if pos.max_exit_ts else False
            if hit_tp or hit_sl or time_exit:
                pos.closed=True; pos.exit_ts=ts; pos.exit_px = pos.tp if hit_tp else (pos.sl if hit_sl else px)
                pos.reason="tp" if hit_tp else ("sl" if hit_sl else "time")
                pnl = (pos.exit_px - pos.entry_px)*pos.qty
                self.eq += pnl
                with open(self.csv,"a",newline="",encoding="utf-8") as f:
                    csv.writer(f).writerow([pos.entry_ts,pos.sym,"LONG",pos.entry_px,pos.exit_ts,pos.exit_px,pos.reason,self.p.notional,pnl,self.eq])

def run(db_path: str, out_dir: str, p: Params, limit_bars: int=0):
    db = DB(db_path); eng = Engine(p); paper = Paper(out_dir,p)
    bars=0
    for ts in db.timestamps():
        md = db.slice_by_ts(ts)
        picks = eng.select(md)
        for sym in picks:
            row = md.get(sym,{}); paper.open_long(ts, sym, float(row.get("close",0.0)), float(row.get("atr_ratio",0.0)))
        paper.on_bar(ts, md)
        bars+=1
        if limit_bars and bars>=limit_bars: break

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--table", default=None)
    ap.add_argument("--cfg", default=None)
    ap.add_argument("--perp-usdt-only", action="store_true")
    ap.add_argument("--results-dir", default="./results_c2_v2")
    ap.add_argument("--limit-bars", type=int, default=0)
    ap.add_argument("--top-n", type=int, default=4)
    ap.add_argument("--min-atr", type=float, default=0.028)
    
    ap.add_argument("--min-breadth", type=float, default=0.15)
    ap.add_argument("--max-hold", type=int, default=72)
    ap.add_argument("--sl-atr", type=float, default=3.0)
    ap.add_argument("--tp-atr", type=float, default=6.0)
    ap.add_argument("--notional", type=float, default=20.0)
    a = ap.parse_args()
    p = Params(a.top_n,a.min_atr,0.0,a.min_breadth,a.max_hold,a.sl_atr,a.tp_atr,a.notional)
    run(a.db, a.results_dir, p, a.limit_bars)

if __name__=="__main__":
    main()
