# common.py — utilities (colors, CCXT, DB, heat-stats)
import os
import sys
import json
import time
import math
import importlib
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import pandas as pd
import numpy as np


# Live-only switch: ignore vol_surge filter by default
IGNORE_VOLSURGE = os.environ.get('BT_IGNORE_VOLSURGE', '1') == '1'

# ---------- colors ----------
_NO_COLOR = (os.environ.get("NO_COLOR") is not None and os.environ.get("NO_COLOR") != '0')
_FORCE_COLOR = os.environ.get("FORCE_COLOR") is not None
_TTY = (_FORCE_COLOR or sys.stdout.isatty() or sys.stderr.isatty())

def _ansi(code: str) -> str:
    return f"\033[{code}m"

def _st(text: str, *, fg: str = "", bold: bool = False, dim: bool = False) -> str:
    if _NO_COLOR or not _TTY:
        return text
    codes = []
    if bold:
        codes.append("1")
    if dim:
        codes.append("2")
    fg_map = {
        "gray": "90", "red": "31", "green": "32", "yellow": "33",
        "blue": "34", "magenta": "35", "cyan": "36", "white": "37",
    }
    if fg:
        codes.append(fg_map.get(fg, ""))
    codes = [c for c in codes if c]
    if not codes:
        return text
    return f"{_ansi(';'.join(codes))}{text}{_ansi('0m')}"

def cprint(*parts, fg: str = "", bold: bool = False, dim: bool = False, file=None, end="\n", flush=False):
    s = " ".join(str(p) for p in parts)
    s = _st(s, fg=fg, bold=bold, dim=dim)
    print(s, file=file, end=end, flush=flush)

def _format_float_short(val):
    try:
        f = float(val)
    except Exception:
        return str(val)
    try:
        if not math.isfinite(f):
            return str(f)
    except Exception:
        return str(f)
    if abs(f) >= 1000 or (0 < abs(f) < 1e-4):
        return f"{f:.3e}"
    return f"{f:.4f}"


def _format_dict_short(data):
    if not isinstance(data, dict) or not data:
        return "{}" if not data else str(data)
    parts = []
    for key in sorted(data.keys()):
        parts.append(f"{key}:{_format_float_short(data[key])}")
    return "{" + ", ".join(parts) + "}"

def dot():
    print(".", end="", flush=True)

try:
    import yaml
except Exception:
    yaml = None

# ---------- optional engine imports ----------
def _try_import(mod_path, names: List[str]):
    mod = importlib.import_module(mod_path)
    return [getattr(mod, n) for n in names]

EnginePortfolio = None
build_md_slice = None
load_cache = None
try:
    EnginePortfolio, = _try_import("engine.portfolio", ["Portfolio"])
    build_md_slice, load_cache = _try_import("engine.data", ["build_md_slice", "load_cache"])
except Exception:
    EnginePortfolio = None
    build_md_slice = None
    load_cache = None

# ---------- helpers ----------
def load_yaml_or_json(path: str) -> dict:
    if not path:
        return {}
    try:
        if path.endswith(".json"):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        if yaml is not None:
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception as e:
        cprint("[cfg] failed to parse", path, ":", e, fg="red")
    cfg = {}
    try:
        for line in open(path, "r", encoding="utf-8").read().splitlines():
            if ':' in line and not line.strip().startswith('#'):
                k, v = line.split(':', 1)
                vv = v.strip()
                if vv.lower() in ('true', 'false'):
                    cfg[k.strip()] = (vv.lower() == 'true')
                else:
                    try:
                        if '.' in vv or 'e' in vv.lower():
                            cfg[k.strip()] = float(vv)
                        else:
                            cfg[k.strip()] = int(vv)
                    except Exception:
                        cfg[k.strip()] = vv
    except Exception:
        pass
    return cfg

def mask(s: str) -> str:
    s = str(s or '')
    if len(s) <= 4:
        return '*' * len(s)
    return s[:2] + '*' * (len(s) - 4) + s[-2:]

def sleep_ms(ms: int):
    time.sleep(max(0.0, float(ms) / 1000.0))

