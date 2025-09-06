#!/usr/bin/env python3
# auto_tuner_rays2grid_v3_fix.py
# - Fix scoring: strong penalty for zero/low trades, use equity return (not absolute).
# - Escape hatch in grid: include open_on_heat toggle; if a candidate yields 0 trades, mark it FAIL.
# - CLI adds --min-trades and --target-trades.

import argparse, itertools, re, subprocess, sys, time, csv
from pathlib import Path
from datetime import datetime
import yaml, copy

BACKTESTER = Path("backtester_core_speed3_veto_universe_2.py")
INIT_CFG = None
GLOBAL_BEST_S = -1e18
GLOBAL_BEST_REC = None
BT_SLEEP_SEC = 0


KV_RE = re.compile(r'(?:\x1b\[[0-9;]*m)?(equity_end|pf|profit_factor|max_dd|mono|monotonicity|trades)\s*=\s*([-+]?[0-9]*\.?[0-9]+)', re.IGNORECASE)

def parse_metrics(text: str):
    out = {}
    for k, v in KV_RE.findall(text):
        if k == 'pf': k = 'profit_factor'
        if k == 'mono': k = 'monotonicity'
        if k in ('equity_end','profit_factor','max_dd','monotonicity'):
            out[k] = float(v)
        elif k == 'trades':
            try: out[k] = int(float(v))
            except: out[k] = int(v)
    return out if out else None

def run_backtest(cfg_path: Path, limit_bars: int, plots_dir: str = ""):
    cmd = [sys.executable, str(BACKTESTER), "--cfg", str(cfg_path), "--limit-bars", str(limit_bars)]
    if plots_dir:
        cmd += ["--plots", plots_dir]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0
    out = (p.stdout or "") + "\\n" + (p.stderr or "")
    stats = parse_metrics(out)
    if not stats:
        raise RuntimeError(f"Could not parse metrics from backtester output. Tail: {out[-800:]}")
    stats["elapsed_sec"] = elapsed
    if BT_SLEEP_SEC > 0:
        time.sleep(BT_SLEEP_SEC)
    return stats



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

# --------- scoring (fixed) ---------

def risk_averse_score(r, w_equity=1.0, w_pf=10.0, w_dd=200.0, w_mono=5.0, dd_target=0.12,
                      min_trades=50, target_trades=300):
    eq = float(r.get("equity_end", 100.0))
    ret = eq - 100.0                    # use return, not absolute equity
    pf = float(r.get("profit_factor", 0.0))
    dd = float(r.get("max_dd", 1.0))
    mono = float(r.get("monotonicity", 0.0))
    trades = int(r.get("trades", 0))

    # Degenerate/no-trade detection
    if trades == 0 or (pf == 0.0 and dd == 0.0):
        return -1e9

    dd_penalty = max(0.0, dd - dd_target)
    base = (w_equity * ret) + (w_pf * (pf - 1.0)) - (w_dd * dd_penalty) + (w_mono * mono)

    # Hard penalty for very low trades; soft penalty until target_trades
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


def do_rays(base_cfg, limit_bars, pname, cand, prefix, log_csv, weights, min_trades, target_trades):
    global GLOBAL_BEST_S, GLOBAL_BEST_REC
    cur, _ = get_current(base_cfg, pname)
    cand = ensure_included(cand, cur) if isinstance(cand, (list,tuple)) else ([cur] if cur is not None else [])
    cand = include_seed_values(cand, pname, cur)

    recs = []

    for v in cand:
        cfg = copy.deepcopy(base_cfg)
        set_param(cfg, pname, v)
        tmp = Path("tune_tmp") / f"{prefix}_{pname}_{str(v).replace('.','p')}.yaml"
        write_yaml(cfg, tmp)
        try:
            res = run_backtest(tmp, limit_bars)
        except Exception as e:
            res = {"equity_end":100.0,"profit_factor":0.0,"max_dd":0.0,"monotonicity":0.0,"trades":0,"error":str(e)}
        res.update({"param":pname,"value":v,"yaml":str(tmp),"ts":datetime.utcnow().isoformat(timespec="seconds")})
        recs.append(res)

    best = pick_best(recs, weights, min_trades, target_trades)
    # regression guard: keep previous if no improvement
    cur_rec = next((r for r in recs if r.get('value') == cur), None)
    if cur_rec and best.get('score', -1e18) < cur_rec.get('score', -1e18):
        best = cur_rec
    set_param(base_cfg, pname, best["value"])
    write_yaml(base_cfg, Path(f"{prefix}_{pname}_best.yaml"))
    with open(log_csv, "a", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["ts","param","value","equity_end","profit_factor","max_dd","monotonicity","trades","elapsed_sec","yaml","score"], extrasaction="ignore")
        if f.tell()==0: wr.writeheader()
        for r in recs: wr.writerow(r)
    print(f"[rays] BEST {pname}={best['value']} score={best['score']} (E={best.get('equity_end')} PF={best.get('profit_factor')} DD={best.get('max_dd')} T={best.get('trades')})")
    return base_cfg


