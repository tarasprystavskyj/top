#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backtest parameter optimizer — v8.4

ЩО НОВОГО проти v8.3:
- Чіткий шлях до summary: {project_dir}/summary.csv і очікування on-change (mtime).
- Після КОЖНОГО прогона друк місячної/річної дохідності (annualized).
- Блокування тюнінгу limit_bars; лише strategy_params.* числові.
- Виправлене визначення періоду з SQLite/назви .db (1440 -> 60 днів).
- Збережений увесь попередній функціонал: PRERUN cache, кольори, вартість токенів (LLM),
  критик із gpt-5 без temperature, SQLite-історія, best-of-run → cs_C2_improved_1h.yaml.
"""

import os
import sys
import argparse
import csv
import json
import sqlite3
import traceback
import subprocess
import re
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Tuple, List

# ===================== ANSI =====================
class C:
    R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"; B = "\033[34m"
    M = "\033[35m"; Cc = "\033[36m"; W = "\033[37m"
    BOLD = "\033[1m"; DIM = "\033[2m"; RST = "\033[0m"

def colorize(ok, text): return (C.G if ok else C.R) + text + C.RST

# ===================== Tee-log ==================
class Tee:
    def __init__(self, path: Path, mode="a"):
        self.file = open(path, mode, encoding="utf-8")
        self.stdout = sys.stdout; self.stderr = sys.stderr
        sys.stdout = self; sys.stderr = self
    def write(self, data): self.stdout.write(data); self.file.write(data)
    def flush(self): self.stdout.flush(); self.file.flush()
    def close(self):
        try: self.file.close()
        finally:
            sys.stdout = self.stdout
            sys.stderr = self.stderr

# ===================== Cost =====================
PRICES = {
    "gpt-4o":      {"in": 5.00,  "cached_in": 2.50, "out": 20.00},
    "gpt-4o-mini": {"in": 0.60,  "cached_in": 0.30, "out": 2.40},
    "gpt-5":       {"in": 1.25,  "cached_in": 0.125, "out": 10.00},
    "gpt-5-mini":  {"in": 0.30,  "cached_in": 0.05,  "out": 1.20},
}
COST_SUM = 0.0
TOK_IN_SUM = TOK_OUT_SUM = TOK_IN_CACHED_SUM = 0
UAH_RATE = float(os.getenv("UAH_RATE", "40"))

def price_for_model(model: str) -> Optional[Dict[str, float]]:
    m = (model or "").lower()
    if m.startswith("gpt-5-mini"): return PRICES["gpt-5-mini"]
    if m.startswith("gpt-5"):      return PRICES["gpt-5"]
    if m.startswith("gpt-4o-mini"):return PRICES["gpt-4o-mini"]
    if m.startswith("gpt-4o"):     return PRICES["gpt-4o"]
    return None

def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int, cached_prompt_tokens: int = 0) -> float:
    p = price_for_model(model or "gpt-4o-mini")
    if not p: return 0.0
    non_cached = max(int(prompt_tokens) - int(cached_prompt_tokens), 0)
    return (non_cached/1e6)*p["in"] + (cached_prompt_tokens/1e6)*p.get("cached_in", p["in"]) + (int(completion_tokens)/1e6)*p["out"]

def log_cost_delta(model: str, before: Tuple[int,int,int], who: str="run") -> float:
    """Вивести інкремент вартості; якщо LLM не юзаємо — буде 0."""
    global TOK_IN_SUM, TOK_OUT_SUM, TOK_IN_CACHED_SUM, COST_SUM
    b_in, b_out, b_cached = before
    pt = TOK_IN_SUM - b_in; ct = TOK_OUT_SUM - b_out; cached = TOK_IN_CACHED_SUM - b_cached
    est = estimate_cost(model or "gpt-4o-mini", pt, ct, cached)
    COST_SUM += est
    print(f"{C.DIM}[COST]{C.RST} stage={who} model={model or 'gpt-4o-mini'} | in={pt} (cached={cached}) | out={ct} | est=${est:.6f} (~₴{est*UAH_RATE:.2f})")
    return est

# ===================== Utils ====================
REQ_SIG = f"py={sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}|deps=pandas,numpy,pyyaml|optimizer=v8.4"

def detect_default_cfg(project_dir: str) -> str:
    p = Path(project_dir).resolve()
    opt2 = p / "configs" / "cs_C2_base_1h.yaml"
    return "configs/cs_C2_base_1h.yaml" if opt2.exists() else "backtest_SK/configs/cs_C2_base_1h.yaml"

def _run(cmd: str, cwd: Path, timeout: int = 900) -> Tuple[int, str]:
    env = os.environ.copy()
    p = subprocess.run(cmd, cwd=str(cwd), shell=True, capture_output=True, text=True, timeout=timeout, env=env)
    out = (p.stdout or "") + ("\n" + (p.stderr or "") if p.stderr else "")
    return p.returncode, out

def _venv_run(proj_root: Path, venv_here: Path, cmd: str, timeout=900) -> Tuple[int, str]:
    env = os.environ.copy()
    env["PATH"] = f"{(venv_here / 'bin')}:{env.get('PATH','')}"
    env["VIRTUAL_ENV"] = str(venv_here)
    p = subprocess.run(cmd, cwd=str(proj_root), shell=True, capture_output=True, text=True, timeout=timeout, env=env)
    return p.returncode, (p.stdout or "") + ("\n" + (p.stderr or "") if p.stderr else "")

def _ensure_venv(proj_root: Path, venv_here: Path, use_cache: bool = True) -> Tuple[bool, str]:
    marker = venv_here / ".deps_ok"
    if not venv_here.exists():
        code, out = _run(f"{sys.executable} -m venv {venv_here}", cwd=proj_root, timeout=300)
        if code != 0: return False, out
    if use_cache and marker.exists():
        try:
            if marker.read_text(encoding="utf-8").strip() == REQ_SIG:
                print(f"{C.DIM}[PRERUN cache]{C.RST} HIT")
                return True, "cache hit"
        except Exception: pass
    print(f"{C.DIM}[PRERUN cache]{C.RST} MISS → installing deps…")
    code, out = _venv_run(proj_root, venv_here, "python -m pip install --upgrade pip", timeout=300)
    if code != 0: return False, out
    code, out = _venv_run(proj_root, venv_here, "pip install pandas numpy pyyaml", timeout=900)
    if code != 0: return False, out
    try: marker.write_text(REQ_SIG, encoding="utf-8")
    except Exception: pass
    return True, "deps installed"

# ---------- summary.csv helpers ----------
def _summary_path(project_dir: str) -> Path:
    return Path(project_dir).resolve() / "summary.csv"

def _wait_summary_update(path: Path, prev_mtime: Optional[float], timeout_s: int = 180) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if path.exists():
            mt = path.stat().st_mtime
            if prev_mtime is None or mt > prev_mtime:
                return True
        time.sleep(0.5)
    return False

def _read_csv_metrics(csv_path: Path) -> Tuple[Dict, Dict]:
    metrics, row = {}, {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if rows:
        row = rows[-1]  # останній рядок — найновіший прогін
        for k, v in row.items():
            kl = (k or "").lower()
            if kl in ("equity_end", "trades", "profit_factor", "max_dd", "win_rate", "wr", "pf", "max_drawdown_%", "win_rate_%", "equity_start", "equity_start_usd"):
                metrics[k] = v
    return metrics, row

def _norm_metrics(csv_row: Dict) -> Dict[str, float]:
    if not csv_row: return {}
    m = {}
    def _get(*keys):
        for k in keys:
            if k in csv_row: return csv_row[k]
        return None
    def _tofloat(x):
        try: return float(str(x).replace('%','').replace('−','-'))
        except Exception: return None
    m['equity_start'] = _tofloat(_get('equity_start', 'equity_start_usd'))
    m['equity_end']    = _tofloat(_get('equity_end','Equity end'))
    m['profit_factor'] = _tofloat(_get('profit_factor','pf','Profit Factor'))
    dd                 = _get('max_drawdown_%','max_dd','Max DD')
    m['max_dd']        = _tofloat(dd)
    m['win_rate']      = _tofloat(_get('win_rate_%','win_rate','Win-rate'))
    m['trades']        = _tofloat(_get('trades','Trades'))
    return m

def _fmt_metrics_readable(m: Dict) -> str:
    if not m: return "метрики не знайдено"
    lines = []
    kv = {k.lower(): (k, v) for k, v in m.items()}
    def pick(*names):
        for n in names:
            if n in kv: return kv[n][1]
        return None
    def add(label, *keys):
        v = pick(*keys)
        if v is not None: lines.append(f"{C.BOLD}{label}{C.RST}: {v}")
    add("Equity end", "equity_end")
    add("Trades", "trades")
    add("Profit Factor", "profit_factor", "pf")
    add("Max DD", "max_dd", "max_drawdown_%")
    add("Win-rate", "win_rate", "win_rate_%", "wr")
    return "\n".join(lines)

def _score_equity(m: Dict[str, float]) -> float:
    return float(m.get('equity_end') or 0.0)

def _calc_period_returns(equity_end: float, equity_start: float, period_days: float):
    """Повертає total, monthly, annual (у частках)."""
    try:
        ee = float(equity_end); es = float(equity_start); pd = float(period_days)
        if es <= 0 or pd <= 0: return {}
        total = ee / es
        monthly = total ** (30.0 / pd) - 1.0
        annual  = total ** (365.0 / pd) - 1.0
        return {"total": total - 1.0, "monthly": monthly, "annual": annual}
    except Exception:
        return {}

# ======== AUTO-DETECT: equity_start & period_days ========
def _detect_equity_start_from_cfg(project_dir: str, cfg_relpath: str) -> Optional[float]:
    try:
        import yaml
        proj = Path(project_dir).resolve()
        cfg = (proj / cfg_relpath).resolve()
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        keys = ["initial_equity", "equity_start", "starting_equity", "capital", "equity"]
        cur = data
        for k in keys:
            if isinstance(cur, dict) and k in cur:
                try: return float(cur[k])
                except Exception: pass
        sp = data.get("strategy_params") if isinstance(data, dict) else None
        if isinstance(sp, dict):
            for k in keys:
                if k in sp:
                    try: return float(sp[k])
                    except Exception: pass
    except Exception:
        return None
    return None

def _try_parse_ts(s):
    try:
        iv = int(s)
        if iv > 1e12:  # ms
            return datetime.utcfromtimestamp(iv/1000.0)
        if iv > 1e9:   # s
            return datetime.utcfromtimestamp(iv)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try: return datetime.strptime(str(s), fmt)
        except Exception: pass
    try: return datetime.fromisoformat(str(s).replace("Z","").split(".")[0])
    except Exception: pass
    return None

def _span_days(min_v, max_v) -> Optional[float]:
    t0 = _try_parse_ts(min_v); t1 = _try_parse_ts(max_v)
    if not t0 or not t1: return None
    delta = (t1 - t0).total_seconds()
    if delta <= 0: return None
    return delta / 86400.0

def _detect_period_days_from_cache(project_dir: str, cfg_relpath: str) -> Optional[float]:
    try:
        import yaml
        proj = Path(project_dir).resolve()
        cfg = (proj / cfg_relpath).resolve()
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        cache_rel = None
        if isinstance(data, dict):
            cache_rel = data.get("cache_db")
            if not cache_rel and isinstance(data.get("strategy_params"), dict):
                cache_rel = data["strategy_params"].get("cache_db")
        if not cache_rel:
            return None
        cache_path = (cfg.parent / cache_rel).resolve()
        if not cache_path.exists():
            return None

        # 1) read span from sqlite
        try:
            con = sqlite3.connect(str(cache_path))
            cur = con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            preferred = [t for t in tables if str(t).lower() in ("bars","klines","candles","ohlc","data")]
            scan_tables = preferred if preferred else tables
            for t in scan_tables:
                cols = [r[1] for r in con.execute(f"PRAGMA table_info('{t}')").fetchall()]
                time_cols = [c for c in cols if str(c).lower() in ("timestamp","ts","time","datetime","date")]
                for c in time_cols:
                    try:
                        mn, mx = con.execute(f"SELECT MIN({c}), MAX({c}) FROM '{t}'").fetchone()
                        if mn is None or mx is None: continue
                        days = _span_days(mn, mx)
                        if days and days > 0:
                            return float(days)
                    except Exception:
                        continue
        except Exception:
            pass

        # 2) fallback: parse "1440" from filename => 1440 1h-bars => 60 days
        m = re.search(r"(\d{3,6})", cache_path.name)
        if m:
            bars = int(m.group(1))
            if bars >= 24:
                return float(bars) / 24.0
        return 60.0
    except Exception:
        return 60.0

# ===================== DB =======================
def db_init(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("""
    CREATE TABLE IF NOT EXISTS trials(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT,
      exp_key TEXT,
      cfg_relpath TEXT,
      param TEXT,
      value REAL,
      equity_end REAL,
      profit_factor REAL,
      max_dd REAL,
      win_rate REAL,
      trades INTEGER,
      summary_path TEXT,
      is_baseline INTEGER DEFAULT 0
    );
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_trials_exp ON trials(exp_key);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_trials_param ON trials(exp_key,param);")
    con.commit()
    return con

