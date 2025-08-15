#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deterministic backtest parameter optimizer — v7.5 (Py3.8)

Збережено/додано:
- Кеш PRERUN (маркер .venv_autogen/.deps_ok), --no-prerun-cache щоб вимкнути.
- Тюнінг лише у strategy_params.*, окрім strategy_params.limit_bars (не змінюємо).
- Кольоровий вивід (ANSI) та читабельні метрики.
- Після КОЖНОГО тесту — рядок вартості токенів (якщо LLM не використовується — 0$).
- З 2-ї ітерації бектест іде по тюненому конфігу.
- Читання РЕАЛЬНИХ метрик з CSV; пріоритет: <project_dir>/summary.csv.
- Збереження найкращого сету як configs/cs_C2_improved_1h.yaml (якщо кращий за baseline).
- Журнал autogen_runs.jsonl.
- Повернено старі CLI-прапори: --task-file, --code-zip (логуються й не заважають).
"""

import os
import sys
import argparse
import csv
import json
import traceback
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Tuple, List

# ===== ANSI Colors =====
class C:
    R = "\033[31m"
    G = "\033[32m"
    Y = "\033[33m"
    B = "\033[34m"
    M = "\033[35m"
    Cc = "\033[36m"
    W = "\033[37m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RST = "\033[0m"

def colorize(ok, text):
    return (C.G if ok else C.R) + text + C.RST

# ===== tee-лог у файл =====
class Tee:
    def __init__(self, path: Path, mode="a"):
        self.file = open(path, mode, encoding="utf-8")
        self.stdout = sys.stdout
        self.stderr = sys.stderr
        sys.stdout = self
        sys.stderr = self
    def write(self, data):
        self.stdout.write(data)
        self.file.write(data)
    def flush(self):
        self.stdout.flush()
        self.file.flush()
    def close(self):
        try:
            self.file.close()
        except Exception:
            pass
        sys.stdout = self.stdout
        sys.stderr = self.stderr

# ===== LLM Cost (placeholders; 0 if LLM not used) =====
PRICES = {
    "gpt-4o":      {"in": 5.00,  "cached_in": 2.50, "out": 20.00},
    "gpt-4o-mini": {"in": 0.60,  "cached_in": 0.30, "out": 2.40},
    "gpt-5":       {"in": 1.25,  "cached_in": 0.125, "out": 10.00},
    "gpt-5-mini":  {"in": 0.30,  "cached_in": 0.05,  "out": 1.20},
}
COST_SUM = 0.0
TOK_IN_SUM = TOK_OUT_SUM = TOK_IN_CACHED_SUM = 0
UAH_RATE = float(os.getenv("UAH_RATE", "40"))

def price_for_model(model: str) -> Dict[str, float]:
    m = (model or "").lower()
    if m.startswith("gpt-5-mini"):
        return PRICES["gpt-5-mini"]
    if m.startswith("gpt-5"):
        return PRICES["gpt-5"]
    if m.startswith("gpt-4o-mini"):
        return PRICES["gpt-4o-mini"]
    if m.startswith("gpt-4o"):
        return PRICES["gpt-4o"]
    return None

def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int, cached_prompt_tokens: int = 0) -> float:
    p = price_for_model(model or "gpt-4o-mini")
    if not p:
        return 0.0
    non_cached = max(int(prompt_tokens) - int(cached_prompt_tokens), 0)
    return (non_cached/1e6)*p["in"] + (cached_prompt_tokens/1e6)*p.get("cached_in", p["in"]) + (int(completion_tokens)/1e6)*p["out"]

def log_cost_delta(model: str, before: Tuple[int,int,int]) -> float:
    """Вивести пер-тестову вартість (часто 0, якщо LLM не використовуємо)."""
    global TOK_IN_SUM, TOK_OUT_SUM, TOK_IN_CACHED_SUM, COST_SUM
    b_in, b_out, b_cached = before
    pt = TOK_IN_SUM - b_in
    ct = TOK_OUT_SUM - b_out
    cached = TOK_IN_CACHED_SUM - b_cached
    est = estimate_cost(model or "gpt-4o-mini", pt, ct, cached)
    COST_SUM += est
    print(f"{C.DIM}[COST]{C.RST} model={model or 'gpt-4o-mini'} | in={pt} (cached={cached}) | out={ct} | est=${est:.6f} (~₴{est*UAH_RATE:.2f})")
    return est

# ===== Utils =====
REQ_SIG = f"py={sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}|deps=pandas,numpy,pyyaml|optimizer=v7.5"

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
    """Готує віртуальне середовище + пакети. З кешем (маркер .deps_ok)."""
    marker = venv_here / ".deps_ok"
    if not venv_here.exists():
        code, out = _run(f"{sys.executable} -m venv {venv_here}", cwd=proj_root, timeout=300)
        if code != 0:
            return False, out
    if use_cache and marker.exists():
        try:
            if marker.read_text(encoding="utf-8").strip() == REQ_SIG:
                print(f"{C.DIM}[PRERUN cache]{C.RST} HIT")
                return True, "cache hit"
        except Exception:
            pass
    print(f"{C.DIM}[PRERUN cache]{C.RST} MISS → installing deps…")
    code, out = _venv_run(proj_root, venv_here, "python -m pip install --upgrade pip", timeout=300)
    if code != 0:
        return False, out
    code, out = _venv_run(proj_root, venv_here, "pip install pandas numpy pyyaml", timeout=900)
    if code != 0:
        return False, out
    try:
        marker.write_text(REQ_SIG, encoding="utf-8")
    except Exception:
        pass
    return True, "deps installed"

def _read_csv_metrics(csv_path: Path) -> Tuple[Dict, Dict]:
    metrics, row = {}, {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if rows:
        row = rows[0]
        for k, v in row.items():
            kl = (k or "").lower()
            if kl in ("equity_end", "trades", "profit_factor", "max_dd", "win_rate", "wr", "pf", "max_drawdown_%", "win_rate_%"):
                metrics[k] = v
    return metrics, row

def _norm_metrics(csv_row: Dict) -> Dict[str, float]:
    if not csv_row:
        return {}
    m = {}
    def _get(*keys):
        for k in keys:
            if k in csv_row:
                return csv_row[k]
        return None
    def _tofloat(x):
        try:
            return float(str(x).replace('%','').replace('−','-'))
        except Exception:
            return None
    m['equity_end']    = _tofloat(_get('equity_end','Equity end'))
    m['profit_factor'] = _tofloat(_get('profit_factor','pf','Profit Factor'))
    dd                 = _get('max_drawdown_%','max_dd','Max DD')
    m['max_dd']        = _tofloat(dd)
    m['win_rate']      = _tofloat(_get('win_rate_%','win_rate','Win-rate'))
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

# ===== Project actions =====
def run_backtest_cfg(project_dir: str, cfg_relpath: str, model_for_cost: str = "gpt-4o-mini", use_cache_prerun: bool = True) -> Dict:
    proj_root = Path(project_dir).resolve()
    venv_here = proj_root / ".venv_autogen"
    ok, out = _ensure_venv(proj_root, venv_here, use_cache=use_cache_prerun)
    if not ok:
        return {"ok": False, "step": "venv_setup", "log": out}

    # backtester_core.py
    script_py = (proj_root / "backtest_SK" / "backtester_core.py")
    if not script_py.exists():
        script_py = (proj_root / "backtester_core.py")
    if not script_py.exists():
        return {"ok": False, "step": "script_missing", "log": f"Not found backtester_core.py under {proj_root}"}

    cfg_path = (proj_root / cfg_relpath).resolve()
    if not cfg_path.exists():
        return {"ok": False, "step": "cfg_missing", "log": f"Config not found: {cfg_path}"}

    try:
        rel_cfg = cfg_path.relative_to(proj_root)
    except Exception:
        rel_cfg = cfg_path

    # cost checkpoint (per test)
    global TOK_IN_SUM, TOK_OUT_SUM, TOK_IN_CACHED_SUM
    before = (TOK_IN_SUM, TOK_OUT_SUM, TOK_IN_CACHED_SUM)

    import time
    t0 = time.time()
    cmd = f"python {script_py} --cfg {rel_cfg}"
    print(f"{C.Cc}{C.BOLD}▶ Run:{C.RST} {cmd}")
    code, out = _venv_run(proj_root, venv_here, cmd, timeout=1800)

    # Per-test cost (likely zero, but visible)
    log_cost_delta(model_for_cost, before)

    # Find newest CSV
    candidates = [
        "summary.csv",
        "backtest_SK/summary.csv",
        "backtest_SK/reports/c2_repeat_1h_1440_summary.csv",
        "reports/c2_repeat_1h_1440_summary.csv",
        "reports/summary.csv",
    ]
    found = None
    search_dirs = [proj_root, proj_root / "reports", proj_root / "backtest_SK" / "reports"]
    recents: List[Tuple[float, Path]] = []
    for d in search_dirs:
        if d.exists():
            for f in d.glob("*.csv"):
                try:
                    recents.append((f.stat().st_mtime, f))
                except Exception:
                    pass
    recents.sort(reverse=True)
    for mtime, f in recents:
        if mtime >= (t0 - 5):
            found = f
            break
    if not found:
        for pth in candidates:
            f = (proj_root / pth).resolve()
            if f.exists() and f.is_file():
                found = f
                break

    metrics, csv_row = {}, {}
    if found:
        try:
            metrics, csv_row = _read_csv_metrics(found)
            try:
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                audit = found.parent / f"summary_{ts}.csv"
                if not audit.exists():
                    audit.write_text(found.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                pass
        except Exception as e:
            metrics = {"parse_error": repr(e)}

    # meta log
    try:
        meta = {
            "ts": datetime.utcnow().isoformat(),
            "cfg_relpath": str(rel_cfg),
            "summary_path": str(found) if found else None,
            "metrics": metrics,
            "return_code": code,
        }
        j = proj_root / "autogen_runs.jsonl"
        j.write_text((j.read_text(encoding="utf-8") if j.exists() else "") + json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass

    ok_run = (code == 0)
    print(colorize(ok_run, f"Backtest finished with code={code}"))
    if found:
        print(f"{C.DIM}Summary:{C.RST} {found}")
    if metrics:
        print(_fmt_metrics_readable(metrics))

    return {
        "ok": ok_run,
        "cmd": cmd,
        "logs_tail": out[-4000:],
        "metrics": metrics,
        "csv_row": csv_row,
        "summary_path": str(found) if found else None,
    }

def update_yaml_params(project_dir: str, cfg_relpath: str, updates: Dict, dest_relpath: Optional[str] = None) -> Dict:
    try:
        import yaml
    except Exception as e:
        return {"ok": False, "step": "import_yaml_failed", "log": repr(e)}
    proj_root = Path(project_dir).resolve()
    cfg_path = (proj_root / cfg_relpath).resolve()
    if not cfg_path.exists():
        return {"ok": False, "step": "cfg_missing", "log": f"Config not found: {cfg_path}"}
    base_dir = cfg_path.parent
    dest_path = (proj_root / (dest_relpath or (base_dir / "cs_C2_tuned_1h.yaml"))).resolve()
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return {"ok": False, "step": "yaml_load_error", "log": repr(e)}
    def set_deep(d, dotted, value):
        parts = dotted.split(".")
        cur = d
        for k in parts[:-1]:
            if not isinstance(cur, dict):
                return False
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
        cur[parts[-1]] = value
        return True
    changed = []
    for k, v in (updates or {}).items():
        if set_deep(data, k, v):
            changed.append(k)
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        import yaml
        txt = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        dest_path.write_text(txt, encoding="utf-8")
        hist = dest_path.parent / "autogen_history"
        hist.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        (hist / f"{dest_path.stem}_{ts}.yaml").write_text(txt, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "step": "yaml_write_error", "log": repr(e)}
    return {"ok": True, "changed": changed, "baseline": str(cfg_path), "tuned": str(dest_path)}

# ===== Param pickers (restrict to strategy_params.*, exclude limit_bars) =====
def _flatten(d, prefix=""):
    items = {}
    if isinstance(d, dict):
        for k, v in d.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                items.update(_flatten(v, p))
            else:
                items[p] = v
    return items

def _pick_param_to_tune(project_dir: str, cfg_relpath: str) -> Optional[str]:
    try:
        import yaml
        proj = Path(project_dir).resolve()
        cfg = (proj / cfg_relpath).resolve()
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    flat = _flatten(data)
    # Only strategy_params.*, exclude strategy_params.limit_bars
    candidates = [k for k, v in flat.items()
                  if k.startswith("strategy_params.") and k != "strategy_params.limit_bars" and isinstance(v, (int, float))]
    if not candidates:
        return None
    for key in candidates:
        if any(t in key for t in ("risk", "atr", "momentum", "tp", "sl", "alpha", "beta")):
            return key
    return candidates[0]

def _propose_new_value(old, step: float = 0.1):
    try:
        base = float(old)
    except Exception:
        return old
    newv = base * (1.0 + step)
    if isinstance(old, int) or abs(round(base) - base) < 1e-9:
        return int(max(1, round(newv)))
    return float(f"{newv:.6f}")

# ===== Main iterate =====
def auto_iterate_optimize(project_dir: str, cycles: int, tuned_cfg_name: str, model_for_cost="gpt-4o-mini", use_cache_prerun=True) -> None:
    proj = Path(project_dir).resolve()
    base_cfg = detect_default_cfg(project_dir)
    tuned_cfg_rel = str((Path(base_cfg).parent / tuned_cfg_name).as_posix())

    print(f"\n{C.BOLD}{C.B}=== BASELINE RUN ==={C.RST}")
    base_res = run_backtest_cfg(project_dir, base_cfg, model_for_cost=model_for_cost, use_cache_prerun=use_cache_prerun)
    if not base_res.get("ok"):
        print(C.R + "[ERR] baseline run failed" + C.RST, base_res.get("log",""))
        return

    base_csv = base_res.get("csv_row", {})
    base_norm = _norm_metrics(base_csv)
    best = {"cfg": base_cfg, "metrics": base_norm, "raw": base_res.get("metrics", {}), "summary": base_res.get("summary_path")}
    print(C.DIM + "Baseline metrics:\n" + C.RST + _fmt_metrics_readable(base_res.get("metrics", {})))

    source_cfg_for_edit = base_cfg
    for i in range(1, max(1, cycles)):
        print(f"\n{C.BOLD}{C.B}=== TUNE ITERATION #{i} ==={C.RST}")
        param = _pick_param_to_tune(project_dir, source_cfg_for_edit)
        if not param:
            print(C.Y + "Не знайшов числовий параметр у strategy_params (окрім limit_bars) — стоп." + C.RST)
            break

        # Read old value
        try:
            import yaml
            y = yaml.safe_load((proj / source_cfg_for_edit).read_text(encoding="utf-8")) or {}
            cur = y
            for k in param.split(".")[:-1]:
                cur = cur.get(k, {})
            old_val = cur.get(param.split(".")[-1])
        except Exception:
            old_val = None

        new_val = _propose_new_value(old_val, step=0.1)
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

        tuned_csv = tuned_res.get("csv_row", {})
        tuned_norm = _norm_metrics(tuned_csv)
        print(C.DIM + "Tuned metrics:\n" + C.RST + _fmt_metrics_readable(tuned_res.get("metrics", {})))

        if _score_equity(tuned_norm) > _score_equity(best["metrics"]):
            best = {"cfg": tuned_cfg_rel, "metrics": tuned_norm, "raw": tuned_res.get("metrics", {}), "summary": tuned_res.get("summary_path")}
            print(C.G + C.BOLD + "✅ Новий найкращий результат за equity_end." + C.RST)

        source_cfg_for_edit = tuned_cfg_rel

    # Save improved if better than baseline
    if _score_equity(best["metrics"]) > _score_equity(base_norm):
        improved_path = (proj / "configs" / "cs_C2_improved_1h.yaml")
        try:
            improved_path.write_text((proj / best["cfg"]).read_text(encoding="utf-8"), encoding="utf-8")
            print(f"\n{C.G}{C.BOLD}=== BEST OF RUN (by equity_end) — SAVED ==={C.RST}")
            print(f"Config: {best['cfg']}  →  saved as configs/cs_C2_improved_1h.yaml")
        except Exception as e:
            print(C.Y + "[WARN] не вдалось записати improved: " + repr(e) + C.RST)
    else:
        print(f"\n{C.Y}{C.BOLD}=== BEST OF RUN — NO IMPROVEMENT over baseline ==={C.RST}")

    print(f"\n{C.B}{C.BOLD}--- BEST METRICS ---{C.RST}")
    print(f"CFG: {best['cfg']}")
    if best.get("summary"):
        print(f"Summary CSV: {best['summary']}")
    print(_fmt_metrics_readable(best.get("raw", {})))

# ===== CLI =====
def main():
    ap = argparse.ArgumentParser()
    # старі параметри (no-op, для сумісності/логування)
    ap.add_argument("--task-file", default=None, help="(legacy) task description file; logged only")
    ap.add_argument("--code-zip", default=None, help="(legacy) code bundle; logged only")

    ap.add_argument("--project-dir", required=True, help="Root of the project (contains backtester)")
    ap.add_argument("--prerun", action="store_true", help="Ensure venv + deps")
    ap.add_argument("--no-prerun-cache", action="store_true", help="Disable prerun cache (force reinstall)")
    ap.add_argument("--cycles", type=int, default=2, help="Number of iterations (>=2 to see a change)")
    ap.add_argument("--tuned-config-name", default="cs_C2_tuned_1h.yaml", help="Output config name under configs/")
    ap.add_argument("--log-file", default=None, help="Optional log file (tee)")
    ap.add_argument("--max-tokens", type=int, default=1200, help="LLM max tokens (placeholder)")
    ap.add_argument("--uah-rate", type=float, default=float(os.getenv("UAH_RATE","40")), help="UAH/USD rate for cost display")
    ap.add_argument("--model", default=os.getenv("OPENAI_EXECUTOR_MODEL", "gpt-4o-mini"), help="AI model name for cost lines")
    args = ap.parse_args()

    global UAH_RATE
    UAH_RATE = float(args.uah_rate or UAH_RATE)

    # tee logging
    tee = None
    if args.log_file:
        try:
            p = Path(args.log_file).expanduser().resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            tee = Tee(p)
            print(f"{C.DIM}[log]{C.RST} tee -> {p}")
        except Exception as e:
            print(C.Y + f"[log warn] cannot open {args.log_file}: {e}" + C.RST)

    # banner із параметрами
    print(f"{C.DIM}Args:{C.RST} project_dir={args.project_dir} | cycles={args.cycles} | tuned={args.tuned_config_name} | model={args.model}")
    if args.task_file: print(f"{C.DIM}(legacy) task-file:{C.RST} {args.task_file}")
    if args.code_zip:  print(f"{C.DIM}(legacy) code-zip:{C.RST}  {args.code_zip}")

    proj_root = Path(args.project_dir).resolve()
    if args.prerun:
        ok, out = _ensure_venv(proj_root, proj_root / ".venv_autogen", use_cache=(not args.no_prerun_cache))
        print(colorize(ok, "[PRERUN] OK" if ok else "[PRERUN] FAIL"))
        if not ok:
            print(out)

    auto_iterate_optimize(
        args.project_dir,
        max(args.cycles, 2),
        args.tuned_config_name,
        model_for_cost=args.model,
        use_cache_prerun=(not args.no_prerun_cache),
    )

    # Final cost summary
    print(f"\n{C.DIM}=== COST SUMMARY ==={C.RST}")
    print(f"total_est=${COST_SUM:.6f}  (~₴{COST_SUM*UAH_RATE:.2f} @ {UAH_RATE} UAH/USD)")

    if tee:
        tee.close()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INTERRUPTED]")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
