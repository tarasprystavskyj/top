#!/usr/bin/env python3
# auto_tuner_rays2grid_v4_exp.py

import argparse, itertools, re, subprocess, sys, time, csv
from pathlib import Path
from datetime import datetime
import yaml, copy
import numpy as np
import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed

BACKTESTER = Path("backtester_core_speed3_veto_universe_2.py")
INIT_CFG = None
GLOBAL_BEST_S = -1e18
GLOBAL_BEST_REC = None
BT_SLEEP_SEC = 0
BT_CACHE = {}


KV_RE = re.compile(
    r'(?:\x1b\[[0-9;]*m)?(equity_end|pf|profit_factor|max_dd|mono|monotonicity|trades|apr|daily_ret|monthly_ret|yearly_ret)\s*=\s*([-+]?[0-9]*\.?[0-9]+)',
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
        ):
            out[k] = float(v)
        elif k == 'trades':
            try:
                out[k] = int(float(v))
            except Exception:
                out[k] = int(v)
    return out if out else None

def run_backtest(cfg_path: Path, limit_bars: int, with_plots: bool = False, label: str = None):
    yaml_text = Path(cfg_path).read_text()
    key = (limit_bars, yaml_text)
    if not with_plots and key in BT_CACHE:
        return BT_CACHE[key]
    cmd = [
        sys.executable,
        str(BACKTESTER),
        "--cfg",
        str(cfg_path),
        "--limit-bars",
        str(limit_bars),
        "--export-csv",
    ]
    if with_plots:
        from uuid import uuid4
        import time, os
        ts = time.strftime("%Y%m%d_%H%M%S")
        tag = (label or "final") + "_" + uuid4().hex[:6]
        plots_dir = f"_reports/_bt_plots/{tag}_{ts}"
        os.makedirs(plots_dir, exist_ok=True)
        cmd += ["--plots", plots_dir]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    stats = parse_metrics(out) or {}
    if not stats:
        raise RuntimeError(f"Could not parse metrics from backtester output. Tail: {out[-800:]}")
    # extract trades csv path if present
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
    cfg_yaml_path, limit_bars = args_tuple
    return run_backtest(cfg_yaml_path, limit_bars, with_plots=False)


def compute_weights(times: pd.Series, now: pd.Timestamp,
                    decay="exp", hl_hours=48.0, mix_alpha=0.7, mix_hl2=168.0):
    age_h = (now - times).dt.total_seconds() / 3600.0
    ln2 = np.log(2.0)
    if decay == "none":
        w = np.ones_like(age_h, dtype=float)
    elif decay == "exp":
        w = np.exp(-ln2 * age_h / hl_hours)
    else:
        w1 = np.exp(-ln2 * age_h / hl_hours)
        w2 = np.exp(-ln2 * age_h / mix_hl2)
        w = mix_alpha * w1 + (1.0 - mix_alpha) * w2
    s = w.sum()
    return w if s == 0 else (w / s)


def weighted_expected_return(trades_df, args):
    if trades_df.empty:
        return -1e9, 0.0
    t = pd.to_datetime(trades_df["exit_time"], utc=True, errors="coerce")
    now = t.max()
    w = compute_weights(t, now, args.decay, args.hl_hours, args.mix_alpha, args.mix_hl2_hours)
    r = pd.to_numeric(trades_df.get("net_return"), errors="coerce").fillna(0.0).to_numpy()
    Ew = float((w * r).sum())
    if args.min_trades_recent > 0 and args.recent_hours > 0:
        recent_mask = (now - t).dt.total_seconds() / 3600.0 <= args.recent_hours
        trades_w_recent = float(w[recent_mask].sum())
    else:
        trades_w_recent = float(w.sum())
    return Ew, trades_w_recent



# ANSI coloring helper: wrap string in green only if cond is True
def _green_if(text: str, cond: bool) -> str:
    try:
        return ("\x1b[32m" + text + "\x1b[0m") if cond else text
    except Exception:
        return text
def read_yaml(p: Path): return yaml.safe_load(p.read_text())
def write_yaml(obj, p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(obj, sort_keys=False), encoding="utf-8")