def db_exp_key(project_dir: str, tuned_cfg: str) -> str:
    return f"{Path(project_dir).resolve()}|{tuned_cfg}"

def db_store_trial(con, exp_key: str, cfg_relpath: str, is_baseline: bool, param: Optional[str], value: Optional[float], norm: Dict[str,float], summary_path: Optional[str]):
    con.execute("""
      INSERT INTO trials(ts,exp_key,cfg_relpath,param,value,equity_end,profit_factor,max_dd,win_rate,trades,summary_path,is_baseline)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.utcnow().isoformat(timespec="seconds"),
        exp_key,
        cfg_relpath,
        param, float(value) if value is not None else None,
        norm.get('equity_end'), norm.get('profit_factor'), norm.get('max_dd'), norm.get('win_rate'), int(norm.get('trades') or 0),
        summary_path, 1 if is_baseline else 0
    ))
    con.commit()

def db_last_cfg(con, exp_key: str) -> Optional[str]:
    cur = con.execute("SELECT cfg_relpath FROM trials WHERE exp_key=? ORDER BY id DESC LIMIT 1", (exp_key,))
    row = cur.fetchone()
    return row[0] if row else None

def db_best_norm(con, exp_key: str) -> Optional[Dict[str,float]]:
    cur = con.execute("SELECT equity_end,profit_factor,max_dd,win_rate,trades FROM trials WHERE exp_key=? ORDER BY equity_end DESC, id DESC LIMIT 1", (exp_key,))
    r = cur.fetchone()
    if not r: return None
    return {"equity_end": r[0], "profit_factor": r[1], "max_dd": r[2], "win_rate": r[3], "trades": float(r[4]) if r[4] is not None else None}

def db_has_value(con, exp_key: str, param: str, value: float) -> bool:
    cur = con.execute("SELECT 1 FROM trials WHERE exp_key=? AND param=? AND value=? LIMIT 1", (exp_key, param, float(value)))
    return cur.fetchone() is not None

def db_param_counts(con, exp_key: str) -> Dict[str,int]:
    d = {}
    cur = con.execute("SELECT param, COUNT(*) FROM trials WHERE exp_key=? AND param IS NOT NULL GROUP BY param", (exp_key,))
    for p, c in cur.fetchall(): d[p] = c
    return d

# ============ Project actions ===================
def run_backtest_cfg(project_dir: str, cfg_relpath: str, model_for_cost: str = "gpt-4o-mini", use_cache_prerun: bool = True) -> Dict:
    proj_root = Path(project_dir).resolve()
    venv_here = proj_root / ".venv_autogen"
    ok, out = _ensure_venv(proj_root, venv_here, use_cache=use_cache_prerun)
    if not ok: return {"ok": False, "step": "venv_setup", "log": out}

    script_py = (proj_root / "backtester_core.py")
    if not script_py.exists():
        script_py = (proj_root / "backtest_SK" / "backtester_core.py")
    if not script_py.exists():
        return {"ok": False, "step": "script_missing", "log": f"Not found backtester_core.py under {proj_root}"}

    cfg_path = (proj_root / cfg_relpath).resolve()
    if not cfg_path.exists():
        return {"ok": False, "step": "cfg_missing", "log": f"Config not found: {cfg_path}"}

    try: rel_cfg = cfg_path.relative_to(proj_root)
    except Exception: rel_cfg = cfg_path

    # cost checkpoint (LLM тут не використовується, але збережемо API)
    global TOK_IN_SUM, TOK_OUT_SUM, TOK_IN_CACHED_SUM
    before = (TOK_IN_SUM, TOK_OUT_SUM, TOK_IN_CACHED_SUM)

    summ = _summary_path(project_dir)
    prev_mtime = summ.stat().st_mtime if summ.exists() else None

    cmd = f"python {script_py} --cfg {rel_cfg}"
    print(f"{C.Cc}{C.BOLD}▶ Run:{C.RST} {cmd}")
    code, out = _venv_run(proj_root, venv_here, cmd, timeout=1800)

    # cost (пер-прогін) — 0, бо не LLM; показуємо для консистентності
    log_cost_delta(model_for_cost, before, who="backtest")

    # Очікуємо, поки summary.csv оновиться
    if not _wait_summary_update(summ, prev_mtime, timeout_s=180):
        print(f"{C.Y}[warn]{C.RST} summary.csv не оновився за 180с — читаю поточний стан")

    metrics, csv_row = {}, {}
    found = summ if summ.exists() else None
    if found:
        try:
            metrics, csv_row = _read_csv_metrics(found)
            try:
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                audit = found.parent / f"summary_{ts}.csv"
                if not audit.exists():
                    audit.write_text(found.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception: pass
        except Exception as e:
            metrics = {"parse_error": repr(e)}

    ok_run = (code == 0)
    print(colorize(ok_run, f"Backtest finished with code={code}"))
    if found: print(f"{C.DIM}Summary:{C.RST} {found}")
    if metrics: print(_fmt_metrics_readable(metrics))

    return {
        "ok": ok_run, "cmd": cmd, "logs_tail": out[-4000:],
        "metrics": metrics, "csv_row": csv_row,
        "summary_path": str(found) if found else None,
    }

def update_yaml_params(project_dir: str, cfg_relpath: str, updates: Dict, dest_relpath: Optional[str] = None) -> Dict:
    try: import yaml
    except Exception as e: return {"ok": False, "step": "import_yaml_failed", "log": repr(e)}
    proj_root = Path(project_dir).resolve()
    cfg_path = (proj_root / cfg_relpath).resolve()
    if not cfg_path.exists(): return {"ok": False, "step": "cfg_missing", "log": f"Config not found: {cfg_path}"}
    base_dir = cfg_path.parent
    dest_path = (proj_root / (dest_relpath or (base_dir / "cs_C2_tuned_1h.yaml"))).resolve()
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return {"ok": False, "step": "yaml_load_error", "log": repr(e)}
    def set_deep(d, dotted, value):
        parts = dotted.split("."); cur = d
        for k in parts[:-1]:
            if not isinstance(cur, dict): return False
            if k not in cur or not isinstance(cur[k], dict): cur[k] = {}
            cur = cur[k]
        cur[parts[-1]] = value; return True
    changed = []
    for k, v in (updates or {}).items():
        if set_deep(data, k, v): changed.append(k)
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        import yaml
        txt = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        dest_path.write_text(txt, encoding="utf-8")
        hist = dest_path.parent / "autogen_history"
        hist.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        (hist / f"{dest_path.stem}_{ts}.yaml").write_text(txt, encoding="utf-8")
    except Exception as e: return {"ok": False, "step": "yaml_write_error", "log": repr(e)}
    return {"ok": True, "changed": changed, "baseline": str(cfg_path), "tuned": str(dest_path)}

# ====== Param selection (strategy_params.*, not limit_bars) ======
TUNE_BLOCKLIST = {"strategy_params.limit_bars", "limit_bars"}

def _flatten(d, prefix=""):
    items = {}
    if isinstance(d, dict):
        for k, v in d.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict): items.update(_flatten(v, p))
            else: items[p] = v
    return items

def _pick_param_to_tune(project_dir: str, cfg_relpath: str, con, exp_key: str, prefer_param: Optional[str]=None) -> Optional[str]:
    try:
        import yaml
        proj = Path(project_dir).resolve()
        cfg = (proj / cfg_relpath).resolve()
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    flat = _flatten(data)
    candidates = [k for k, v in flat.items()
                  if k.startswith("strategy_params.") and k not in TUNE_BLOCKLIST and isinstance(v, (int, float))]
    if not candidates: return None
    if prefer_param and prefer_param in candidates:
        return prefer_param
    counts = db_param_counts(con, exp_key)
    candidates.sort(key=lambda p: counts.get(p, 0))
    return candidates[0]

def _propose_new_value(old, step: float = 0.1):
    try: base = float(old)
    except Exception: return old
    newv = base * (1.0 + step)
    if isinstance(old, int) or abs(round(base) - base) < 1e-9:
        return int(max(1, round(newv)))
    return float(f"{newv:.6f}")

# ===================== Critic ===================
def _supports_temperature(model: str) -> bool:
    m = (model or "").lower()
    if m.startswith("gpt-5"): return False
    return True

def _parse_critic_suggestion(text: str) -> Tuple[Optional[str], Optional[float]]:
    if not text: return None, None
    s = text.strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m: return None, None
    try:
        obj = json.loads(m.group(0))
        p = obj.get("next_param")
        pct = obj.get("pct")
        dirn = (obj.get("direction") or "").lower()
        if isinstance(pct, (int,float)) and p:
            signed = float(pct) if dirn in ("up","+","increase") else (-float(pct) if dirn in ("down","-","decrease") else float(pct))
            return p, signed
    except Exception:
        return None, None
    return None, None

def critic_evaluate(prev_norm: Dict[str,float], new_norm: Dict[str,float], param_changed: str, old_val, new_val, model: str, max_tokens: int) -> Optional[str]:
    if not os.getenv("OPENAI_API_KEY"):
        print(f"{C.DIM}[critic]{C.RST} OPENAI_API_KEY not set — пропускаю.")
        return None

    payload_messages = [
        {"role":"system", "content": (
            "Ти — тюнер параметрів для крипто-стратегій. Отримай різницю метрик між двома бектестами та порадь наступний параметр/напрямок для проби. "
            "Відповідай коротко у JSON: {\"next_param\":\"strategy_params.X\",\"direction\":\"up|down\",\"pct\":0.10,\"note\":\"...\"}."
        )},
        {"role":"user", "content": json.dumps({
            "prev": prev_norm, "new": new_norm,
            "changed": {"param": param_changed, "old": old_val, "new": new_val}
        }, ensure_ascii=False)}
    ]

    global TOK_IN_SUM, TOK_OUT_SUM, TOK_IN_CACHED_SUM
    before = (TOK_IN_SUM, TOK_OUT_SUM, TOK_IN_CACHED_SUM)

    try:
        from openai import OpenAI
        client = OpenAI()

        base_kwargs = dict(model=model, messages=payload_messages)
        if _supports_temperature(model):
            base_kwargs["temperature"] = 0.2

        try:
            resp = client.chat.completions.create(
                **base_kwargs,
                max_completion_tokens=max_tokens
            )
        except Exception as e1:
            try:
                kwargs2 = dict(base_kwargs)
                if not _supports_temperature(model) and "temperature" in kwargs2:
                    kwargs2.pop("temperature", None)
                resp = client.chat.completions.create(
                    **kwargs2,
                    max_tokens=max_tokens
                )
            except Exception as e2:
                try:
                    input_payload = json.dumps({"messages": payload_messages}, ensure_ascii=False)
                    kwargs3 = dict(model=model, input=input_payload, max_output_tokens=max_tokens)
                    if _supports_temperature(model):
                        kwargs3["temperature"] = 0.2
                    resp2 = client.responses.create(**kwargs3)
                    text = ""
                    try:
                        text = resp2.output_text.strip()
                    except Exception:
                        try:
                            text = resp2.output[0].content[0].text.strip()
                        except Exception:
                            text = ""
                    try:
                        u = getattr(resp2, "usage", None)
                        if u:
                            TOK_IN_SUM  += int(getattr(u, "input_tokens", 0))
                            TOK_OUT_SUM += int(getattr(u, "output_tokens", 0))
                    except Exception:
                        pass
                    log_cost_delta(model, before, who="critic")
                    if text:
                        print(f"{C.B}CRITIC SAYS:{C.RST} {text}")
                    return text or None
                except Exception as e3:
                    print(f"{C.Y}[critic warn]{C.RST} {e1} | {e2} | {e3}")
                    return None

        try:
            usage = resp.usage
            TOK_IN_SUM  += int(getattr(usage, "prompt_tokens", 0))
            TOK_OUT_SUM += int(getattr(usage, "completion_tokens", 0))
        except Exception:
            pass

        log_cost_delta(model, before, who="critic")
        text = resp.choices[0].message.content.strip()
        print(f"{C.B}CRITIC SAYS:{C.RST} {text}")
        return text

    except Exception as e:
        print(f"{C.Y}[critic warn]{C.RST} {e}")
        return None

# =============== Main iterate ===================
def auto_iterate_optimize(project_dir: str, cycles: int, tuned_cfg_name: str, model_for_cost="gpt-4o-mini",
                          use_cache_prerun=True, db_path: Optional[str]=None,
                          critic_enabled: bool=False, critic_model: str="gpt-5-mini", max_tokens:int=600,
                          period_days: Optional[float] = None, equity_start: Optional[float] = None) -> None:
    proj = Path(project_dir).resolve()
    base_cfg = detect_default_cfg(project_dir)
    tuned_cfg_rel = str((Path(base_cfg).parent / tuned_cfg_name).as_posix())
    exp_key = db_exp_key(project_dir, tuned_cfg_rel)

    # Автодетект equity_start/period_days (якщо не задані вручну)
    auto_eq = _detect_equity_start_from_cfg(project_dir, base_cfg) if equity_start is None else None
    auto_pd = _detect_period_days_from_cache(project_dir, base_cfg) if period_days is None else None
    equity_start_eff = equity_start if equity_start is not None else (auto_eq if auto_eq is not None else 100.0)
    period_days_eff  = period_days  if period_days  is not None else (auto_pd if auto_pd is not None else 60.0)

    print(f"{C.DIM}[auto]{C.RST} equity_start={equity_start_eff} (src={'CLI' if equity_start is not None else ('YAML' if auto_eq is not None else 'default')})")
    print(f"{C.DIM}[auto]{C.RST} period_days={period_days_eff} (src={'CLI' if period_days is not None else ('cache_db' if auto_pd is not None else 'default')})")

    # DB
    con = db_init(Path(db_path) if db_path else (proj / "autogen_results.sqlite"))

    # Можемо продовжити з останнього конфігу
    last_cfg = db_last_cfg(con, exp_key)
    source_cfg_for_edit = last_cfg if last_cfg else base_cfg
    continuing = (last_cfg is not None)
    if continuing:
        print(f"{C.DIM}[resume]{C.RST} continue with last cfg: {source_cfg_for_edit}")

    # Якщо в БД немає baseline — запустимо його
    if not continuing:
        print(f"\n{C.BOLD}{C.B}=== BASELINE RUN ==={C.RST}")
        base_res = run_backtest_cfg(project_dir, base_cfg, model_for_cost=model_for_cost, use_cache_prerun=use_cache_prerun)
        if not base_res.get("ok"):
            print(C.R + "[ERR] baseline run failed" + C.RST, base_res.get("log",""))
            return
        base_csv = base_res.get("csv_row", {})
        base_norm = _norm_metrics(base_csv)
        db_store_trial(con, exp_key, base_cfg, True, None, None, base_norm, base_res.get("summary_path"))
        print(C.DIM + "Baseline metrics:\n" + C.RST + _fmt_metrics_readable(base_res.get("metrics", {})))
        # Returns
        if base_norm.get("equity_end"):
            ret = _calc_period_returns(base_norm['equity_end'], equity_start_eff, period_days_eff)
            if ret:
                print(f"{C.M}Baseline returns:{C.RST} Period={(ret['total']*100):+.2f}%  Monthly={(ret['monthly']*100):+.2f}%  Annual={(ret['annual']*100):+.2f}%")
    else:
        base_norm = db_best_norm(con, exp_key) or {}
        print(f"{C.DIM}Baseline exists in DB — using DB best as reference.{C.RST}")

    # Best from DB
    best_norm = db_best_norm(con, exp_key) or (base_norm if base_norm else {})
    best_cfg  = db_last_cfg(con, exp_key) or base_cfg
    best_info = {"cfg": best_cfg, "metrics": best_norm, "summary": None}

    prefer_param = None
    prefer_step_pct = None  # signed

    # Ітерації тюнінгу
    for i in range(1, max(1, cycles)):
        print(f"\n{C.BOLD}{C.B}=== TUNE ITERATION #{i} ==={C.RST}")
        param = _pick_param_to_tune(project_dir, source_cfg_for_edit, con, exp_key, prefer_param=prefer_param)
        if not param:
            print(C.Y + "Не знайшов числовий параметр у strategy_params (окрім limit_bars) — стоп." + C.RST)
            break

        # Читаємо старе значення
        try:
            import yaml
            y = yaml.safe_load((proj / source_cfg_for_edit).read_text(encoding="utf-8")) or {}
            cur = y
            for k in param.split(".")[:-1]: cur = cur.get(k, {})
            old_val = cur.get(param.split(".")[-1])
        except Exception:
            old_val = None

        step = abs(prefer_step_pct) if (prefer_step_pct is not None and abs(prefer_step_pct) > 1e-9) else 0.1
        sign = 1.0 if (prefer_step_pct is None or prefer_step_pct >= 0) else -1.0
        tries = 0
        while True:
            new_val = _propose_new_value(old_val, step=step*sign)
            if not db_has_value(con, exp_key, param, float(new_val) if isinstance(new_val,(int,float)) else new_val):
                break
            tries += 1
            step *= 1.25
            if tries > 5:
                print(C.Y + f"[tune] значення для {param} вже тестувались, пропускаю." + C.RST)
                new_val = old_val
                break

        upd = update_yaml_params(project_dir, source_cfg_for_edit, {param: new_val}, dest_relpath=tuned_cfg_rel)
        if not upd.get("ok"):
            print(C.R + "[ERR] YAML update failed: " + str(upd.get("log","")) + C.RST)
            break

        print(f"{C.M}Змінив параметр:{C.RST} {param}: {old_val} → {C.BOLD}{new_val}{C.RST}")
        print(f"{C.DIM}Новий файл:{C.RST} {upd.get('tuned')}")

        tuned_res = run_backtest_cfg(project_dir, tuned_cfg_rel, model_for_cost=model_for_cost, use_cache_prerun=use_cache_prerun)
        if not tuned_res.get("ok"):
            print(C.R + "[ERR] tuned run failed" + C.RST, tuned_res.get("logs_tail","")[-400:])
            break

        tuned_csv  = tuned_res.get("csv_row", {})
        tuned_norm = _norm_metrics(tuned_csv)
        print(C.DIM + "Tuned metrics:\n" + C.RST + _fmt_metrics_readable(tuned_res.get("metrics", {})))
        # Returns per iteration
        if tuned_norm.get("equity_end"):
            tret = _calc_period_returns(tuned_norm['equity_end'], equity_start_eff, period_days_eff)
            if tret:
                print(f"{C.M}Tuned returns:{C.RST} Period={(tret['total']*100):+.2f}%  Monthly={(tret['monthly']*100):+.2f}%  Annual={(tret['annual']*100):+.2f}%")

        # Зберегти у БД
        db_store_trial(con, exp_key, tuned_cfg_rel, False, param, float(new_val) if isinstance(new_val,(int,float)) else None, tuned_norm, tuned_res.get("summary_path"))

        # Критик
        changed_any = not all([
            abs((tuned_norm.get('equity_end') or 0) - (best_norm.get('equity_end') or 0)) < 1e-12,
            abs((tuned_norm.get('profit_factor') or 0) - (best_norm.get('profit_factor') or 0)) < 1e-12,
            abs((tuned_norm.get('max_dd') or 0) - (best_norm.get('max_dd') or 0)) < 1e-12,
            abs((tuned_norm.get('win_rate') or 0) - (best_norm.get('win_rate') or 0)) < 1e-12,
        ])
        if critic_enabled and changed_any:
            text = critic_evaluate(best_norm or {}, tuned_norm or {}, param, old_val, new_val, model=critic_model, max_tokens=max_tokens)
            np, sgn = _parse_critic_suggestion(text or "")
            if np:
                prefer_param = np
            if sgn is not None:
                prefer_step_pct = sgn
            if np or (sgn is not None):
                print(f"{C.Cc}[critic hint]{C.RST} next_param={np} step_pct={sgn}")

        # Оновити best
        if _score_equity(tuned_norm) > _score_equity(best_norm):
            best_norm = tuned_norm
            best_info = {"cfg": tuned_cfg_rel, "metrics": best_norm, "summary": tuned_res.get("summary_path")}
            print(C.G + C.BOLD + "✅ Новий найкращий результат за equity_end." + C.RST)

        # Наступного разу редагуємо останній тюнений
        source_cfg_for_edit = tuned_cfg_rel

    # Порівняння з базою та фінал
    # NB: equity_start_eff/period_days_eff ми вже маємо
    best_ret = _calc_period_returns(best_info["metrics"].get("equity_end"), equity_start_eff, period_days_eff)
    if best_ret:
        print(f"\nBest-of-run returns: Period={(best_ret['total']*100):+.2f}%  Monthly={(best_ret['monthly']*100):+.2f}%  Annual={(best_ret['annual']*100):+.2f}%")

    # Якщо best >= бази — зберігаємо improved
    ref_norm = db_best_norm(con, exp_key) or {}
    if _score_equity(best_info["metrics"]) >= _score_equity(ref_norm):
        improved_path = (proj / "configs" / "cs_C2_improved_1h.yaml")
        try:
            improved_path.write_text((proj / best_info["cfg"]).read_text(encoding="utf-8"), encoding="utf-8")
            print(f"\n{C.G}{C.BOLD}=== BEST OF RUN (by equity_end) — SAVED ==={C.RST}")
            print(f"Config: {best_info['cfg']}  →  saved as configs/cs_C2_improved_1h.yaml")
        except Exception as e:
            print(C.Y + "[WARN] не вдалось записати improved: " + repr(e) + C.RST)
    else:
        print(f"\n{C.Y}{C.BOLD}=== BEST OF RUN — NO IMPROVEMENT over DB best ==={C.RST}")

    print(f"\n{C.B}{C.BOLD}--- BEST METRICS (current session) ---{C.RST}")
    print(f"CFG: {best_info['cfg']}")
    if best_info.get("summary"): print(f"Summary CSV: {best_info['summary']}")
    print(_fmt_metrics_readable(best_info.get("metrics", {})))

# ===================== CLI ======================
def main():
    ap = argparse.ArgumentParser()
    # legacy (сумісність)
    ap.add_argument("--task-file", default=None, help="(legacy) task description file; logged only")
    ap.add_argument("--code-zip", default=None, help="(legacy) code bundle; logged only")

    ap.add_argument("--project-dir", required=True, help="Root of the project (contains backtester)")
    ap.add_argument("--prerun", action="store_true", help="Ensure venv + deps")
    ap.add_argument("--no-prerun-cache", action="store_true", help="Disable prerun cache (force reinstall)")
    ap.add_argument("--cycles", type=int, default=2, help="Number of iterations (>=2 to see a change)")
    ap.add_argument("--tuned-config-name", default="cs_C2_tuned_1h.yaml", help="Output config name under configs/")
    ap.add_argument("--log-file", default=None, help="Optional log file (tee)")
    ap.add_argument("--max-tokens", type=int, default=1200, help="LLM max tokens (used by critic)")
    ap.add_argument("--uah-rate", type=float, default=float(os.getenv("UAH_RATE","40")), help="UAH/USD rate for cost display")
    ap.add_argument("--model", default=os.getenv("OPENAI_EXECUTOR_MODEL", "gpt-4o-mini"), help="AI model name for cost lines")
    ap.add_argument("--db-path", default=None, help="SQLite path (default: <project_dir>/autogen_results.sqlite)")
    ap.add_argument("--critic", action="store_true", help="Enable critic model suggestions when metrics change")
    ap.add_argument("--critic-model", default="gpt-5-mini", help="Model for critic suggestions")

    # За замовчуванням None → авто-детект з проекту (YAML/cache_db)
    ap.add_argument("--period-days", type=float, default=None, help="Період бектесту у днях (None — автодетект)")
    ap.add_argument("--equity-start", type=float, default=None, help="Стартовий капітал (None — автодетект з YAML)")

    args = ap.parse_args()
    global UAH_RATE
    UAH_RATE = float(args.uah_rate or UAH_RATE)

    tee = None
    if args.log_file:
        try:
            p = Path(args.log_file).expanduser().resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            tee = Tee(p); print(f"{C.DIM}[log]{C.RST} tee -> {p}")
        except Exception as e:
            print(C.Y + f"[log warn] cannot open {args.log_file}: {e}" + C.RST)

    print(f"{C.DIM}Args:{C.RST} project_dir={args.project_dir} | cycles={args.cycles} | tuned={args.tuned_config_name} | model={args.model} | critic={args.critic}({args.critic_model}) | period_days={args.period_days} | equity_start={args.equity_start}")
    if args.task_file: print(f"{C.DIM}(legacy) task-file:{C.RST} {args.task_file}")
    if args.code_zip:  print(f"{C.DIM}(legacy) code-zip:{C.RST}  {args.code_zip}")

    proj_root = Path(args.project_dir).resolve()
    if args.prerun:
        ok, out = _ensure_venv(proj_root, proj_root / ".venv_autogen", use_cache=(not args.no_prerun_cache))
        print(colorize(ok, "[PRERUN] OK" if ok else "[PRERUN] FAIL"))
        if not ok: print(out)

    auto_iterate_optimize(
        args.project_dir,
        max(args.cycles, 2),
        args.tuned_config_name,
        model_for_cost=args.model,
        use_cache_prerun=(not args.no_prerun_cache),
        db_path=args.db_path,
        critic_enabled=args.critic,
        critic_model=args.critic_model,
        max_tokens=args.max_tokens,
        period_days=args.period_days,
        equity_start=args.equity_start,
    )

    print(f"\n{C.DIM}=== COST SUMMARY ==={C.RST}")
    print(f"total_est=${COST_SUM:.6f}  (~₴{COST_SUM*UAH_RATE:.2f} @ {UAH_RATE} UAH/USD)")

    if tee: tee.close()

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt:
        print("\n[INTERRUPTED]")
    except Exception:
        traceback.print_exc(); sys.exit(1)
