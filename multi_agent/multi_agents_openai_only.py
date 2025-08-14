#!/usr/bin/env python3
"""
AutoGen multi-agent (OpenAI-only) — v6.9 (Py3.8)

Що вміє:
- Облік вартості (USD) + конвертація у грн (--uah-rate).
- CLI: --max-tokens, --log-file, --uah-rate, --cycles, --prerun, --project-dir, --code-zip, --tuned-config-name.
- Executor:
  • Хід 1: запускає базовий бектест і друкує реальні метрики з CSV.
  • Далі: змінює один числовий параметр YAML, ПОВЕРХ попередньої тюненої версії (або бази, якщо tuned ще нема),
    зберігає у `cs_C2_tuned_1h.yaml`, запускає бектест по tuned і друкує реальні метрики з нового CSV.
- Пошук найсвіжішого summary у reports (після часу старту), копія у summary_YYYYmmdd_HHMMSS.csv для аудиту.
- Повний транскрипт чату + підсумок вартості у файл (--log-file).

Зміна від v6.8:
- Важливо: `update_yaml_params(..., cfg_relpath=src_cfg, ...)` → тепер зміни накопичуються в tuned і вже «з другого разу»
  бектест точно йде по tuned.
"""

import os
import sys
import argparse
import csv
import traceback
import subprocess
import inspect
from pathlib import Path
from typing import Dict, Optional, Tuple, List
from zipfile import ZipFile
from datetime import datetime

# ---------- Pricing (USD / 1M tokens) ----------
PRICES = {
    "gpt-4o":      {"in": 5.00,  "cached_in": 2.50, "out": 20.00},
    "gpt-4o-mini": {"in": 0.60,  "cached_in": 0.30, "out": 2.40},
}

COST_SUM = 0.0
TOK_IN_SUM = 0
TOK_OUT_SUM = 0
TOK_IN_CACHED_SUM = 0
UAH_RATE = float(os.getenv("UAH_RATE", "40"))

def price_for_model(model: str) -> Dict[str, float]:
    key = "gpt-4o" if model.startswith("gpt-4o") else (
          "gpt-4o-mini" if model.startswith("gpt-4o-mini") else model)
    return PRICES.get(key)

def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int, cached_prompt_tokens: int = 0) -> float:
    p = price_for_model(model)
    if not p:
        return 0.0
    non_cached = max(prompt_tokens - cached_prompt_tokens, 0)
    return (non_cached/1e6)*p["in"] + (cached_prompt_tokens/1e6)*p.get("cached_in", p["in"]) + (completion_tokens/1e6)*p["out"]

def print_cost(model: str, usage, where: str):
    global COST_SUM, TOK_IN_SUM, TOK_OUT_SUM, TOK_IN_CACHED_SUM
    pt = getattr(usage, "prompt_tokens", None)
    ct = getattr(usage, "completion_tokens", None)
    if pt is None and isinstance(usage, dict): pt = usage.get("prompt_tokens", 0)
    if ct is None and isinstance(usage, dict): ct = usage.get("completion_tokens", 0)
    prompt_tokens = int(pt or 0)
    completion_tokens = int(ct or 0)
    cached = 0
    ptd = getattr(usage, "prompt_tokens_details", None) or (usage.get("prompt_tokens_details") if isinstance(usage, dict) else None)
    if isinstance(ptd, dict):
        cached = int(ptd.get("cached_tokens", 0))
    cost = estimate_cost(model, prompt_tokens, completion_tokens, cached)
    TOK_IN_SUM += prompt_tokens
    TOK_OUT_SUM += completion_tokens
    TOK_IN_CACHED_SUM += cached
    COST_SUM += float(cost)
    print(f"\n[COST] {where} -> model={model} | in={prompt_tokens} (cached={cached}) | out={completion_tokens} | est=${cost:.6f}")

def _wrap_openai_v1():
    try:
        import openai
        comp_obj = openai.resources.chat.completions.Completions
        orig_create = comp_obj.create
        def wrapped_create(self, *args, **kwargs):
            resp = orig_create(self, *args, **kwargs)
            try:
                model = getattr(resp, "model", None) or (resp.get("model") if isinstance(resp, dict) else None)
                usage = getattr(resp, "usage", None) or (resp.get("usage") if isinstance(resp, dict) else None)
                if model and usage:
                    if not isinstance(usage, dict):
                        usage = {
                            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                            "completion_tokens": getattr(usage, "completion_tokens", 0),
                            "prompt_tokens_details": getattr(usage, "prompt_tokens_details", {}) or {},
                        }
                    print_cost(model, usage, where="autogen")
            except Exception:
                pass
            return resp
        comp_obj.create = wrapped_create
        return True
    except Exception:
        return False

def _wrap_openai_v0():
    try:
        import openai
        orig_create = openai.ChatCompletion.create
        def wrapped_create(*args, **kwargs):
            resp = orig_create(*args, **kwargs)
            try:
                model = resp.get("model") if isinstance(resp, dict) else getattr(resp, "model", None)
                usage = resp.get("usage") if isinstance(resp, dict) else getattr(resp, "usage", None)
                if model and usage:
                    print_cost(model, usage, where="autogen")
            except Exception:
                pass
            return resp
        openai.ChatCompletion.create = wrapped_create
        return True
    except Exception:
        return False