def do_grid(base_cfg, limit_bars, params, prefix, log_csv, weights, min_trades, target_trades):    # Expand search lists with current+seed inclusion
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
    import itertools, copy, csv
    from datetime import datetime

    grid = list(itertools.product(*[cand_lists[k] for k in keys]))
    recs = []
    for vec in grid:
        cfg = copy.deepcopy(base_cfg)
        name = []
        for k, v in zip(keys, vec):
            set_param(cfg, k, v)
            name.append(f"{k}={v}")
        tmp = Path("tune_tmp") / f"{prefix}_grid_{'_'.join(str(x).replace('.', 'p') for x in vec)}.yaml"
        write_yaml(cfg, tmp)
        try:
            res = run_backtest(tmp, limit_bars)
        except Exception as e:
            res = {"equity_end": 100.0, "profit_factor": 0.0, "max_dd": 0.0, "monotonicity": 0.0, "trades": 0, "error": str(e)}
        res.update({
            "param": "|".join(keys),
            "value": "|".join(map(str, vec)),
            "yaml": str(tmp),
            "ts": datetime.utcnow().isoformat(timespec="seconds")
        })
        recs.append(res)

    best = pick_best(recs, weights, min_trades, target_trades)
    # regression guard: global (baseline or prev best)
    cur_vec = [str(get_current(base_cfg, k)[0]) for k in keys]
    cur_name = "|".join(cur_vec)
    cur_rec = next((r for r in recs if r.get('value') == cur_name), None)
    chosen = best
    if GLOBAL_BEST_S is not None and best.get('score', -1e18) < GLOBAL_BEST_S:
        if cur_rec is not None and cur_rec.get('score', -1e18) >= GLOBAL_BEST_S:
            chosen = cur_rec
        else:
            chosen = cur_rec if cur_rec is not None else best

    for k, v in zip(keys, chosen["value"].split("|")):
        try:
            v2 = float(v)
            v2 = int(v2) if v2.is_integer() else v2
        except Exception:
            v2 = v
        set_param(base_cfg, k, v2)

    write_yaml(base_cfg, Path(f"{prefix}_grid_best.yaml"))
    with open(log_csv, "a", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["ts","param","value","equity_end","profit_factor","max_dd","monotonicity","trades","elapsed_sec","yaml","score"], extrasaction="ignore")
        if f.tell() == 0:
            wr.writeheader()
        for r in recs:
            wr.writerow(r)

    prev_best = GLOBAL_BEST_S
    # update global best if improved
    if chosen.get('score', -1e18) > GLOBAL_BEST_S:
        GLOBAL_BEST_S, GLOBAL_BEST_REC = chosen['score'], chosen

    line = f"[grid] BEST {chosen['value']} score={chosen.get('score')} (E={chosen.get('equity_end')} PF={chosen.get('profit_factor')} DD={chosen.get('max_dd')} T={chosen.get('trades')})"
    print(_green_if(line, (chosen.get('score', -1e18) > prev_best)))


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
    ap.add_argument("--prefix", default="t5m2880_fix")
    ap.add_argument("--w-equity", type=float, default=1.0)
    ap.add_argument("--w-pf", type=float, default=10.0)
    ap.add_argument("--w-dd", type=float, default=200.0)
    ap.add_argument("--w-mono", type=float, default=5.0)
    ap.add_argument("--dd-target", type=float, default=0.12)
    ap.add_argument("--min-trades", type=int, default=50)
    ap.add_argument("--target-trades", type=int, default=300)
    ap.add_argument("--plan", help="Path to external plan module (default_plan used if omitted)")
    ap.add_argument("--sleep-sec", type=float, default=60.0, help="pause between backtests in seconds")
    args = ap.parse_args()

    global INIT_CFG
    INIT_CFG = read_yaml(Path(args.cfg))
    weights = (args.w_equity, args.w_pf, args.w_dd, args.w_mono, args.dd_target)
    global BT_SLEEP_SEC
    BT_SLEEP_SEC = args.sleep_sec
    


    base = read_yaml(Path(args.cfg))
    # ---- Baseline (original cfg) ----
    baseline_yaml = Path(f"{args.prefix}_baseline.yaml")
    write_yaml(base, baseline_yaml)
    base_res = run_backtest(baseline_yaml, args.limit_bars)
    baseline = {
    "equity_end": float(base_res.get("equity_end", 100.0)),
    "profit_factor": float(base_res.get("profit_factor", 0.0)),
    "max_dd": float(base_res.get("max_dd", 0.0)),
    "monotonicity": float(base_res.get("monotonicity", 0.0)),
    "trades": int(base_res.get("trades", 0)),
    "elapsed_sec": float(base_res.get("elapsed_sec", 0.0)),
    "yaml": str(baseline_yaml),
    "param": "baseline",
    "value": "baseline"
    }
    base_scored = score_rec(baseline, weights, args.min_trades, args.target_trades)
    GLOBAL_BEST_S, GLOBAL_BEST_REC = base_scored["score"], base_scored
    print(f"[baseline] equity_end={baseline['equity_end']:.6f} trades={baseline['trades']} "
      f"pf={baseline['profit_factor']:.6f} max_dd={baseline['max_dd']:.6f} "
      f"mono={baseline['monotonicity']:.6f} score={GLOBAL_BEST_S:.6f}")
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


    # sanity run
    try: _ = run_backtest(Path(args.cfg), args.limit_bars)
    except Exception as e: print(e); sys.exit(1)

    cur_yaml = Path(args.cfg)
    for i, (mode, params) in enumerate(plan, 1):
        prefix = f"{args.prefix}_s{i}_{mode}"
        if mode == "rays":
            (pname, cand) = list(params.items())[0]
            base = do_rays(base, args.limit_bars, pname, cand, prefix, log_csv, weights, args.min_trades, args.target_trades)
        elif mode == "grid":
            base = do_grid(base, args.limit_bars, params, prefix, log_csv, weights, args.min_trades, args.target_trades)
        else:
            raise ValueError(mode)

    final = Path(f"{args.prefix}_final_best.yaml")
    write_yaml(base, final)
    print(f"DONE -> {final}")


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

if __name__ == "__main__":
    main()