def _tf_to_seconds(tf: str) -> int:
    tf = (tf or "1h").strip().lower()
    unit = tf[-1]
    try:
        n = int(tf[:-1])
    except Exception:
        n = 1
    mult = {'m': 60, 'h': 3600, 'd': 86400, 'w': 7*86400}
    return n * mult.get(unit, 3600)

def _align_bar_close(now_dt, tf_seconds: int):
    epoch = int(now_dt.timestamp())
    aligned = epoch - (epoch % tf_seconds)
    return datetime.fromtimestamp(aligned, tz=timezone.utc)

# ---------- feature engineering ----------
def compute_feats(df: pd.DataFrame, tf_seconds: Optional[int] = None) -> pd.DataFrame:
    out = df.copy()
    prev_close = out['close'].shift(1)
    tr = pd.concat([
        (out['high'] - out['low']).abs(),
        (out['high'] - prev_close).abs(),
        (out['low'] - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14.0, adjust=False).mean()
    out['atr_ratio'] = (atr / out['close']).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    out['quote_volume'] = (out['volume'] * out['close']).fillna(0.0)

    if tf_seconds is None:
        out['qv_24h'] = out['quote_volume'].rolling(24, min_periods=1).sum()
        out['dp6h']  = (out['close'] / out['close'].shift(6)  - 1.0).fillna(0.0)
        out['dp12h'] = (out['close'] / out['close'].shift(12) - 1.0).fillna(0.0)
        if IGNORE_VOLSURGE:
            out['vol_surge_mult'] = 1e9
        else:
            avg1 = out['qv_24h'] / 24.0
            with np.errstate(divide='ignore', invalid='ignore'):
                out['vol_surge_mult'] = np.where(avg1 > 0, out['quote_volume'] / avg1, 0.0)
    else:
        bars_24h = max(1, int(round(24*3600 / tf_seconds)))
        bars_6h  = max(1, int(round( 6*3600 / tf_seconds)))
        bars_12h = max(1, int(round(12*3600 / tf_seconds)))
        out['qv_24h'] = out['quote_volume'].rolling(bars_24h, min_periods=1).sum()
        out['dp6h']  = (out['close'] / out['close'].shift(bars_6h)  - 1.0).fillna(0.0)
        out['dp12h'] = (out['close'] / out['close'].shift(bars_12h) - 1.0).fillna(0.0)
        if IGNORE_VOLSURGE:
            out['vol_surge_mult'] = 1e9
        else:
            avg_per_bar = out['qv_24h'] / float(bars_24h)
            with np.errstate(divide='ignore', invalid='ignore'):
                out['vol_surge_mult'] = np.where(avg_per_bar > 0, out['quote_volume'] / avg_per_bar, 0.0)
    for k in ('rsi','stochastic','mfi','overbought_index','gain_24h_before'):
        if k not in out.columns:
            out[k] = 0.0
    return out

# ---------- CCXT ----------
try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None

RATE_MS = 130

class CCXTFetcher:
    def __init__(self, exchange='bingx', symbol_format='usdtm', debug=False, logfile=''):
        if not ccxt:
            raise RuntimeError("ccxt is not installed. pip install 'ccxt<5'")
        self.debug = debug
        self.logfile = logfile
        api_k = os.environ.get('BINGX_KEY') or os.environ.get('API_KEY')
        api_s = os.environ.get('BINGX_SECRET') or os.environ.get('API_SECRET')
        opts = {'enableRateLimit': True, 'timeout': 20000}
        if api_k and api_s:
            opts.update({'apiKey': api_k, 'secret': api_s})
        self.ex = getattr(ccxt, exchange)(opts)
        try:
            self.markets = self.ex.load_markets()
        except Exception as e:
            self.markets = {}
            cprint("[ccxt load_markets]", e, fg="red")

        self.by_base: Dict[str, str] = {}
        for m in self.markets.values():
            try:
                if m.get('swap') and m.get('quote') == 'USDT':
                    b = m.get('base')
                    if b:
                        self.by_base[b] = m['symbol']
            except Exception:
                continue

    def resolve_symbol(self, s: str) -> Optional[str]:
        if s in self.markets:
            return s
        if s in self.by_base:
            return self.by_base[s]
        u = s.upper().replace('-', '/').replace('USDTUSDT', 'USDT:USDT')
        u = u.replace(':USDTUSDT', ':USDT')
        for cand in (u, u.replace('/USDT', '/USDT:USDT'), u.replace('/USDT:USDT', '/USDT')):
            if cand in self.markets:
                return cand
        b = s.split('/', 1)[0].split('-', 1)[0].replace('USDT','')
        if b in self.by_base:
            return self.by_base[b]
        return None

    def _choose_fetch_tf(self, req_tf: str):
        try:
            exchange_tfs = set(getattr(self.ex, 'timeframes', {}) or {})
        except Exception:
            exchange_tfs = set()
        fetch_tf = req_tf
        agg = 1
        if exchange_tfs and req_tf not in exchange_tfs:
            def sec(tf):
                try:
                    return int(self.ex.parse_timeframe(tf))
                except Exception:
                    return _tf_to_seconds(tf)
            s_req = sec(req_tf)
            cands = []
            for tf in exchange_tfs:
                s = sec(tf)
                if s <= s_req and s_req % s == 0:
                    cands.append((s, tf))
            if cands:
                cands.sort()
                s_fetch, fetch_tf = cands[-1]
                agg = s_req // s_fetch
            else:
                smaller = [(sec(tf), tf) for tf in exchange_tfs if sec(tf) < s_req]
                if smaller:
                    smaller.sort()
                    s_fetch, fetch_tf = smaller[-1]
                    agg = max(1, s_req // s_fetch)
        return fetch_tf, agg

    def fetch_ohlcv_df(self, sym: str, timeframe='1h', limit=180) -> Optional[pd.DataFrame]:
        ccxt_sym = self.resolve_symbol(sym) or sym
        fetch_tf, agg = self._choose_fetch_tf(timeframe)
        try:
            data = self.ex.fetch_ohlcv(ccxt_sym, timeframe=fetch_tf, limit=int(limit)*int(agg)+5)
            sleep_ms(RATE_MS)
        except Exception as e:
            cprint("[fetch_ohlcv]", sym, ":", e, fg="red")
            return None
        if not data:
            return None
        df = pd.DataFrame(data, columns=['ts','open','high','low','close','volume'])
        if agg > 1:
            slot_ms = _tf_to_seconds(timeframe) * 1000
            df['slot'] = (df['ts'] // slot_ms) * slot_ms
            df = df.groupby('slot', as_index=False).agg({
                'ts':'max','open':'first','high':'max','low':'min','close':'last','volume':'sum'
            })
        df['datetime_utc'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
        df = df.set_index('datetime_utc')[['open','high','low','close','volume']].astype(float)
        return df.sort_index().tail(limit)

    def fetch_ticker_price(self, sym: str) -> Optional[float]:
        ccxt_sym = self.resolve_symbol(sym) or sym
        try:
            t = self.ex.fetch_ticker(ccxt_sym)
            sleep_ms(RATE_MS)
            p = float(t.get('last') or t.get('close') or 0.0)
            return p if p > 0 else None
        except Exception as e:
            cprint("[ticker]", sym, ":", e, fg="red")
            return None

    def fetch_mark_price(self, sym: str) -> Optional[float]:
        ccxt_sym = self.resolve_symbol(sym) or sym

        def _extract_mark(obj) -> Optional[float]:
            if not isinstance(obj, dict):
                return None
            for key in (
                'markPrice',
                'mark_price',
                'indexPrice',
                'index_price',
                'lastMarkPrice',
                'last_mark_price',
                'marketPrice',
                'market_price',
            ):
                val = obj.get(key)
                if val in (None, ''):
                    continue
                try:
                    fv = float(val)
                    if math.isfinite(fv):
                        return fv
                except Exception:
                    continue
            return None

        try:
            fetch_fn = getattr(self.ex, 'fetch_mark_price', None)
            if callable(fetch_fn):
                data = fetch_fn(ccxt_sym)
                sleep_ms(RATE_MS)
                if isinstance(data, list):
                    data = data[0] if data else {}
                price = _extract_mark(data)
                if price is None and isinstance(data, dict):
                    info = data.get('info')
                    if isinstance(info, dict):
                        price = _extract_mark(info)
                if price is not None and price > 0:
                    return price
        except Exception as e:
            if self.debug:
                cprint('[mark.fetch]', sym, ':', e, fg='yellow', dim=True)

        try:
            t = self.ex.fetch_ticker(ccxt_sym)
            sleep_ms(RATE_MS)
        except Exception as e:
            cprint('[mark.ticker]', sym, ':', e, fg='yellow')
            return None

        price = _extract_mark(t)
        if price is None and isinstance(t, dict):
            info = t.get('info')
            if isinstance(info, dict):
                price = _extract_mark(info)
        try:
            if price is not None and price > 0 and math.isfinite(float(price)):
                return float(price)
        except Exception:
            pass
        return None

# ---------- Orders table helpers ----------
def ensure_orders_db(path: str):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS orders(
        order_id TEXT PRIMARY KEY,
        ts_utc TEXT,
        bar_time_utc TEXT,
        mode TEXT,
        symbol TEXT,
        side TEXT,
        type TEXT,
        price REAL,
        qty REAL,
        status TEXT,
        reason TEXT,
        run_id TEXT,
        extra TEXT
    )''')
    con.commit()
    con.close()

def insert_order_row(db_path: str, row: dict):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cols = ['order_id','ts_utc','bar_time_utc','mode','symbol','side','type',
            'price','qty','status','reason','run_id','extra']
    vals = [row.get(c) for c in cols]
    cur.execute(f"INSERT OR REPLACE INTO orders({','.join(cols)}) VALUES({','.join(['?']*len(cols))})", vals)
    con.commit()
    con.close()

# ---------- Session DB & Cache-out helpers ----------
def ensure_session_dbs(results_dir: str, session_db: str = '', cache_out: str = ''):
    sess = session_db or os.path.join(results_dir, 'session.sqlite')
    cachep = cache_out or os.path.join(results_dir, 'combined_cache_session.db')
    os.makedirs(os.path.dirname(sess) or '.', exist_ok=True)
    os.makedirs(os.path.dirname(cachep) or '.', exist_ok=True)

    con = sqlite3.connect(sess)
    cur = con.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS config_snapshots(
        run_id TEXT, ts_utc TEXT, cfg_json TEXT, PRIMARY KEY(run_id, ts_utc)
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS decisions(
        run_id TEXT, bar_time_utc TEXT, universe_size INTEGER, ranked_json TEXT, selected_json TEXT,
        PRIMARY KEY(run_id, bar_time_utc)
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS equity(
        run_id TEXT, ts_utc TEXT, equity_usdt REAL, cash_usdt REAL, position_value_usdt REAL,
        realized_pnl_cum REAL, unrealized_pnl REAL,
        PRIMARY KEY(run_id, ts_utc)
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS open_positions(
        bot_id TEXT,
        symbol TEXT,
        side TEXT,
        qty REAL,
        entry REAL,
        tp_price REAL,
        sl_price REAL,
        ts_open TEXT,
        run_id TEXT,
        local_order_uuid TEXT,
        exchange_order_id TEXT,
        exchange TEXT,
        timeframe TEXT,
        status TEXT,
        ts_close TEXT,
        entry_fill REAL,
        entry_fill_ts TEXT,
        exit_fill REAL,
        exit_fill_ts TEXT,
        entry_slip_bp REAL,
        entry_lag_sec REAL,
        exit_slip_bp REAL,
        exit_lag_sec REAL,
        entry_mark_price REAL,
        exit_mark_price REAL,
        PRIMARY KEY(bot_id, symbol, local_order_uuid)
    )''')

    # ensure new columns for older databases
    try:
        cols = [r[1] for r in cur.execute("PRAGMA table_info(open_positions)").fetchall()]
        add_cols = [
            ("entry_fill", "REAL"),
            ("entry_fill_ts", "TEXT"),
            ("exit_fill", "REAL"),
            ("exit_fill_ts", "TEXT"),
            ("entry_slip_bp", "REAL"),
            ("entry_lag_sec", "REAL"),
            ("exit_slip_bp", "REAL"),
            ("exit_lag_sec", "REAL"),
            ("entry_mark_price", "REAL"),
            ("exit_mark_price", "REAL"),
        ]
        for name, typ in add_cols:
            if name not in cols:
                cur.execute(f"ALTER TABLE open_positions ADD COLUMN {name} {typ}")
    except Exception:
        pass

    con.commit()
    con.close()

    con2 = sqlite3.connect(cachep)
    cur2 = con2.cursor()
    cur2.execute('''CREATE TABLE IF NOT EXISTS price_indicators(
        symbol TEXT, datetime_utc TEXT,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        rsi REAL, stochastic REAL, mfi REAL, overbought_index REAL,
        atr_ratio REAL, gain_24h_before REAL, dp6h REAL, dp12h REAL,
        quote_volume REAL, qv_24h REAL, vol_surge_mult REAL,
        PRIMARY KEY(symbol, datetime_utc)
    )''')
    cur2.execute('''CREATE TABLE IF NOT EXISTS heat_stats(
        datetime_utc TEXT,
        mode TEXT,
        symbol TEXT,
        combined_gap REAL,
        heat REAL,
        gap_atr REAL,
        gap_volsurge REAL,
        gap_qv24 REAL,
        gap_qv1h REAL,
        gap_momentum REAL,
        gap_breadth REAL,
        a_atr_ratio REAL,
        th_min_atr_ratio REAL,
        a_qv_24h REAL,
        th_min_qv_24h REAL,
        a_qv_1h REAL,
        th_min_qv_1h REAL,
        a_momentum REAL,
        th_min_momentum REAL,
        a_breadth REAL,
        th_min_breadth REAL,
        extra_json TEXT,
        PRIMARY KEY(datetime_utc, mode, symbol)
    )''')
    con2.commit()
    con2.close()
    return sess, cachep

def write_config_snapshot(sess_path: str, run_id: str, cfg: dict):
    con = sqlite3.connect(sess_path)
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO config_snapshots(run_id, ts_utc, cfg_json) VALUES(?,?,?)",
                (run_id, datetime.utcnow().isoformat(), json.dumps(cfg)))
    con.commit()
    con.close()

def write_decisions(sess_path: str, run_id: str, bar_time, ranked_list, selected_list):
    con = sqlite3.connect(sess_path)
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO decisions(run_id, bar_time_utc, universe_size, ranked_json, selected_json) VALUES(?,?,?,?,?)",
                (run_id, bar_time.isoformat(), int(len(ranked_list)), json.dumps(list(ranked_list)), json.dumps(list(selected_list))))
    con.commit()
    con.close()

def write_equity(sess_path: str, run_id: str, t, equity_dict: dict):
    con = sqlite3.connect(sess_path)
    cur = con.cursor()
    cur.execute('''INSERT OR REPLACE INTO equity(run_id, ts_utc, equity_usdt, cash_usdt, position_value_usdt,
                    realized_pnl_cum, unrealized_pnl)
                    VALUES(?,?,?,?,?,?,?)''',
                (run_id, t.isoformat(),
                 float(equity_dict.get('equity', 0.0)),
                 float(equity_dict.get('cash', 0.0)),
                 float(equity_dict.get('position_value', 0.0)),
                 float(equity_dict.get('realized_pnl_cum', 0.0)),
                 float(equity_dict.get('unrealized_pnl', 0.0))))
    con.commit()
    con.close()

def cache_out_upsert(cache_path: str, symbol: str, feats_df: pd.DataFrame):
    con = sqlite3.connect(cache_path)
    cur = con.cursor()
    cols = ['symbol','datetime_utc','open','high','low','close','volume',
            'rsi','stochastic','mfi','overbought_index','atr_ratio','gain_24h_before',
            'dp6h','dp12h','quote_volume','qv_24h','vol_surge_mult']
    placeholders = ','.join(['?'] * len(cols))
    for idx, r in feats_df.iterrows():
        row = [
            symbol,
            pd.to_datetime(idx, utc=True).strftime('%Y-%m-%dT%H:%M:%S+00:00'),
            float(r.get('open', 0.0)), float(r.get('high', 0.0)), float(r.get('low', 0.0)),
            float(r.get('close', 0.0)), float(r.get('volume', 0.0)),
            float(r.get('rsi', 0.0)), float(r.get('stochastic', 0.0)), float(r.get('mfi', 0.0)), float(r.get('overbought_index', 0.0)),
            float(r.get('atr_ratio', 0.0)), float(r.get('gain_24h_before', 0.0)),
            float(r.get('dp6h', 0.0)), float(r.get('dp12h', 0.0)),
            float(r.get('quote_volume', 0.0)), float(r.get('qv_24h', 0.0)), float(r.get('vol_surge_mult', 0.0))
        ]
        cur.execute(f"INSERT OR REPLACE INTO price_indicators({','.join(cols)}) VALUES({placeholders})", row)
    con.commit()
    con.close()

def read_hour_cache_row(cache_path: str, symbol: str, dt_utc) -> dict:
    try:
        con = sqlite3.connect(cache_path)
        cur = con.cursor()
        ts = dt_utc.isoformat()
        row = cur.execute(
            "SELECT open,high,low,close,volume,rsi,stochastic,mfi,overbought_index,atr_ratio,gain_24h_before,dp6h,dp12h,quote_volume,qv_24h,vol_surge_mult FROM price_indicators WHERE symbol=? AND datetime_utc=? LIMIT 1",
            (symbol, ts)
        ).fetchone()
        con.close()
        if not row:
            return {}
        keys = ['open','high','low','close','volume','rsi','stochastic','mfi','overbought_index','atr_ratio','gain_24h_before','dp6h','dp12h','quote_volume','qv_24h','vol_surge_mult']
        return {k: float(v) for k, v in zip(keys, row)}
    except Exception:
        return {}

# ---------- positions persistence ----------
def positions_state_path(results_dir: str) -> str:
    os.makedirs(results_dir, exist_ok=True)
    return os.path.join(results_dir, 'live_positions.json')

def load_positions(results_dir: str) -> Dict[str, dict]:
    path = positions_state_path(results_dir)
    if os.path.exists(path):
        try:
            return json.load(open(path, 'r', encoding='utf-8')) or {}
        except Exception:
            return {}
    return {}

def save_positions(results_dir: str, pos: Dict[str, dict]):
    path = positions_state_path(results_dir)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(pos, f, indent=2, ensure_ascii=False)

# ---------- per-bot open positions ----------
def make_bot_id(results_dir: str, exchange: str, timeframe: str) -> str:
    results_dir = os.path.abspath(results_dir or '.')
    return f"{results_dir}|{exchange}|{str(timeframe)}"

def db_load_open_positions(session_db_path: str, bot_id: str) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    try:
        con = sqlite3.connect(session_db_path)
        cur = con.cursor()
        rows = cur.execute(
            """
            SELECT symbol, side, qty, entry, tp_price, sl_price, ts_open, run_id,
                   local_order_uuid, exchange_order_id, exchange, timeframe,
                   entry_fill, entry_fill_ts, entry_slip_bp, entry_lag_sec,
                   entry_mark_price, exit_mark_price
            FROM open_positions
            WHERE bot_id=? AND status='OPEN'
            ORDER BY ts_open ASC
            """,
            (bot_id,)
        ).fetchall()
        con.close()
        for r in rows:
            (
                sym, side, qty, entry, tp, sl, ts_open, run_id,
                luid, exid, exch, tf, entry_fill, entry_fill_ts,
                entry_slip_bp, entry_lag_sec, entry_mark_price, exit_mark_price
            ) = r
            out[sym] = {
                'symbol': sym,
                'side': side,
                'qty': float(qty or 0.0),
                'entry': float(entry) if entry is not None else None,
                'tp_price': tp,
                'sl_price': sl,
                'ts_open': ts_open,
                'run_id': run_id,
                'order_id': luid,
                'exchange_order_id': exid,
                'exchange': exch,
                'timeframe': tf,
                'entry_fill': entry_fill,
                'entry_fill_ts': entry_fill_ts,
                'entry_slip_bp': entry_slip_bp,
                'entry_lag_sec': entry_lag_sec,
                'entry_mark_price': entry_mark_price,
                'exit_mark_price': exit_mark_price,
            }
    except Exception as e:
        cprint('[db load open_positions]', e, fg='red')
    return out

def db_upsert_open_position(session_db_path: str, bot_id: str, rec: dict):
    try:
        con = sqlite3.connect(session_db_path)
        cur = con.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO open_positions(
                bot_id, symbol, side, qty, entry, tp_price, sl_price, ts_open, run_id,
                local_order_uuid, exchange_order_id, exchange, timeframe, status, ts_close,
                entry_fill, entry_fill_ts, exit_fill, exit_fill_ts,
                entry_slip_bp, entry_lag_sec, exit_slip_bp, exit_lag_sec,
                entry_mark_price, exit_mark_price
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                bot_id,
                rec.get('symbol'),
                rec.get('side','LONG'),
                float(rec.get('qty',0.0)),
                float(rec.get('entry')) if rec.get('entry') is not None else None,
                rec.get('tp_price'),
                rec.get('sl_price'),
                rec.get('ts_open'),
                rec.get('run_id'),
                rec.get('order_id'),
                rec.get('exchange_order_id'),
                rec.get('exchange'),
                rec.get('timeframe'),
                rec.get('status','OPEN'),
                rec.get('ts_close'),
                rec.get('entry_fill'),
                rec.get('entry_fill_ts'),
                rec.get('exit_fill'),
                rec.get('exit_fill_ts'),
                rec.get('entry_slip_bp'),
                rec.get('entry_lag_sec'),
                rec.get('exit_slip_bp'),
                rec.get('exit_lag_sec'),
                rec.get('entry_mark_price'),
                rec.get('exit_mark_price')
            )
        )
        con.commit()
        con.close()
    except Exception as e:
        cprint('[db upsert open_positions]', e, fg='red')

def db_mark_closed(
    session_db_path: str,
    bot_id: str,
    local_order_uuid: str,
    ts_close_iso: str,
    *,
    exit_fill=None,
    exit_fill_ts=None,
    exit_slip_bp=None,
    exit_lag_sec=None,
    exit_mark_price=None,
):
    try:
        con = sqlite3.connect(session_db_path)
        cur = con.cursor()
        cur.execute(
            """
            UPDATE open_positions SET status='CLOSED', ts_close=?,
                exit_fill=?, exit_fill_ts=?, exit_slip_bp=?, exit_lag_sec=?,
                exit_mark_price=?
            WHERE bot_id=? AND local_order_uuid=?
            """,
            (
                ts_close_iso,
                exit_fill,
                exit_fill_ts,
                exit_slip_bp,
                exit_lag_sec,
                exit_mark_price,
                bot_id,
                local_order_uuid,
            ),
        )
        con.commit()
        con.close()
    except Exception as e:
        cprint('[db mark closed]', e, fg='red')

# ---------- heat-stats ----------
def save_heat_stats(cache_path: str, bar_time, mode_label: str, best: dict):
    try:
        if not cache_path:
            return
        rec_time = bar_time.isoformat()
        sym = best.get('symbol') if isinstance(best, dict) else None
        g = (best.get('gaps') if isinstance(best, dict) else {}) or {}
        th = (best.get('thresholds') if isinstance(best, dict) else {}) or {}
        a = (best.get('actuals') if isinstance(best, dict) else {}) or {}
        combined_gap = float(best.get('combined_gap', 1.0)) if isinstance(best, dict) else 1.0
        heat = max(0.0, 1.0 - combined_gap)
        row = (
            rec_time, str(mode_label or ''), str(sym or ''),
            combined_gap, heat,
            float(g.get('atr', 0.0)), float(g.get('volsurge', 0.0)),
            float(g.get('qv24', 0.0)), float(g.get('qv1h', 0.0)),
            float(g.get('momentum', 0.0)), float(g.get('breadth', 0.0)),
            float(a.get('atr_ratio', 0.0)), float(th.get('min_atr_ratio', 0.0)),
            float(a.get('qv_24h', 0.0)), float(th.get('min_qv_24h', 0.0)),
            float(a.get('qv_1h', 0.0)), float(th.get('min_qv_1h', 0.0)),
            float(a.get('mom_sum', 0.0)), float(th.get('min_momentum_sum', 0.0)),
            float(a.get('breadth', 0.0)), float(th.get('min_breadth', 0.0)),
            json.dumps(best, ensure_ascii=False)
        )
        con = sqlite3.connect(cache_path)
        cur = con.cursor()
        cur.execute('''INSERT OR REPLACE INTO heat_stats(
            datetime_utc, mode, symbol, combined_gap, heat,
            gap_atr, gap_volsurge, gap_qv24, gap_qv1h, gap_momentum, gap_breadth,
            a_atr_ratio, th_min_atr_ratio, a_qv_24h, th_min_qv_24h,
            a_qv_1h, th_min_qv_1h, a_momentum, th_min_momentum, a_breadth, th_min_breadth,
            extra_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', row)
        con.commit()
        con.close()
    except Exception as e:
        cprint("[heat save]", e, fg="red")

def print_and_save_heat_from_strategy(strat, mode_label: str, bar_close, md: dict, uni_syms, cache_path: str = None):
    try:
        best_entry_distance = getattr(strat, 'best_entry_distance', None)
        if best_entry_distance is None:
            return False
        symbols = list(uni_syms) if uni_syms else list(md.keys())
        best = best_entry_distance(bar_close, md, symbols=symbols)

        cprint(f"[{mode_label}] opened=0 at {bar_close.isoformat()}", fg="cyan")
        if not best:
            return True
        heat = max(0.0, 1.0 - float(best.get('combined_gap', 1.0)))
        g = best.get('gaps', {}) or {}
        th = best.get('thresholds', {}) or {}
        a = best.get('actuals', {}) or {}
        cprint(f"[heat] nearest={best.get('symbol')} heat={heat*100:.1f}% (lower gap is closer to entry)", fg="yellow", bold=True)
        reason = best.get('reason') or '-'
        cprint(f"       reason: {reason}", fg="yellow", dim=True)
        cprint(f"       gaps: {_format_dict_short(g)}", fg="yellow", dim=True)
        cprint(f"       actuals: {_format_dict_short(a)}", fg="yellow", dim=True)
        cprint(f"       thresholds: {_format_dict_short(th)}", fg="yellow", dim=True)
        if g.get('atr', 0.0) > 0:
            cprint(
                f"       min_atr_ratio {a.get('atr_ratio',0.0):.6f} vs {th.get('min_atr_ratio',0.0):.6f} -> gap {g.get('atr',0.0)*100:.1f}%",
                fg="yellow", dim=True)
        if g.get('volsurge', 0.0) > 0:
            if 'vol_surge_mult' in a and 'min_vol_surge_mult' in th:
                cprint(
                    f"       min_vol_surge_mult {a.get('vol_surge_mult',0.0):.2f} vs {th.get('min_vol_surge_mult',0.0):.2f} -> gap {g.get('volsurge',0.0)*100:.1f}%",
                    fg="yellow", dim=True)
            else:
                cprint(
                    f"       min_vol_surge_mult gap {g.get('volsurge',0.0)*100:.1f}%",
                    fg="yellow", dim=True)
        if g.get('qv24', 0.0) > 0:
            cprint(
                f"       min_qv_24h {a.get('qv_24h',0.0):.0f} vs {th.get('min_qv_24h',0.0):.0f} -> gap {g.get('qv24',0.0)*100:.1f}%",
                fg="yellow", dim=True)
        if g.get('qv1h', 0.0) > 0:
            cprint(
                f"       min_qv_1h  {a.get('qv_1h',0.0):.0f} vs {th.get('min_qv_1h',0.0):.0f} -> gap {g.get('qv1h',0.0)*100:.1f}%",
                fg="yellow", dim=True)
        if g.get('momentum', 0.0) > 0:
            cprint(
                f"       min_momentum_sum {a.get('mom_sum',0.0):.4f} vs {th.get('min_momentum_sum',0.0):.4f} -> gap {g.get('momentum',0.0)*100:.1f}%",
                fg="yellow", dim=True)
        if g.get('breadth', 0.0) > 0:
            cprint(
                f"       min_breadth  {a.get('breadth',0.0):.3f} vs {th.get('min_breadth',0.0):.3f} -> gap {g.get('breadth',0.0)*100:.1f}%",
                fg="yellow", dim=True)
        if cache_path:
            save_heat_stats(cache_path, bar_close, mode_label, best)
        return True
    except Exception:
        return False

def _print_heat_from_strategy(strat, mode_label: str, bar_close, md: dict, uni_syms):
    try:
        return print_and_save_heat_from_strategy(strat, mode_label, bar_close, md, uni_syms, cache_path=None)
    except Exception:
        return False