def patch_openai_for_cost_logging():
    ok1 = _wrap_openai_v1()
    ok0 = False if ok1 else _wrap_openai_v0()
    if not (ok1 or ok0):
        print("[WARN] Could not patch OpenAI SDK for cost logging (v0/v1).")

# ---------- Task & ZIP context ----------
ALLOWLIST = [
    "README_REPRO_C2.md",
    "DEPLOY_GUIDE.md",
    "backtest_SK/configs/cs_C2_base_1h.yaml",
    "backtest_SK/verify_c2_result.py",
    "backtest_SK/run_c2_1h_1440.sh",
    "reports/c2_repeat_1h_1440_summary.csv",
    "reports/c2_repeat_1h_1440_trades.csv",
]

def read_task_from_file(path: Optional[str]) -> str:
    if not path:
        return ("Запропонуй детальний план вирішення нетривіальної задачі. "
                "Формат: план → кроки → валідація → ризики → висновок.")
    return Path(path).read_text(encoding="utf-8").strip()

def read_text_from_zip(zip_path: str, rel_path: str, max_bytes: int = 20000) -> Optional[str]:
    with ZipFile(zip_path, "r") as zf:
        if rel_path not in zf.namelist():
            return None
        with zf.open(rel_path) as fp:
            data = fp.read(max_bytes + 1)
            text = data.decode("utf-8", errors="replace")
            if len(data) > max_bytes:
                text += "\n\n[...TRUNCATED...]"
            return text

def build_zip_context(zip_path: Optional[str], max_total_bytes: int = 60000) -> str:
    if not zip_path or not os.path.exists(zip_path):
        return ""
    chunks, used = [], 0
    for p in ALLOWLIST:
        t = read_text_from_zip(zip_path, p, max_bytes=20000)
        if not t:
            continue
        block = f"\n### {p}\n```\n{t}\n```"
        b = block.encode("utf-8")
        if used + len(b) > max_total_bytes:
            chunks.append("\n[...ZIP CONTEXT TRUNCATED...]")
            break
        chunks.append(block)
        used += len(b)
    return ("=== PROJECT CONTEXT (from ZIP) ===" + "".join(chunks) + "\n=== END CONTEXT ===") if chunks else ""

# ---------- Helpers ----------
def detect_default_cfg(project_dir: str) -> str:
    p = Path(project_dir).resolve()
    opt2 = p / "configs" / "cs_C2_base_1h.yaml"
    opt1 = p / "backtest_SK" / "configs" / "cs_C2_base_1h.yaml"
    if opt2.exists():
        return "configs/cs_C2_base_1h.yaml"
    return "backtest_SK/configs/cs_C2_base_1h.yaml"

