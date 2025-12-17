#!/usr/bin/env python3
# auto_tuner_rays2grid_v3_fix_cryptomine_mtm.py
# Derived from auto_tuner_rays2grid_v3_fix.py, adapted for Cryptomine + MTM backtester.
#
# Changes vs original:
# - BACKTESTER points to backtester_core_speed3_veto_universe_4_mtm_unrealized.py by default.
# - Adds --backtester to override.
# - Disables plot generation by default, and provides --final-plots to opt-in.
# - No universe optimization logic (this tuner never tuned universe; we keep it that way).
#
# Usage example:
#   python3 auto_tuner_rays2grid_v3_fix_cryptomine_mtm.py \
#     --cfg configs/cfg_cryptomine_c_limit14_robust_1m.yaml \
#     --limit-bars 288000 \
#     --plan tuner_plan_cryptomine_1m_smoke.py \
#     --prefix cm1m_smoke \
#     --min-trades 40 --target-trades 220 \
#     --w-equity 1.0 --w-pf 12.0 --w-dd 220.0 --w-mono 5.0 --dd-target 0.12 \
#     --sleep-sec 0.0 --jobs 1
#
import argparse, itertools, re, subprocess, sys, time, csv, os
from pathlib import Path
from datetime import datetime
import yaml, copy
from concurrent.futures import ProcessPoolExecutor, as_completed

DEFAULT_BACKTESTER = Path("backtester_core_speed3_veto_universe_4_mtm_unrealized.py")

INIT_CFG = None
GLOBAL_BEST_S = -1e18
GLOBAL_BEST_REC = None
BT_SLEEP_SEC = 0
BT_CACHE = {}
SESSION_DIR = None

DELTA_MODE_DEFAULT = True  # interpret grid lists as deltas around current by default
TUNE_ROOT = Path("_reports") / "tune"
TUNE_TMP_ROOT = TUNE_ROOT / "tmp"

# Clamp only what we touch (safe); everything else passes through.
PARAM_CLAMPS = {
    "strategy_params.tpPercent": (0.05, 10.0),
    "strategy_params.callbackPercent": (0.01, 5.0),
    "strategy_params.subSellTPPercent": (0.1, 10.0),
    "strategy_params.linearDropPercent": (0.05, 10.0),
    "strategy_params.marginCallLimit": (10, 5000),
    "strategy_params.maxFillsPerBar": (1, 50),
    "strategy_params.maxSignalsWindow": (1, 200),
    "strategy_params.windowBars": (1, 500),
    "strategy_params.firstBuyUSDT": (0.5, 2000.0),
    "strategy_params.drop1": (0.01, 20.0),
    "strategy_params.drop2": (0.01, 20.0),
    "strategy_params.drop3": (0.01, 20.0),
    "strategy_params.drop4": (0.01, 20.0),
    "strategy_params.drop5": (0.01, 20.0),
    "strategy_params.mult2": (0.1, 20.0),
    "strategy_params.mult3": (0.1, 20.0),
    "strategy_params.mult4": (0.1, 20.0),
    "strategy_params.mult5": (0.1, 50.0),
}

def _clamp_param(pname, val):
    lo, hi = PARAM_CLAMPS.get(pname, (-1e18, 1e18))
    try:
        v = float(val)
        v = int(v) if v.is_integer() else v
    except Exception:
        return val
    return max(lo, min(hi, v))

KV_RE = re.compile(
    r'(?:\x1b\[[0-9;]*m)?(equity_end|pf|profit_factor|max_dd|mono|monotonicity|trades|apr|daily_ret|monthly_ret|yearly_ret|fees)\s*=\s*([-+]?[0-9]*\.?[0-9]+)',
    re.IGNORECASE,
)