def deep_get(d, key):
    cur = d
    for k in key.split("."):
        if not isinstance(cur, dict) or k not in cur: return None
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
    from pathlib import Path
    import shutil
    root = Path("_reports/_backtest")
    pats = sorted([p for p in root.glob(f"backtest{prefix}*") if p.is_dir()],
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for p in pats[keep:]:
        shutil.rmtree(p, ignore_errors=True)

ALIASES = {
    "tp": ["strategy_params.tp_atr_mult"],
    "sl": ["strategy_params.sl_atr_mult"],
    "min-mom": ["strategy_params.min_momentum_sum"],
    "min-atr": ["strategy_params.min_atr_ratio"],
    "top-n": ["top-n"],
    "side": ["side"],
    "strategy_params.min_vol_surge_mult": ["strategy_params.min_vol_surge_mult"],
    "strategy_params.min_qv_24h": ["strategy_params.min_qv_24h"],
    "strategy_params.min_qv_1h": ["strategy_params.min_qv_1h"],
    "open_on_heat": ["open_on_heat"],
    "open_heat_min": ["open_heat_min"],
    # exit-related aliases
    "max-bars": ["strategy_params.max_bars_in_position"],
    "exit-macd": ["strategy_params.exit_on_macd_flip"],
    "adx-exit": ["strategy_params.adx_exit_threshold"],
    "rsi-exit-long": ["strategy_params.rsi_exit_long"],
    "rsi-exit-short": ["strategy_params.rsi_exit_short"],
    "heat-exit": ["strategy_params.heat_exit_threshold"],
}

def get_current(cfg, pname):
    for key in ALIASES.get(pname, [pname]):
        v = deep_get(cfg, key)
        if v is not None: return v, key
    return None, None

def set_param(cfg, pname, value):
    for key in ALIASES.get(pname, [pname]):
        try:
            deep_set(cfg, key, value); return key
        except Exception: continue
    return None

# --------- search ---------

def ensure_included(values, cur):
    try: curf = float(cur)
    except: return list(values)
    out, seen = [], False
    for v in values:
        try: vf = float(v)
        except: continue
        if abs(vf - curf) < 1e-12: seen = True
        out.append(vf)
    if not seen: out.append(curf)
    return sorted(set(out))

def around(val, step, n=1):
    xs = [val]
    for k in range(1, n+1):
        xs += [round(val - k*step, 10), round(val + k*step, 10)]
    return sorted(set([x for x in xs if isinstance(x,(int,float)) and x>0]))

def realize_around(spec, current):
    if isinstance(spec, str) and spec.startswith("around:"):
        step = float(spec.split(":")[1])
        try: c = float(current)
        except: return [current]
        return around(c, step, n=1)
    return spec


def include_seed_values(values, pname, current_value):
    # include initial YAML seed (if present) and current stage value; de-duplicate
    try:
        init_val, _ = get_current(INIT_CFG, pname) if INIT_CFG is not None else (None, None)
    except Exception:
        init_val = None
    vals = list(values) if isinstance(values,(list,tuple,set)) else ([values] if values is not None else [])
    if current_value is not None and current_value not in vals:
        vals.append(current_value)
    if init_val is not None and init_val not in vals:
        vals.append(init_val)
    # try numeric sort, fallback to str
    try:
        vals = sorted(set(float(x) for x in vals))
    except Exception:
        try:
            vals = sorted(set(vals))
        except Exception:
            vals = list(dict.fromkeys(vals))
    return list(vals)


def do_rays(base_cfg, limit_bars, pname, cand, prefix, log_csv, args):
    cur, _ = get_current(base_cfg, pname)
    cand = ensure_included(cand, cur) if isinstance(cand, (list, tuple)) else ([cur] if cur is not None else [])
    cand = include_seed_values(cand, pname, cur)

    recs = []
    local_best_Ew = -1e18
    local_best_cfg = None
    local_best_rec = None
    jobs = []

    for v in cand:
        cfg = copy.deepcopy(base_cfg)
        set_param(cfg, pname, v)
        tmp = Path("tune_tmp") / f"{prefix}_{pname}_{str(v).replace('.','p')}.yaml"
        write_yaml(cfg, tmp)
        jobs.append((tmp, v, cfg))

    def handle_result(res, v, tmp, cfg):
        nonlocal local_best_Ew, local_best_cfg, local_best_rec
        T = int(res.get("trades", 0))
        Ew, w_tr = -1e9, 0.0
        trades_len = T
        if args.min_trades == 0 or T >= args.min_trades:
            if res.get("trades_csv"):
                trades_df = pd.read_csv(
                    res["trades_csv"],
                    usecols=["exit_time", "net_return"],
                    dtype={"net_return": "float32"},
                    engine="c",
                    low_memory=False,
                )
                trades_len = len(trades_df)
                Ew, w_tr = weighted_expected_return(trades_df, args)
        res.update({
            "param": pname,
            "value": v,
            "cfg_path": str(tmp),
            "yaml": str(tmp),
            "ts": datetime.utcnow().isoformat(timespec="seconds"),
            "Ew": Ew,
            "trades_w_recent": w_tr,
        })
        recs.append(res)
        ok_trades = (args.min_trades == 0 or T >= args.min_trades)
        ok_recent = (args.min_trades_recent == 0 or w_tr >= args.min_trades_recent / max(1.0, trades_len))
        if ok_trades and ok_recent and (Ew > local_best_Ew + 1e-12):
            local_best_Ew = Ew
            local_best_cfg = copy.deepcopy(cfg)
            local_best_rec = res

    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            fut_map = {ex.submit(_eval_one, (tmp, limit_bars)): (tmp, v, cfg) for tmp, v, cfg in jobs}
            for fut in as_completed(fut_map):
                tmp, v, cfg = fut_map[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"equity_end":100.0,"profit_factor":0.0,"max_dd":0.0,"monotonicity":0.0,"trades":0,"error":str(e)}
                handle_result(res, v, tmp, cfg)
    else:
        for tmp, v, cfg in jobs:
            try:
                res = run_backtest(tmp, limit_bars, with_plots=False)
            except Exception as e:
                res = {"equity_end":100.0,"profit_factor":0.0,"max_dd":0.0,"monotonicity":0.0,"trades":0,"error":str(e)}
            handle_result(res, v, tmp, cfg)

    if local_best_cfg is not None:
        base_cfg = local_best_cfg
        best = local_best_rec
    else:
        best = {"value": cur, "Ew": local_best_Ew}

    best_yaml = Path(f"{prefix}_{pname}_best.yaml")
    write_yaml(base_cfg, best_yaml)
    with open(log_csv, "a", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["ts","param","value","equity_end","profit_factor","max_dd","monotonicity","trades","elapsed_sec","yaml","Ew","trades_w_recent"], extrasaction="ignore")
        if f.tell() == 0:
            wr.writeheader()
        for r in recs:
            wr.writerow(r)
    print(f"[rays] BEST {pname}={best.get('value')} Ew={best.get('Ew')} (E={best.get('equity_end')} PF={best.get('profit_factor')} DD={best.get('max_dd')} T={best.get('trades')})")
    return base_cfg, recs


def do_grid(base_cfg, limit_bars, params, prefix, log_csv, args):
    cand_lists = {}
    for p, spec in params.items():
        cur, _ = get_current(base_cfg, p)
        vals = realize_around(spec, cur)
        if isinstance(vals, (list, tuple)):
            vals = ensure_included(vals, cur)
        else:
            vals = [cur] if cur is not None else []
        vals = include_seed_values(vals, p, cur)
        cand_lists[p] = vals

    keys = list(cand_lists.keys())
    grid = list(itertools.product(*[cand_lists[k] for k in keys]))
    recs = []
    local_best_Ew = -1e18
    local_best_cfg = None
    local_best_rec = None
    jobs = []

    for vec in grid:
        cfg = copy.deepcopy(base_cfg)
        for k, v in zip(keys, vec):
            set_param(cfg, k, v)
        tmp = Path("tune_tmp") / f"{prefix}_grid_{'_'.join(str(x).replace('.', 'p') for x in vec)}.yaml"
        write_yaml(cfg, tmp)
        jobs.append((tmp, vec, cfg))

    def handle_result(res, vec, tmp, cfg):
        nonlocal local_best_Ew, local_best_cfg, local_best_rec
        T = int(res.get("trades", 0))
        Ew, w_tr = -1e9, 0.0
        trades_len = T
        if args.min_trades == 0 or T >= args.min_trades:
            if res.get("trades_csv"):
                trades_df = pd.read_csv(
                    res["trades_csv"],
                    usecols=["exit_time", "net_return"],
                    dtype={"net_return": "float32"},
                    engine="c",
                    low_memory=False,
                )
                trades_len = len(trades_df)
                Ew, w_tr = weighted_expected_return(trades_df, args)
        res.update({
            "param": "|".join(keys),
            "value": "|".join(map(str, vec)),
            "cfg_path": str(tmp),
            "yaml": str(tmp),
            "ts": datetime.utcnow().isoformat(timespec="seconds"),
            "Ew": Ew,
            "trades_w_recent": w_tr,
        })
        recs.append(res)
        ok_trades = (args.min_trades == 0 or T >= args.min_trades)
        ok_recent = (args.min_trades_recent == 0 or w_tr >= args.min_trades_recent / max(1.0, trades_len))
        if ok_trades and ok_recent and (Ew > local_best_Ew + 1e-12):
            local_best_Ew = Ew
            local_best_cfg = copy.deepcopy(cfg)
            local_best_rec = res

    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            fut_map = {ex.submit(_eval_one, (tmp, limit_bars)): (tmp, vec, cfg) for tmp, vec, cfg in jobs}
            for fut in as_completed(fut_map):
                tmp, vec, cfg = fut_map[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"equity_end":100.0,"profit_factor":0.0,"max_dd":0.0,"monotonicity":0.0,"trades":0,"error":str(e)}
                handle_result(res, vec, tmp, cfg)
    else:
        for tmp, vec, cfg in jobs:
            try:
                res = run_backtest(tmp, limit_bars, with_plots=False)
            except Exception as e:
                res = {"equity_end":100.0,"profit_factor":0.0,"max_dd":0.0,"monotonicity":0.0,"trades":0,"error":str(e)}
            handle_result(res, vec, tmp, cfg)

    if local_best_cfg is not None:
        base_cfg = local_best_cfg
        chosen = local_best_rec
    else:
        chosen = {"value": "|".join(str(get_current(base_cfg, k)[0]) for k in keys), "Ew": local_best_Ew}

    for k, v in zip(keys, chosen.get("value", "").split("|")):
        if v == "":
            continue
        try:
            v2 = float(v)
            v2 = int(v2) if v2.is_integer() else v2
        except Exception:
            v2 = v
        set_param(base_cfg, k, v2)

    write_yaml(base_cfg, Path(f"{prefix}_grid_best.yaml"))
    with open(log_csv, "a", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["ts","param","value","equity_end","profit_factor","max_dd","monotonicity","trades","elapsed_sec","yaml","Ew","trades_w_recent"], extrasaction="ignore")
        if f.tell() == 0:
            wr.writeheader()
        for r in recs:
            wr.writerow(r)

    line = f"[grid] BEST {chosen.get('value')} Ew={chosen.get('Ew')} (E={chosen.get('equity_end')} PF={chosen.get('profit_factor')} DD={chosen.get('max_dd')} T={chosen.get('trades')})"
    print(line)
    return base_cfg, recs


def default_plan(limit_bars: int = None):
    long_window = (limit_bars or 1440) >= 2000
    if long_window:
        return [
            ("rays", {"side": ["LONG","BOTH","SHORT"]}),
            ("rays", {"top-n": [6,8,10,12]}),
            ("rays", {"min-mom": [0.03,0.05,0.07,0.09]}),
            ("rays", {"min-atr": [0.008,0.012,0.016,0.020]}),
            ("rays", {"strategy_params.min_vol_surge_mult": [1.10,1.30,1.50]}),
            ("rays", {"strategy_params.min_qv_24h": [300000,500000,800000,1200000]}),
            ("rays", {"strategy_params.min_qv_1h":  [20000,40000,60000,80000]}),
            ("rays", {"open_on_heat": [False, True]}),
            ("rays", {"open_heat_min": [0.75,0.85,0.90,0.95]}),
            ("grid", {
                "min-mom": "around:0.01",
                "min-atr": "around:0.004",
                "strategy_params.min_vol_surge_mult": "around:0.10",
                "strategy_params.min_qv_24h": "around:200000",
                "strategy_params.min_qv_1h":  "around:10000",
                "top-n": "around:2",
                "tp": "around:0.2",
                "sl": "around:0.1",
                "open_heat_min": "around:0.05",
                "open_on_heat": [False, True],  # escape hatch
            }),
        ]
    # default (shorter windows)
    return [
        ("rays", {"tp": [3.25,3.5,3.75]}),
        ("rays", {"sl": [1.02,1.08,1.12]}),
        ("rays", {"min-mom": [0.02,0.022,0.024]}),
        ("rays", {"min-atr": [0.0,0.0008,0.0012]}),
        ("rays", {"side": ["LONG","BOTH"]}),
        ("rays", {"top-n": [10,12,14]}),
        ("rays", {"open_on_heat": [False, True]}),
        ("rays", {"open_heat_min": [0.80,0.90,0.95]}),
        ("grid", {
            "tp": "around:0.02", "sl": "around:0.02",
            "min-mom": "around:0.001", "min-atr": "around:0.0002",
            "top-n": "around:2", "open_heat_min": "around:0.05",
            "open_on_heat": [False, True],
        }),
    ]

def main():

    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--limit-bars", type=int, default=2880)
    ap.add_argument("--prefix", default="t5m2880_exp")
    ap.add_argument("--min-trades", type=int, default=50)
    ap.add_argument("--plan", help="Path to external plan module (default_plan used if omitted)")
    ap.add_argument("--sleep-sec", type=float, default=0.0, help="pause between backtests in seconds")
    ap.add_argument("--jobs", type=int, default=1, help="number of parallel backtest jobs")
    ap.add_argument("--decay", choices=["none", "exp", "mix"], default="exp")
    ap.add_argument("--hl-hours", type=float, default=48.0)
    ap.add_argument("--mix-alpha", type=float, default=0.7)
    ap.add_argument("--mix-hl2-hours", type=float, default=168.0)
    ap.add_argument("--min-trades-recent", type=int, default=0)
    ap.add_argument("--recent-hours", type=float, default=72.0)
    args = ap.parse_args()

    global INIT_CFG
    INIT_CFG = read_yaml(Path(args.cfg))
    global BT_SLEEP_SEC
    BT_SLEEP_SEC = args.sleep_sec
    


    base = read_yaml(Path(args.cfg))
    rays_results = []
    grid_results = []
    # ---- Baseline (original cfg) ----
    baseline_yaml = Path(f"{args.prefix}_baseline.yaml")
    write_yaml(base, baseline_yaml)
    base_res = run_backtest(baseline_yaml, args.limit_bars)
    T = int(base_res.get("trades", 0))
    Ew0 = -1e9
    if args.min_trades == 0 or T >= args.min_trades:
        if base_res.get("trades_csv"):
            trades_df = pd.read_csv(
                base_res["trades_csv"],
                usecols=["exit_time", "net_return"],
                dtype={"net_return": "float32"},
                engine="c",
                low_memory=False,
            )
            Ew0, _ = weighted_expected_return(trades_df, args)
    print(f"[baseline] Ew={Ew0:.6f} trades={base_res.get('trades')}")
    log_csv = Path(f"{args.prefix}_tuner_log.csv")

    # Load plan: external module if provided, else fallback to internal default_plan
    plan = None
    if getattr(args, "plan", None):
        import importlib.util
        from pathlib import Path as _Path
        _pp = _Path(args.plan)
        if not _pp.exists():
            raise FileNotFoundError(f"--plan file not found: {_pp}")
        spec = importlib.util.spec_from_file_location("user_tuner_plan", str(_pp))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot import plan module from {_pp}")
        _mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_mod)
        if not hasattr(_mod, "default_plan"):
            raise AttributeError(f"Plan module {_pp} has no default_plan(limit_bars)")
        plan = _mod.default_plan(args.limit_bars)
    else:
        plan = default_plan(args.limit_bars)


    cur_yaml = Path(args.cfg)
    for i, (mode, params) in enumerate(plan, 1):
        prefix = f"{args.prefix}_s{i}_{mode}"
        if mode == "rays":
            (pname, cand) = list(params.items())[0]
            base, rays_results = do_rays(base, args.limit_bars, pname, cand, prefix, log_csv, args)
        elif mode == "grid":
            base, grid_results = do_grid(base, args.limit_bars, params, prefix, log_csv, args)
        else:
            raise ValueError(mode)

    final = Path(f"{args.prefix}_final_best.yaml")
    write_yaml(base, final)
    print(f"DONE -> {final}")

    TOPK_RAYS_PLOTS = 3
    if rays_results:
        best = sorted(rays_results, key=lambda r: r.get("Ew", -1e18), reverse=True)[:TOPK_RAYS_PLOTS]
        for i, r in enumerate(best, 1):
            run_backtest(Path(r["cfg_path"]), args.limit_bars, with_plots=True, label=f"{args.prefix}_rays_final_{i}")
    if grid_results:
        best_grid = max(grid_results, key=lambda r: r.get("Ew", -1e18))
        run_backtest(Path(best_grid["cfg_path"]), args.limit_bars, with_plots=True, label=f"{args.prefix}_grid_final")
    prune_reports(args.prefix)
    tmp_dir = Path("tune_tmp")
    for p in tmp_dir.glob("*.yaml"):
        p.unlink(missing_ok=True)



if __name__ == "__main__":
    main()