def tuned_name_for(basename: str, explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    stem = basename[:-5] if basename.endswith(".yaml") else basename
    return f"{stem}_tuned.yaml"

# ---------- SAFE project-run tools ----------
SANDBOX = Path("/tmp/agent_tradebot_sandbox").resolve()
VENV = SANDBOX / "venv"

def _run(cmd: str, cwd: Path, timeout: int = 900) -> Tuple[int, str]:
    env = os.environ.copy()
    if (VENV / "bin").exists():
        env["PATH"] = f"{VENV / 'bin'}:{env.get('PATH','')}"
        env["VIRTUAL_ENV"] = str(VENV)
    p = subprocess.run(cmd, cwd=str(cwd), shell=True, capture_output=True, text=True, timeout=timeout, env=env)
    out = (p.stdout or "") + ("\n" + (p.stderr or "") if p.stderr else "")
    return p.returncode, out

def _venv_run(proj_root: Path, venv_here: Path, cmd: str, timeout=900) -> Tuple[int, str]:
    env = os.environ.copy()
    env["PATH"] = f"{(venv_here / 'bin')}:{env.get('PATH','')}"
    env["VIRTUAL_ENV"] = str(venv_here)
    p = subprocess.run(cmd, cwd=str(proj_root), shell=True, capture_output=True, text=True, timeout=timeout, env=env)
    return p.returncode, (p.stdout or "") + ("\n" + (p.stderr or "") if p.stderr else "")

def _ensure_venv(proj_root: Path, venv_here: Path) -> Tuple[bool, str]:
    if not venv_here.exists():
        code, out = _run(f"{sys.executable} -m venv {venv_here}", cwd=proj_root, timeout=300)
        if code != 0:
            return False, out
    code, out = _venv_run(proj_root, venv_here, "python -m pip install --upgrade pip", timeout=300)
    if code != 0:
        return False, out
    code, out = _venv_run(proj_root, venv_here, "pip install pandas numpy pyyaml", timeout=600)
    if code != 0:
        return False, out
    return True, out

def setup_and_run_backtest(path: str = None, zip_path: str = None) -> Dict:
    if not path and zip_path:
        path = zip_path
    if not path:
        return {"ok": False, "step": "path_missing", "log": "Provide path=<zip|dir>."}

    def is_zip(p: str) -> bool:
        return str(p).lower().endswith(".zip") and os.path.isfile(p)

    if is_zip(path):
        SANDBOX.mkdir(parents=True, exist_ok=True)
        with ZipFile(path, "r") as zf:
            zf.extractall(SANDBOX)
        proj_root = SANDBOX
        venv_here = VENV
    else:
        proj_root = Path(path).resolve()
        if not proj_root.exists() or not proj_root.is_dir():
            return {"ok": False, "step": "path_invalid", "log": f"Not found or not a dir: {path}"}
        venv_here = proj_root / ".venv_autogen"

    ok, out = _ensure_venv(proj_root, venv_here)
    if not ok:
        return {"ok": False, "step": "venv_setup", "log": out}

    script = proj_root / "backtest_SK" / "run_c2_1h_1440.sh"
    if not script.exists():
        return {"ok": False, "step": "script_missing", "log": f"Not found: {script}"}
    code, out = _venv_run(proj_root, venv_here, f"bash {script}", timeout=1800)

    verify = proj_root / "backtest_SK" / "verify_c2_result.py"
    if verify.exists():
        vcode, vout = _venv_run(proj_root, venv_here, f"python {verify}", timeout=600)
    else:
        vcode, vout = (1, "verify_c2_result.py not found")

    summary_csv = proj_root / "reports" / "c2_repeat_1h_1440_summary.csv"
    metrics = {}
    if summary_csv.exists():
        try:
            with open(summary_csv, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if rows:
                row = rows[0]
                for k, v in row.items():
                    kl = (k or "").lower()
                    if kl in ("equity_end", "trades", "profit_factor", "max_dd", "win_rate", "wr", "pf"):
                        metrics[k] = v
        except Exception as e:
            metrics = {"parse_error": repr(e)}

    return {
        "ok": code == 0 and vcode == 0,
        "steps": {"run_code": code, "verify_code": vcode},
        "logs_tail": out[-4000:],
        "verify_tail": vout[-2000:],
        "metrics": metrics,
        "sandbox": str(proj_root),
    }

def run_backtest_cfg(project_dir: str, cfg_relpath: str,
                     summary_candidates: Optional[List[str]] = None) -> Dict:
    proj_root = Path(project_dir).resolve()
    if not proj_root.exists() or not proj_root.is_dir():
        return {"ok": False, "step": "path_invalid", "log": f"Not found or not a dir: {project_dir}"}
    venv_here = proj_root / ".venv_autogen"

    ok, out = _ensure_venv(proj_root, venv_here)
    if not ok:
        return {"ok": False, "step": "venv_setup", "log": out}

    script_py = (proj_root / "backtest_SK" / "backtester_core.py")
    if not script_py.exists():
        script_py = proj_root / "backtester_core.py"
    if not script_py.exists():
        return {"ok": False, "step": "script_missing", "log": f"Not found: backtester_core.py under {proj_root}"}

    cfg_path = (proj_root / cfg_relpath).resolve()
    if not cfg_path.exists():
        return {"ok": False, "step": "cfg_missing", "log": f"Config not found: {cfg_path}"}

    try:
        rel_cfg = cfg_path.relative_to(proj_root)
    except Exception:
        rel_cfg = cfg_path

    import time
    t0 = time.time()
    cmd = f"python {script_py} --cfg {rel_cfg}"
    code, out = _venv_run(proj_root, venv_here, cmd, timeout=1800)

    cand = summary_candidates or [
        "summary.csv",
        
    ]
    found = None
    search_dirs = [proj_root / "reports", proj_root / "backtest_SK" / "reports"]
    recents = []
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
        for pth in cand:
            f = (proj_root / pth).resolve()
            if f.exists() and f.is_file():
                found = f
                break

    metrics, csv_row = {}, {}
    if found:
        try:
            with open(found, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if rows:
                row = rows[0]
                csv_row = row
                for k, v in row.items():
                    kl = (k or "").lower()
                    if kl in ("equity_end", "trades", "profit_factor", "max_dd", "win_rate", "wr", "pf"):
                        metrics[k] = v
            # збережемо копію для аудиту
            try:
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                audit = found.parent / f"summary_{ts}.csv"
                if not audit.exists():
                    audit.write_text(found.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                pass
        except Exception as e:
            metrics = {"parse_error": repr(e)}

    return {
        "ok": code == 0,
        "steps": {"run_code": code},
        "cmd": cmd,
        "logs_tail": out[-4000:],
        "metrics": metrics,
        "csv_row": csv_row,
        "summary_path": str(found) if found else None,
        "sandbox": str(proj_root),
    }

def update_yaml_params(project_dir: str, cfg_relpath: str, updates: Dict, dest_relpath: Optional[str] = None) -> Dict:
    try:
        import yaml
    except Exception as e:
        return {"ok": False, "step": "import_yaml_failed", "log": repr(e)}

    proj_root = Path(project_dir).resolve()
    if not proj_root.exists():
        return {"ok": False, "step": "path_invalid", "log": f"Not found dir: {project_dir}"}
    cfg_path = (proj_root / cfg_relpath).resolve()
    if not cfg_path.exists():
        return {"ok": False, "step": "cfg_missing", "log": f"Config not found: {cfg_path}"}

    base_dir = cfg_path.parent
    default_name = tuned_name_for(cfg_path.name, None)
    dest_name = dest_relpath if dest_relpath else str(base_dir / default_name)
    dest_path = (proj_root / dest_name) if not dest_relpath or not Path(dest_relpath).is_absolute() else Path(dest_relpath)
    dest_path = dest_path.resolve()

    if dest_path == cfg_path:
        dest_path = base_dir / tuned_name_for(cfg_path.name, None)

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
        dest_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "step": "yaml_write_error", "log": repr(e)}

    rel_dest = str(dest_path.relative_to(proj_root)) if str(dest_path).startswith(str(proj_root)) else str(dest_path)
    return {"ok": True, "changed": changed, "baseline": str(cfg_path), "tuned": rel_dest}

def diff_yaml(project_dir: str, base_relpath: str, tuned_relpath: str) -> Dict:
    try:
        import yaml
    except Exception as e:
        return {"ok": False, "step": "import_yaml_failed", "log": repr(e)}

    proj_root = Path(project_dir).resolve()
    base = (proj_root / base_relpath).resolve()
    tuned = (proj_root / tuned_relpath).resolve()
    if not base.exists() or not tuned.exists():
        return {"ok": False, "step": "missing", "log": f"base exists={base.exists()}, tuned exists={tuned.exists()}"}

    try:
        b = yaml.safe_load(base.read_text(encoding="utf-8")) or {}
        t = yaml.safe_load(tuned.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return {"ok": False, "step": "yaml_load_error", "log": repr(e)}

    def flatten(d, prefix=""):
        out = {}
        if isinstance(d, dict):
            for k, v in d.items():
                newp = f"{prefix}.{k}" if prefix else str(k)
                out.update(flatten(v, newp))
        else:
            out[prefix] = d
        return out

    fb, ft = flatten(b), flatten(t)
    keys = set(fb) | set(ft)
    changed = {k: {"old": fb.get(k), "new": ft.get(k)} for k in keys if fb.get(k) != ft.get(k)}

    return {"ok": True, "changed": changed, "base": str(base), "tuned": str(tuned), "count": len(changed)}

def read_project_file(rel_path: str, max_bytes: int = 20000) -> str:
    p1 = (Path("/tmp/agent_tradebot_sandbox") / rel_path).resolve()
    candidates = [p1, Path(rel_path).resolve()]
    for p in candidates:
        try:
            if p.exists() and p.is_file():
                data = p.read_bytes()[: max_bytes + 1]
                s = data.decode("utf-8", errors="replace")
                if len(data) > max_bytes:
                    s += "\n\n[...TRUNCATED...]"
                return s
        except Exception:
            pass
    return f"Not found: {rel_path}"

def _fmt_metrics(metrics: Dict) -> str:
    if not metrics:
        return "Метрики не знайдено."
    kmap = {k.lower(): k for k in metrics.keys()}
    def g(*names):
        for n in names:
            if n in kmap:
                return metrics[kmap[n]]
        return None
    eq  = g("equity_end")
    tr  = g("trades")
    pf  = g("profit_factor", "pf")
    dd  = g("max_dd", "max_drawdown_%")
    wr  = g("win_rate", "wr", "win_rate_%")
    lines = []
    if eq is not None: lines.append(f"Equity end: {eq}")
    if tr is not None: lines.append(f"Trades: {tr}")
    if pf is not None: lines.append(f"Profit Factor: {pf}")
    if dd is not None: lines.append(f"Max DD: {dd}")
    if wr is not None: lines.append(f"Win-rate: {wr}")
    return "\n".join(lines) if lines else "Метрики не знайдено у CSV."

# ---------- logging transcript ----------
def _dump_transcript_to_file(group, log_path: str):
    try:
        msgs = getattr(group, "messages", None) or []
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write("=== TRANSCRIPT START ===\n")
            for i, m in enumerate(msgs, 1):
                role = m.get("name") or m.get("role") or "unknown"
                content = m.get("content")
                if isinstance(content, list):
                    content = " ".join([str(x) for x in content])
                lf.write(f"[{i}] {role}:\n{content}\n\n")
            lf.write("=== TRANSCRIPT END ===\n\n")
    except Exception as e:
        print("[WARN] failed to dump transcript:", repr(e))

# ---------- AutoGen ----------
def _unpack_reply_args(args, kwargs):
    self = args[0] if len(args) >= 1 else kwargs.get("self")
    messages = kwargs.get("messages", args[1] if len(args) >= 2 else None)
    sender = kwargs.get("sender", args[2] if len(args) >= 3 else None)
    config = kwargs.get("config", args[3] if len(args) >= 4 else None)
    return self, messages, sender, config

def build_openai_config(model_env: str, default_model: str, temp_default: float, max_tokens: int):
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv(model_env, default_model)
    if not api_key:
        raise SystemExit("Вкажіть OPENAI_API_KEY.")
    return {
        "seed": 42,
        "temperature": float(os.getenv(f"{model_env}_TEMP", temp_default)),
        "max_tokens": int(max_tokens),
        "config_list": [{"model": model, "api_key": api_key}],
    }

def _import_autogen_classes():
    try:
        from autogen import AssistantAgent, UserProxyAgent, GroupChat, GroupChatManager
        return AssistantAgent, UserProxyAgent, GroupChat, GroupChatManager
    except Exception:
        from autogen.agentchat import AssistantAgent, UserProxyAgent, GroupChat, GroupChatManager
        return AssistantAgent, UserProxyAgent, GroupChat, GroupChatManager

def _pick_param_to_tune(project_dir: str, cfg_relpath: str) -> Optional[str]:
    try:
        import yaml
        from collections import deque
        proj = Path(project_dir).resolve()
        cfg = (proj / cfg_relpath).resolve()
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        return None

    out = {}
    dq = deque([('', data)])
    while dq:
        pref, node = dq.popleft()
        if isinstance(node, dict):
            for k, v in node.items():
                newp = f"{pref}.{k}" if pref else str(k)
                dq.append((newp, v))
        else:
            out[pref] = node

    prefer = ["risk", "strategy.risk", "money_management.risk"]
    for k in prefer:
        if k in out and isinstance(out[k], (int, float)):
            return k
    for k, v in out.items():
        if isinstance(v, (int, float)):
            return k
    return None

def _propose_new_value(old, step: float = 0.1):
    try:
        base = float(old)
    except Exception:
        return old
    newv = base * (1.0 + step)
    if base <= 1.0:
        newv = max(1e-5, min(newv, 1.0))
        return round(newv, 6)
    if isinstance(old, int) or abs(round(base) - base) < 1e-9:
        return int(max(1, round(newv)))
    return round(newv, 6)

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

def _score(m: Dict[str, float]) -> float:
    if not m: return -1e9
    eq = m.get('equity_end') or 0.0
    pf = m.get('profit_factor') or 0.0
    dd = abs(m.get('max_dd') or 0.0)
    return float(eq) * (1.0 + float(pf)) / (1.0 + dd/100.0)

def run_multi_agent(task: str, work_path: Optional[str], cycles: int, tuned_cfg_name: str, max_tokens: int):
    AssistantAgent, UserProxyAgent, GroupChat, GroupChatManager = _import_autogen_classes()

    planner_llm = build_openai_config("OPENAI_PLANNER_MODEL", "gpt-4o", temp_default=0.4, max_tokens=max_tokens)
    executor_llm = build_openai_config("OPENAI_EXECUTOR_MODEL", "gpt-4o-mini", temp_default=0.2, max_tokens=max_tokens)
    manager_llm  = build_openai_config("OPENAI_MANAGER_MODEL",  "gpt-4o-mini", temp_default=0.3, max_tokens=max_tokens)

    planner_sys = (
        "Ти — Planner. На ПЕРШОМУ ході дай короткий, структурований план. "
        "Якщо у чаті вже є твій план — відповідай рівно 'PASS' без жодного тексту."
    )
    path_hint = work_path or ""
    base_cfg = detect_default_cfg(path_hint) if path_hint and os.path.isdir(path_hint) else "backtest_SK/configs/cs_C2_base_1h.yaml"
    base_dir = Path(base_cfg).parent
    tuned_cfg = str(base_dir / tuned_cfg_name)

    executor_sys = (
        "НІКОЛИ не пиши плейсхолдери типу [значення] — завжди підставляй реальні значення з CSV. "
        "Ти — Executor/Critic. НЕ дублюй план. Використовуй ТІЛЬКИ зареєстровані інструменти. "
        "Алгоритм:\n"
        f"1) run_backtest_cfg(project_dir='{path_hint}', cfg_relpath='{base_cfg}')\n"
        "2) Виведи метрики (реальні числа) та шлях до summary\n"
        f"3) Внеси правки YAML у НОВИЙ/ОНОВЛЕНИЙ файл через update_yaml_params(..., cfg_relpath='<джерело>', dest_relpath='{tuned_cfg}', ...)\n"
        f"   (джерело: якщо '{tuned_cfg}' існує — використовуй його; інакше — '{base_cfg}')\n"
        f"4) diff_yaml(..., base_relpath='{base_cfg}', tuned_relpath='{tuned_cfg}')\n"
        f"5) run_backtest_cfg(..., cfg_relpath='{tuned_cfg}') і порівняй метрики."
    )

    planner  = AssistantAgent(name="Planner",  system_message=planner_sys,  llm_config=planner_llm)
    executor = AssistantAgent(name="Executor", system_message=executor_sys, llm_config=executor_llm)

    tools = {
        "setup_and_run_backtest": setup_and_run_backtest,
        "read_project_file": read_project_file,
        "run_backtest_cfg": run_backtest_cfg,
        "update_yaml_params": update_yaml_params,
        "diff_yaml": diff_yaml,
    }
    try:
        executor.register_function(tools)
        print("[tools] registered via mapping dict")
    except Exception:
        try:
            for _n, _f in tools.items():
                executor.register_function(_f)
            print("[tools] registered each func individually")
        except Exception:
            fmap = getattr(executor, "_function_map", {})
            fmap.update(tools)
            setattr(executor, "_function_map", fmap)
            print("[tools] injected into _function_map")

    user = UserProxyAgent(
        name="User",
        human_input_mode="NEVER",
        is_termination_msg=lambda x: False,
        system_message="Представляє інтереси замовника.",
        code_execution_config=False,
    )

    baseline_done = {"flag": False}
    def _executor_autorun(*args, **kwargs):
        self, messages, sender, config = _unpack_reply_args(args, kwargs)
        if baseline_done["flag"]:
            return False, None
        baseline_done["flag"] = True
        if not path_hint:
            return True, "Шлях до проекту не задано. Передай --project-dir для запуску бектесту."
        res = run_backtest_cfg(project_dir=path_hint, cfg_relpath=base_cfg)
        m = res.get("metrics") or {}
        summary_path = res.get("summary_path") or "не знайдено"
        cmd = res.get("cmd", "")
        csv_row = res.get("csv_row") or {}
        msg = [
            "Базовий бектест виконано.",
            "Команда запуску: " + cmd,
            "Шлях до summary: " + str(summary_path),
            _fmt_metrics(m)
        ]
        if csv_row:
            extras = {k: v for k, v in csv_row.items()
                      if k.lower() not in ("equity_end","trades","profit_factor","max_dd","win_rate","wr","pf")}
            if extras:
                msg.append("Додаткові поля CSV: " + ", ".join(f"{k}={v}" for k, v in extras.items()))
        return True, "\n".join([x for x in msg if x])

    # register autorun hook across API variants
    try:
        sig = inspect.signature(executor.register_reply)
        names = [p.name for p in sig.parameters.values()]
        if len(names) >= 2 and names[1] == "reply_func":
            def _always_trigger(*args, **kwargs):
                self, messages, sender, config = _unpack_reply_args(args, kwargs)
                return not baseline_done["flag"]
            executor.register_reply(_always_trigger, _executor_autorun)
            print("[hook] autorun (legacy register_reply)")
        else:
            executor.register_reply(_executor_autorun)
            print("[hook] autorun (new register_reply)")
    except Exception as e:
        print("[WARN] Failed to add executor autorun hook:", repr(e))

    # ---- Iteration hook ----
    state = {"iter": 0, "tried": set()}

    def _executor_iterate(*args, **kwargs):
        self, messages, sender, config = _unpack_reply_args(args, kwargs)
        if not baseline_done["flag"]:
            return False, None
        state["iter"] += 1
        print(f"[iterate] turn #{state['iter']} starting")

        src_cfg = tuned_cfg if Path(path_hint, tuned_cfg).exists() else base_cfg
        key = _pick_param_to_tune(path_hint, src_cfg)
        # avoid repeating the same key
        if key in state.get('tried', set()):
            try:
                import yaml
                from collections import deque
                data = yaml.safe_load(Path(path_hint, src_cfg).read_text(encoding="utf-8")) or {}
                flat = {}
                dq = deque([('', data)])
                while dq:
                    pref, node = dq.popleft()
                    if isinstance(node, dict):
                        for k, v in node.items():
                            newp = f"{pref}.{k}" if pref else str(k)
                            dq.append((newp, v))
                    else:
                        flat[pref] = node
                for k2, v2 in flat.items():
                    if isinstance(v2, (int, float)) and k2 not in state['tried']:
                        key = k2; break
            except Exception:
                pass
        state.setdefault('tried', set()).add(key)

        if not key:
            return True, "Не вдалося знайти числовий параметр для тюнінгу у конфігу."

        try:
            import yaml
            data = yaml.safe_load(Path(path_hint, src_cfg).read_text(encoding="utf-8")) or {}
            cur = data
            for k in key.split('.')[:-1]:
                cur = cur.get(k, {})
            old_val = cur.get(key.split('.')[-1])
            if not isinstance(old_val, (int, float)):
                raise ValueError("Обраний параметр не є числовим")
        except Exception as e:
            return True, f"Не вдалося прочитати поточне значення '{key}': {e}"

        new_val = _propose_new_value(old_val, step=0.10)
        updates = {key: new_val}
        print(f"[iterate] tuning '{key}': {old_val} -> {new_val}")
        # ВАЖЛИВО: тепер джерело — src_cfg (tuned якщо існує), а не завжди base_cfg
        ures = update_yaml_params(project_dir=path_hint, cfg_relpath=src_cfg, updates=updates, dest_relpath=tuned_cfg)
        if not ures.get("ok"):
            return True, f"Помилка оновлення YAML: {ures.get('log')}"

        d = diff_yaml(project_dir=path_hint, base_relpath=base_cfg, tuned_relpath=tuned_cfg)
        if not d.get("ok"):
            diff_txt = f"diff: {d.get('log')}"
        else:
            changes = d.get("changed") or {}
            diff_txt = "; ".join(f"{k}: {v['old']} -> {v['new']}" for k, v in changes.items()) or "(немає змін)"

        r2 = run_backtest_cfg(project_dir=path_hint, cfg_relpath=tuned_cfg)
        print(f"[iterate] tuned cmd: {r2.get('cmd')}")
        print(f"[iterate] tuned summary: {r2.get('summary_path')}")
        csv2 = r2.get("csv_row") or {}
        m2 = _norm_metrics(csv2)

        metrics_block = _fmt_metrics({
            k: v for k, v in {
                'equity_end': m2.get('equity_end'),
                'trades': csv2.get('trades'),
                'profit_factor': m2.get('profit_factor'),
                'max_dd': m2.get('max_dd'),
                'win_rate': m2.get('win_rate'),
            }.items() if v is not None
        })

        msg_lines = [
            f"Ітерація #{state['iter']}: тюнінг '{key}': {old_val} → {new_val}",
            f"Джерело для оновлення: {src_cfg}",
            f"Зміни (diff base→tuned): {diff_txt}",
            "Результати (tuned):",
            metrics_block,
            f"Команда запуску tuned: {r2.get('cmd')}",
            f"Summary tuned: {r2.get('summary_path') or 'не знайдено'}",
        ]
        return True, "\n".join([x for x in msg_lines if x])

    # register iteration hook and try to move it first
    try:
        sig2 = inspect.signature(executor.register_reply)
        names2 = [p.name for p in sig2.parameters.values()]
        if len(names2) >= 2 and names2[1] == "reply_func":
            def _iter_trigger(*args, **kwargs):
                self, messages, sender, config = _unpack_reply_args(args, kwargs)
                return baseline_done["flag"]
            executor.register_reply(_iter_trigger, _executor_iterate)
            print("[hook] iterate (legacy register_reply)")
        else:
            executor.register_reply(_executor_iterate)
            print("[hook] iterate (new register_reply)")
        try:
            rf = getattr(executor, "_reply_funcs", None)
            if isinstance(rf, list):
                for i, t in enumerate(rf):
                    f = t.get("reply_func") if isinstance(t, dict) else None
                    if getattr(f, "__name__", "") == "_executor_iterate":
                        rf.insert(0, rf.pop(i))
                        print("[hook] iterate moved to front")
                        break
        except Exception as e:
            print("[WARN] cannot reorder hooks:", repr(e))
    except Exception as e:
        print("[WARN] Failed to add iterate hook:", repr(e))

    max_round = max(2, int(cycles) * 2)
    group = GroupChat(agents=[user, planner, executor], messages=[], max_round=max_round, speaker_selection_method="round_robin")
    manager = GroupChatManager(groupchat=group, llm_config=manager_llm,
        system_message=f"Ти — Модератор. Planner→Executor до {max_round} ходів; заверши після цього.")

    print("\n=== TASK ===\n", task[:4000], "\n")
    tool_hint = ""\
        + (f"\n\n[TOOL HINT] baseline: run_backtest_cfg(project_dir='{path_hint}', cfg_relpath='{base_cfg}')" if path_hint else "")\
        + (f"\n[TOOL HINT] tuned: update_yaml_params(project_dir='{path_hint}', cfg_relpath='<src>', dest_relpath='{tuned_cfg}', updates={{...}})" if path_hint else "")
    user.initiate_chat(manager, message=task + tool_hint)

    log_path = os.getenv('CHAT_LOG_FILE')
    if not log_path:
        log_path = f"agent_chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    _dump_transcript_to_file(group, log_path)

def single_call(task: str, max_tokens: int):
    try:
        from openai import OpenAI
    except Exception:
        import openai
        model = os.getenv("OPENAI_EXECUTOR_MODEL", "gpt-4o-mini")
        sys_prompt = (
            "Ти — системний архітектор. Формат: 1) короткий план (буліти), "
            "2) кроки виконання, 3) перевірка/валідація, 4) ризики/обмеження, 5) висновок (3–5 речень)."
        )
        print("\n=== SINGLE CALL (", model, ") ===\n")
        resp = openai.ChatCompletion.create(
            model=model,
            temperature=float(os.getenv("OPENAI_EXECUTOR_MODEL_TEMP", "0.2")),
            max_tokens=int(max_tokens),
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": task},
            ],
        )
        text = resp["choices"][0]["message"]["content"].strip()
        print(text)
        usage = resp.get("usage", {})
        print_cost(model, usage, where="single")
        return

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    model = os.getenv("OPENAI_EXECUTOR_MODEL", "gpt-4o-mini")
    sys_prompt = (
        "Ти — системний архітектор. Формат: 1) короткий план (буліти), "
        "2) кроки виконання, 3) перевірка/валідація, 4) ризики/обмеження, 5) висновок (3–5 речень)."
    )
    print("\n=== SINGLE CALL (", model, ") ===\n")
    resp = client.chat.completions.create(
        model=model,
        temperature=float(os.getenv("OPENAI_EXECUTOR_MODEL_TEMP", "0.2")),
        max_tokens=int(max_tokens),
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": task},
        ],
    )
    print(resp.choices[0].message.content.strip())
    usage = resp.usage
    usage_dict = {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
        "completion_tokens": getattr(usage, "completion_tokens", 0),
        "prompt_tokens_details": getattr(usage, "prompt_tokens_details", {}) or {},
    }
    print_cost(model, usage_dict, where="single")

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--task-file", type=str, default=os.getenv("TASK_FILE"))
    p.add_argument("--code-zip", type=str, default=os.getenv("CODE_ZIP"))
    p.add_argument("--project-dir", type=str, help="Path to unpacked project directory (alternative to --code-zip)" )
    p.add_argument("--prerun", action="store_true", help="Run a backtest before starting the chat" )
    p.add_argument("--cycles", type=int, default=int(os.getenv("CYCLES", 2)), help="Number of Planner→Executor cycles (default 2)" )
    p.add_argument("--tuned-config-name", type=str, default=os.getenv("TUNED_CONFIG_NAME", "cs_C2_tuned_1h.yaml"),
                   help="Filename for tuned YAML (created next to baseline)" )
    p.add_argument("--max-tokens", type=int, default=int(os.getenv("OPENAI_MAX_TOKENS", "700")),
                   help="LLM max tokens per response (overrides OPENAI_MAX_TOKENS env)")
    p.add_argument("--log-file", type=str, default=os.getenv("CHAT_LOG_FILE"),
                   help="Path to save full chat transcript & cost summary")
    p.add_argument("--uah-rate", type=float, default=float(os.getenv("UAH_RATE", "40")),
                   help="UAH per 1 USD for cost conversion (default from env UAH_RATE or 40)")
    return p.parse_args()

def main():
    global UAH_RATE
    os.environ.setdefault("AUTOGEN_USE_DOCKER", "0")
    patch_openai_for_cost_logging()
    args = parse_args()
    UAH_RATE = float(args.uah_rate)

    log_path = args.log_file or f"agent_chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    os.environ["CHAT_LOG_FILE"] = log_path

    task_text = read_task_from_file(args.task_file)
    zip_ctx = build_zip_context(args.code_zip) if args.code_zip else ""
    final_task = task_text + ("\n\n" + zip_ctx if zip_ctx else "")

    work_path = args.code_zip or args.project_dir
    max_tokens = args.max_tokens

    prerun_flag = args.prerun or os.getenv("PRERUN_BACKTEST", "0").lower() in ("1", "true", "yes")
    if work_path and prerun_flag:
        try:
            if os.path.isdir(work_path):
                cfg = detect_default_cfg(work_path)
                res = run_backtest_cfg(work_path, cfg)
            else:
                res = setup_and_run_backtest(path=work_path)
            short = f"\n\n=== PRE-RUN RESULT ===\nOK: {res.get('ok')} | METRICS: {res.get('metrics')} | SUMMARY: {res.get('summary_path', 'n/a')} | SANDBOX: {res.get('sandbox')}\n"
            final_task = final_task + short
        except Exception as e:
            final_task = final_task + f"\n\n[PRE-RUN ERROR] {e}\n"

    if os.getenv("SINGLE_CALL", "0").lower() in ("1", "true", "yes"):
        try:
            single_call(final_task, max_tokens=max_tokens); return
        except Exception as e:
            print("SINGLE_CALL failed, trying multi-agent...\nReason:", repr(e))

    try:
        run_multi_agent(final_task, work_path=work_path, cycles=args.cycles, tuned_cfg_name=args.tuned_config_name, max_tokens=max_tokens)
    except Exception as e:
        msg = str(e)
        if any(k in msg for k in ("insufficient_quota", "RateLimitError", "429")):
            print("\n[ERROR] OpenAI quota/plan issue.\n- Billing: https://platform.openai.com/account/billing/overview\n- Try SINGLE_CALL=1 and/or reduce OPENAI_MAX_TOKENS.\n")
        else:
            print("\n[ERROR] Unexpected LLM error:\n"); traceback.print_exc()

    if TOK_IN_SUM or TOK_OUT_SUM:
        print("\n=== COST SUMMARY ===")
        print(f"input_tokens={TOK_IN_SUM} (cached={TOK_IN_CACHED_SUM}), output_tokens={TOK_OUT_SUM}")
        print(f"estimated_total_cost=${COST_SUM:.6f}  (~₴{COST_SUM*UAH_RATE:.2f} @ {UAH_RATE} UAH/USD)\n")

    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write("=== RUN ARGS ===\n")
            lf.write(f"project_dir={work_path}\n")
            lf.write(f"cycles={args.cycles}\n")
            lf.write(f"max_tokens={args.max_tokens}\n")
            lf.write(f"tuned_config_name={args.tuned_config_name}\n")
            lf.write(f"uah_rate={UAH_RATE}\n\n")
            if TOK_IN_SUM or TOK_OUT_SUM:
                lf.write("=== COST SUMMARY ===\n")
                lf.write(f"input_tokens={TOK_IN_SUM} (cached={TOK_IN_CACHED_SUM}), output_tokens={TOK_OUT_SUM}\n")
                lf.write(f"estimated_total_cost=${COST_SUM:.6f}  (~₴{COST_SUM*UAH_RATE:.2f} @ {UAH_RATE} UAH/USD)\n\n")
        print(f"[log] transcript & cost summary → {log_path}")
    except Exception as e:
        print("[WARN] failed to write log file:", repr(e))

if __name__ == "__main__":
    main()