def parse_metrics(text: str):
    out = {}
    for k, v in KV_RE.findall(text):
        if k == 'pf':
            k = 'profit_factor'
        if k == 'mono':
            k = 'monotonicity'
        if k in (
            'equity_end',
            'profit_factor',
            'max_dd',
            'monotonicity',
            'apr',
            'daily_ret',
            'monthly_ret',
            'yearly_ret',
            'fees',
        ):
            out[k] = float(v)
        elif k == 'trades':
            try:
                out[k] = int(float(v))
            except Exception:
                out[k] = int(v)
    return out if out else None

def run_backtest(backtester: Path, cfg_path: Path, limit_bars: int, with_plots: bool = False, plots_dir: Path | None = None):
    yaml_text = Path(cfg_path).read_text()
    key = (str(backtester), limit_bars, yaml_text)
    if not with_plots and key in BT_CACHE:
        return BT_CACHE[key]

    cmd = [
        sys.executable,
        str(backtester),
        "--cfg", str(cfg_path),
        "--limit-bars", str(limit_bars),
        "--export-csv",
    ]
    if with_plots:
        if plots_dir is None:
            plots_dir = Path("_reports") / "_bt_plots"
        plots_dir.mkdir(parents=True, exist_ok=True)
        cmd += ["--plots", str(plots_dir)]

    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    stats = parse_metrics(out) or {}
    if not stats:
        raise RuntimeError(f"Could not parse metrics from backtester output. Tail: {out[-900:]}")

    trades_csv = None
    for line in out.splitlines():
        if "bt_trades=" in line:
            m = re.search(r"bt_trades=([^\s]+)", line)
            if m:
                trades_csv = m.group(1)
            break
    stats["trades_csv"] = trades_csv
    stats["elapsed_sec"] = elapsed

    if not with_plots:
        BT_CACHE[key] = stats
    if with_plots and BT_SLEEP_SEC > 0:
        time.sleep(BT_SLEEP_SEC)
    return stats

def _eval_one(args_tuple):
    backtester, cfg_yaml_path, limit_bars = args_tuple
    start_ts = time.time()
    try:
        res = run_backtest(backtester, cfg_yaml_path, limit_bars, with_plots=False)
    except Exception as e:
        elapsed = time.time() - start_ts
        return {
            "equity_end": 100.0,
            "profit_factor": 0.0,
            "max_dd": 0.0,
            "monotonicity": 0.0,
            "trades": 0,
            "error": str(e),
            "elapsed_sec": elapsed,
        }
    res.setdefault("elapsed_sec", time.time() - start_ts)
    return res

def _green_if(text: str, cond: bool) -> str:
    try:
        return ("\x1b[32m" + text + "\x1b[0m") if cond else text
    except Exception:
        return text

def read_yaml(p: Path): return yaml.safe_load(p.read_text())
def write_yaml(obj, p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(obj, sort_keys=False), encoding="utf-8")

def _tmp_dir_for_session(session_dir):
    session_path = Path(session_dir)
    try:
        rel = session_path.relative_to(TUNE_ROOT)
    except ValueError:
        rel = session_path.name
    return TUNE_TMP_ROOT / rel

def deep_get(d, key):
    cur = d
    for k in key.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur

def deep_set(d, key, val):
    cur = d
    parts = key.split(".")
    for k in parts[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[parts[-1]] = val

def prune_reports(prefix: str, keep: int = 60):
    import shutil
    root = Path("_reports/_backtest")
    pats = sorted([p for p in root.glob(f"backtest{prefix}*") if p.is_dir()],
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for p in pats[keep:]:
        shutil.rmtree(p, ignore_errors=True)

# Aliases: keep simple, tune only cryptomine params.
ALIASES = {
    "tp": ["strategy_params.tpPercent"],
    "cb": ["strategy_params.callbackPercent"],
    "subtp": ["strategy_params.subSellTPPercent"],
    "lin-drop": ["strategy_params.linearDropPercent"],
    "mcl": ["strategy_params.marginCallLimit"],
    "fills": ["strategy_params.maxFillsPerBar"],
    "sigwin": ["strategy_params.maxSignalsWindow"],
    "winbars": ["strategy_params.windowBars"],
    "buy0": ["strategy_params.firstBuyUSDT"],
    "d1": ["strategy_params.drop1"],
    "d2": ["strategy_params.drop2"],
    "d3": ["strategy_params.drop3"],
    "d4": ["strategy_params.drop4"],
    "d5": ["strategy_params.drop5"],
    "m2": ["strategy_params.mult2"],
    "m3": ["strategy_params.mult3"],
    "m4": ["strategy_params.mult4"],
    "m5": ["strategy_params.mult5"],
}

def get_current(cfg, pname):
    for key in ALIASES.get(pname, [pname]):
        v = deep_get(cfg, key)
        if v is not None:
            return v, key
    return None, None

def set_param(cfg, pname, value):
    for key in ALIASES.get(pname, [pname]):
        try:
            deep_set(cfg, key, value)
            return key
        except Exception:
            continue
    return None

# --------- scoring ---------

def risk_averse_score(r, w_equity=1.0, w_pf=10.0, w_dd=200.0, w_mono=5.0, dd_target=0.12,
                      min_trades=50, target_trades=300):
    eq = float(r.get("equity_end", 100.0))
    ret = eq - 100.0
    pf = float(r.get("profit_factor", 0.0))
    dd = float(r.get("max_dd", 1.0))
    mono = float(r.get("monotonicity", 0.0))
    trades = int(r.get("trades", 0))

    if trades == 0 or (pf == 0.0 and dd == 0.0):
        return -1e9

    dd_penalty = max(0.0, dd - dd_target)
    base = (w_equity * ret) + (w_pf * (pf - 1.0)) - (w_dd * dd_penalty) + (w_mono * mono)

    low = max(0, min_trades - trades)
    base -= 50.0 * low
    t_factor = min(1.0, trades / float(target_trades))
    return base * (0.5 + 0.5 * t_factor)

def score_rec(rec, weights, min_trades, target_trades):
    s = risk_averse_score(rec, *weights, min_trades=min_trades, target_trades=target_trades)
    rec = dict(rec); rec['score'] = s; return rec

def pick_best(recs, weights, min_trades, target_trades):
    best = None; best_s = -1e18
    for r in recs:
        s = risk_averse_score(r, *weights, min_trades=min_trades, target_trades=target_trades)
        r["score"] = s
        if s > best_s:
            best_s, best = s, r
    return best

# --------- search helpers ---------

def ensure_included(values, cur):
    try:
        curf = float(cur)
    except Exception:
        return list(values)
    out, seen = [], False
    for v in values:
        try:
            vf = float(v)
        except Exception:
            continue
        if abs(vf - curf) < 1e-12:
            seen = True
        out.append(vf)
    if not seen:
        out.append(curf)
    return sorted(set(out))

def around(val, step, n=1):
    xs = [val]
    for k in range(1, n+1):
        xs += [round(val - k*step, 10), round(val + k*step, 10)]
    return sorted(set([x for x in xs if isinstance(x,(int,float)) and x > 0]))

def realize_around(spec, current):
    if isinstance(spec, str) and spec.startswith("around:"):
        step = float(spec.split(":")[1])
        try:
            c = float(current)
        except Exception:
            return [current]
        return around(c, step, n=1)
    return spec

def _as_list(x):
    if isinstance(x, (list, tuple, set)):
        return list(x)
    return [x]

def include_seed_values(values, pname, current_value):
    try:
        init_val, _ = get_current(INIT_CFG, pname) if INIT_CFG is not None else (None, None)
    except Exception:
        init_val = None
    vals = list(values) if isinstance(values,(list,tuple,set)) else ([values] if values is not None else [])
    vals = [_clamp_param(pname, v) for v in vals]
    if current_value is not None:
        cur_clamped = _clamp_param(pname, current_value)
        if cur_clamped not in vals:
            vals.append(cur_clamped)
    if init_val is not None:
        init_clamped = _clamp_param(pname, init_val)
        if init_clamped not in vals:
            vals.append(init_clamped)
    try:
        vals = sorted(set(float(x) for x in vals))
    except Exception:
        try:
            vals = sorted(set(vals))
        except Exception:
            vals = list(dict.fromkeys(vals))
    return list(vals)

def _make_grid_candidates(pname, spec, current, delta_mode=DELTA_MODE_DEFAULT):
    vals = realize_around(spec, current)
    if isinstance(vals, (list, tuple, set)):
        vals = _as_list(vals)
        out = []
        for v in vals:
            if delta_mode and isinstance(v, (int, float)):
                try:
                    curf = float(current)
                except Exception:
                    curf = 0.0
                out.append(_clamp_param(pname, curf + float(v)))
            else:
                out.append(_clamp_param(pname, v))
        try:
            curf = _clamp_param(pname, float(current))
            if curf not in out:
                out.append(curf)
        except Exception:
            if current not in out:
                out.append(current)
        try:
            out = sorted(set(float(x) for x in out))
        except Exception:
            out = list(dict.fromkeys(out))
        return out
    else:
        if delta_mode:
            try:
                curf = float(current)
                vf = float(vals)
                cand = [
                    _clamp_param(pname, curf + vf),
                    _clamp_param(pname, curf),
                ]
            except Exception:
                cand = [current]
            return list(dict.fromkeys(cand))
        else:
            return include_seed_values([vals], pname, current)

# --------- RAYS / GRID ---------

def do_rays(backtester, base_cfg, limit_bars, pname, cand, prefix, session_dir, log_csv, weights, min_trades, target_trades, jobs):
    global GLOBAL_BEST_S, GLOBAL_BEST_REC
    cur, _ = get_current(base_cfg, pname)
    cand = ensure_included(cand, cur) if isinstance(cand, (list,tuple)) else ([cur] if cur is not None else [])
    cand = include_seed_values(cand, pname, cur)

    recs = []
    tasks = []

    session_path = Path(session_dir)
    tmp_base = _tmp_dir_for_session(session_path)
    tmp_base.mkdir(parents=True, exist_ok=True)

    for v in cand:
        cfg = copy.deepcopy(base_cfg)
        set_param(cfg, pname, v)
        tmp = tmp_base / f"{prefix}_{pname}_{str(v).replace('.','p')}.yaml"
        write_yaml(cfg, tmp)
        tasks.append((tmp, v))

    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            fut_map = {ex.submit(_eval_one, (backtester, tmp, limit_bars)): (tmp, v) for tmp, v in tasks}
            for fut in as_completed(fut_map):
                tmp, v = fut_map[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"equity_end":100.0,"profit_factor":0.0,"max_dd":0.0,"monotonicity":0.0,"trades":0,"error":str(e)}
                res.update({"param":pname,"value":v,"cfg_path":str(tmp),"yaml":str(tmp),"ts":datetime.utcnow().isoformat(timespec="seconds")})
                recs.append(res)
    else:
        for tmp, v in tasks:
            res = _eval_one((backtester, tmp, limit_bars))
            res.update({"param":pname,"value":v,"cfg_path":str(tmp),"yaml":str(tmp),"ts":datetime.utcnow().isoformat(timespec="seconds")})
            recs.append(res)

    best = pick_best(recs, weights, min_trades, target_trades)
    cur_rec = next((r for r in recs if r.get('value') == cur), None)
    if cur_rec and best.get('score', -1e18) < cur_rec.get('score', -1e18):
        best = cur_rec

    set_param(base_cfg, pname, best["value"])
    best_yaml = Path(session_dir) / f"{prefix}_{pname}_best.yaml"
    write_yaml(base_cfg, best_yaml)

    with open(log_csv, "a", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["ts","param","value","equity_end","profit_factor","max_dd","monotonicity","trades","elapsed_sec","yaml","score"], extrasaction="ignore")
        if f.tell()==0: wr.writeheader()
        for r in recs: wr.writerow(r)

    print(f"[rays] BEST {pname}={best['value']} score={best['score']:.6f} (E={best.get('equity_end')} PF={best.get('profit_factor')} DD={best.get('max_dd')} T={best.get('trades')})")
    return base_cfg, recs

def do_grid(backtester, base_cfg, limit_bars, params, prefix, session_dir, log_csv, weights, min_trades, target_trades, jobs):
    global GLOBAL_BEST_S, GLOBAL_BEST_REC
    cand_lists = {}
    for p, spec in params.items():
        cur, _ = get_current(base_cfg, p)
        vals = _make_grid_candidates(p, spec, cur, delta_mode=DELTA_MODE_DEFAULT)
        vals = include_seed_values(vals, p, cur)
        cand_lists[p] = vals

    keys = list(cand_lists.keys())
    if not keys:
        print("[grid] no parameters provided; skipping")
        return base_cfg, []

    print("[grid] candidates:")
    for k in keys:
        print(f"  - {k}: {cand_lists[k]}")

    grid = list(itertools.product(*[cand_lists[k] for k in keys]))
    if not grid:
        print("[grid] no combinations; skipping")
        return base_cfg, []

    session_path = Path(session_dir)
    tmp_base = _tmp_dir_for_session(session_path)
    tmp_base.mkdir(parents=True, exist_ok=True)

    tasks = []
    for vec in grid:
        cfg = copy.deepcopy(base_cfg)
        for k, v in zip(keys, vec):
            set_param(cfg, k, v)
        tmp = tmp_base / f"{prefix}_grid_{'_'.join(str(x).replace('.','p') for x in vec)}.yaml"
        write_yaml(cfg, tmp)
        tasks.append((tmp, vec))

    recs = []
    local_best_s = -1e18
    prev_best = GLOBAL_BEST_S

    def _fmt_val(val):
        if isinstance(val, float):
            return f"{val:.6f}"
        return str(val)

    def _process(vec, res):
        nonlocal local_best_s
        score = risk_averse_score(res, *weights, min_trades=min_trades, target_trades=target_trades)
        res["score"] = score
        if score > local_best_s:
            local_best_s = score
            params_str = ", ".join(f"{k}={_fmt_val(v)}" for k, v in zip(keys, vec))
            print(f"[grid][new-best] score={score:.6f} {params_str} | E={res.get('equity_end')} PF={res.get('profit_factor')} DD={res.get('max_dd')} T={res.get('trades')}")
        recs.append(res)

    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            fut_map = {ex.submit(_eval_one, (backtester, tmp, limit_bars)): (tmp, vec) for tmp, vec in tasks}
            for fut in as_completed(fut_map):
                tmp, vec = fut_map[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"equity_end":100.0,"profit_factor":0.0,"max_dd":0.0,"monotonicity":0.0,"trades":0,"error":str(e)}
                res.update({"param":"|".join(keys),"value":"|".join(map(str, vec)),"cfg_path":str(tmp),"yaml":str(tmp),"ts":datetime.utcnow().isoformat(timespec="seconds")})
                _process(vec, res)
    else:
        for tmp, vec in tasks:
            res = _eval_one((backtester, tmp, limit_bars))
            res.update({"param":"|".join(keys),"value":"|".join(map(str, vec)),"cfg_path":str(tmp),"yaml":str(tmp),"ts":datetime.utcnow().isoformat(timespec="seconds")})
            _process(vec, res)

    chosen = pick_best(recs, weights, min_trades, target_trades)
    for k, v in zip(keys, chosen["value"].split("|")):
        try:
            v2 = float(v)
            v2 = int(v2) if v2.is_integer() else v2
        except Exception:
            v2 = v
        set_param(base_cfg, k, v2)

    write_yaml(base_cfg, Path(session_dir) / f"{prefix}_grid_best.yaml")
    with open(log_csv, "a", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["ts","param","value","equity_end","profit_factor","max_dd","monotonicity","trades","elapsed_sec","yaml","score"], extrasaction="ignore")
        if f.tell()==0: wr.writeheader()
        for r in recs: wr.writerow(r)

    if chosen.get('score', -1e18) > GLOBAL_BEST_S:
        GLOBAL_BEST_S, GLOBAL_BEST_REC = chosen['score'], chosen

    line = f"[grid] BEST {chosen['value']} score={chosen.get('score'):.6f} (E={chosen.get('equity_end')} PF={chosen.get('profit_factor')} DD={chosen.get('max_dd')} T={chosen.get('trades')})"
    print(_green_if(line, (chosen.get('score', -1e18) > prev_best)))
    return base_cfg, recs

def default_plan(limit_bars: int = None):
    # fallback
    return [
        ("rays", {"tp": ["around:0.2"]}),
        ("rays", {"subtp": ["around:0.2"]}),
        ("grid", {"tp": "around:0.1", "subtp": "around:0.1"}),
    ]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--limit-bars", type=int, default=288000)
    ap.add_argument("--prefix", default="cm1m")
    ap.add_argument("--backtester", default=str(DEFAULT_BACKTESTER))
    ap.add_argument("--w-equity", type=float, default=1.0)
    ap.add_argument("--w-pf", type=float, default=12.0)
    ap.add_argument("--w-dd", type=float, default=220.0)
    ap.add_argument("--w-mono", type=float, default=5.0)
    ap.add_argument("--dd-target", type=float, default=0.12)
    ap.add_argument("--min-trades", type=int, default=40)
    ap.add_argument("--target-trades", type=int, default=220)
    ap.add_argument("--plan", help="Path to external plan module (default_plan used if omitted)")
    ap.add_argument("--sleep-sec", type=float, default=0.0)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--final-plots", action="store_true", help="Generate plots for top candidates at the end (OFF by default).")
    args = ap.parse_args()

    backtester = Path(args.backtester)

    global INIT_CFG, BT_SLEEP_SEC
    INIT_CFG = read_yaml(Path(args.cfg))
    BT_SLEEP_SEC = args.sleep_sec
    weights = (args.w_equity, args.w_pf, args.w_dd, args.w_mono, args.dd_target)

    prefix_path = Path(args.prefix)
    if prefix_path.is_absolute():
        prefix_path = Path(*prefix_path.parts[1:])
    file_prefix = prefix_path.name or "tuner"
    session_ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    base_parent = TUNE_ROOT / prefix_path.parent
    base_parent.mkdir(parents=True, exist_ok=True)
    session_dir = base_parent / f"{file_prefix}_{session_ts}"
    counter = 1
    while session_dir.exists():
        session_dir = base_parent / f"{file_prefix}_{session_ts}_{counter}"
        counter += 1
    session_dir.mkdir(parents=True, exist_ok=False)
    global SESSION_DIR
    SESSION_DIR = session_dir

    base = read_yaml(Path(args.cfg))

    # Baseline
    baseline_yaml = session_dir / f"{file_prefix}_baseline.yaml"
    write_yaml(base, baseline_yaml)
    base_res = run_backtest(backtester, baseline_yaml, args.limit_bars, with_plots=False)
    baseline = {
        "equity_end": float(base_res.get("equity_end", 100.0)),
        "profit_factor": float(base_res.get("profit_factor", 0.0)),
        "max_dd": float(base_res.get("max_dd", 0.0)),
        "monotonicity": float(base_res.get("monotonicity", 0.0)),
        "trades": int(base_res.get("trades", 0)),
        "elapsed_sec": float(base_res.get("elapsed_sec", 0.0)),
        "yaml": str(baseline_yaml),
        "param": "baseline",
        "value": "baseline",
    }
    base_scored = score_rec(baseline, weights, args.min_trades, args.target_trades)
    global GLOBAL_BEST_S, GLOBAL_BEST_REC
    GLOBAL_BEST_S, GLOBAL_BEST_REC = base_scored["score"], base_scored
    print(f"[baseline] equity_end={baseline['equity_end']:.6f} trades={baseline['trades']} "
          f"pf={baseline['profit_factor']:.6f} max_dd={baseline['max_dd']:.6f} "
          f"mono={baseline['monotonicity']:.6f} score={GLOBAL_BEST_S:.6f}")

    log_csv = session_dir / f"{file_prefix}_tuner_log.csv"

    # Load plan
    if args.plan:
        import importlib.util
        _pp = Path(args.plan)
        if not _pp.exists():
            raise FileNotFoundError(f"--plan file not found: {_pp}")
        spec = importlib.util.spec_from_file_location("user_tuner_plan", str(_pp))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot import plan module from {_pp}")
        _mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_mod)
        if not hasattr(_mod, "default_plan"):
            raise AttributeError(f"Plan module {_pp} has no default_plan(limit_bars)")
        if hasattr(_mod, "GRID_VALUES_ARE_DELTAS"):
            global DELTA_MODE_DEFAULT
            DELTA_MODE_DEFAULT = bool(getattr(_mod, "GRID_VALUES_ARE_DELTAS"))
        plan = _mod.default_plan(args.limit_bars)
    else:
        plan = default_plan(args.limit_bars)

    rays_results_all = []
    grid_results_all = []

    for i, stage in enumerate(plan, 1):
        mode = params = None
        if isinstance(stage, (list, tuple)) and len(stage) >= 2:
            mode, params = stage[0], stage[1]
        elif isinstance(stage, dict) and len(stage) == 1:
            mode, params = next(iter(stage.items()))
        else:
            print(f"[plan] malformed stage {i}: {stage}; skipping")
            continue

        if not params:
            print(f"[{mode}] empty params stage {i}; skipping")
            continue

        prefix = f"{file_prefix}_s{i}_{mode}"

        if mode == "rays":
            (pname, cand) = list(params.items())[0]
            base, rays_results = do_rays(
                backtester, base, args.limit_bars, pname, cand, prefix, session_dir, log_csv,
                weights, args.min_trades, args.target_trades, args.jobs
            )
            rays_results_all += rays_results
        elif mode == "grid":
            base, grid_results = do_grid(
                backtester, base, args.limit_bars, params, prefix, session_dir, log_csv,
                weights, args.min_trades, args.target_trades, args.jobs
            )
            grid_results_all += grid_results
        else:
            raise ValueError(mode)

    final = session_dir / f"{file_prefix}_final_best.yaml"
    write_yaml(base, final)
    print(f"DONE -> {final}")

    # Optional plots (OFF by default, per your request)
    if args.final_plots:
        plots_dir = Path(session_dir) / "_final_plots"
        plots_dir.mkdir(parents=True, exist_ok=True)
        TOPK = 2
        if rays_results_all:
            best = sorted(rays_results_all, key=lambda r: r.get("score", -1e18), reverse=True)[:TOPK]
            for j, r in enumerate(best, 1):
                run_backtest(backtester, Path(r["cfg_path"]), args.limit_bars, with_plots=True, plots_dir=plots_dir)
        if grid_results_all:
            best_grid = pick_best(grid_results_all, weights, args.min_trades, args.target_trades)
            run_backtest(backtester, Path(best_grid["cfg_path"]), args.limit_bars, with_plots=True, plots_dir=plots_dir)

    prune_reports(args.prefix)
    tmp_dir = _tmp_dir_for_session(session_dir)
    if tmp_dir.exists():
        for p in tmp_dir.glob("*.yaml"):
            p.unlink(missing_ok=True)

if __name__ == "__main__":
    main()
