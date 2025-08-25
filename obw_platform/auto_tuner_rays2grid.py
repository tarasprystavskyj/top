
#!/usr/bin/env python3
"""
auto_tuner_rays2grid_v2.py
- Fix: robust metric parsing (any order) to match backtester prints like:
  'equity_end=... trades=... pf=... max_dd=... mono=... elapsed_sec=...'
- Sanity-run of base cfg before tuning.
- Rays -> small Grid auto-tuning, incremental CSV logging, tmp YAMLs.
"""

import argparse, itertools, re, subprocess, sys, time, csv
from pathlib import Path
from datetime import datetime
import yaml
import copy

BACKTESTER = Path("backtester_core_speed3_veto.py")

# ---------- utils ----------

def read_yaml(path: Path):
    with open(path, "r") as f:
        return yaml.safe_load(f)

def write_yaml(obj, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(obj, f, sort_keys=False)

def deep_get(d, key):
    if "." in key:
        cur = d
        for k in key.split("."):
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                return None
        return cur
    return d.get(key, None)

def deep_set(d, key, value):
    if "." in key:
        parts = key.split(".")
        cur = d
        for k in parts[:-1]:
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
        cur[parts[-1]] = value
    else:
        d[key] = value

def best_of(records):
    def key(r):
        return (
            float(r.get("equity_end", float("-inf"))),
            float(r.get("profit_factor", float("-inf"))),
            -float(r.get("max_dd", float("inf"))),
            float(r.get("monotonicity", float("-inf"))),
        )
    return max(records, key=key)

KV_RE = re.compile(r'\b(equity_end|pf|profit_factor|max_dd|mono|monotonicity|trades)\s*=\s*([-\d\.]+)')

def parse_metrics(text: str):
    """
    Parse in any order. Accept both 'pf' and 'profit_factor', 'mono' or 'monotonicity'.
    """
    out = {}
    for k, v in KV_RE.findall(text):
        if k == 'pf':
            k = 'profit_factor'
        if k == 'mono':
            k = 'monotonicity'
        if k in ('equity_end','profit_factor','max_dd','monotonicity'):
            out[k] = float(v)
        elif k == 'trades':
            try:
                out[k] = int(float(v))
            except:
                out[k] = int(v)
    if set(('equity_end','profit_factor','max_dd','monotonicity','trades')).issubset(out.keys()):
        return out
    # fallback to old strict pattern (unlikely needed now)
    return None

def run_backtest(cfg_path: Path, limit_bars: int, plots_dir: str = ""):
    cmd = [sys.executable, str(BACKTESTER), "--cfg", str(cfg_path), "--limit-bars", str(limit_bars)]
    if plots_dir:
        cmd += ["--plots", plots_dir]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    stats = parse_metrics(out)
    if not stats:
        raise RuntimeError(
            "Could not parse metrics from backtester output.\n"
            f"Command: {' '.join(cmd)}\n"
            f"YAML: {cfg_path}\n"
            f"--- stdout/stderr (last 1200 chars) ---\n{out[-1200:]}"
        )
    stats["elapsed_sec"] = elapsed
    stats["stdout_tail"] = out[-500:]
    return stats

def append_csv(path: Path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = (not path.exists()) or path.stat().st_size == 0
    with open(path, "a", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header: wr.writeheader()
        for r in rows: wr.writerow(r)

def ensure_included(values, cur):
    try:
        curf = float(cur)
    except Exception:
        return list(values)
    nums, seen = [], False
    for v in values:
        try:
            vf = float(v)
        except Exception:
            continue
        if abs(vf - curf) < 1e-12:
            seen = True
        nums.append(vf)
    if not seen:
        nums.append(curf)
    return sorted(set(nums))

ALIASES = {
    "tp": ["strategy_params.tp_atr_mult", "tp_atr_mult", "tp"],
    "sl": ["strategy_params.sl_atr_mult", "sl_atr_mult", "sl"],
    "min-mom": ["min_momentum_sum", "min-mom"],
    "min-atr": ["min_atr_ratio", "min-atr"],
    "position_notional": ["portfolio.position_notional", "position_notional"],
    "top-n": ["top-n", "top_n", "topn"],
    "side": ["side"],
}

def get_current(cfg, pname):
    for key in ALIASES.get(pname, [pname]):
        val = deep_get(cfg, key)
        if val is not None:
            return val, key
    return None, None

def set_param(cfg, pname, value):
    last_key = None
    for key in ALIASES.get(pname, [pname]):
        last_key = key
        try:
            if value is None: continue
            if isinstance(value, str):
                if value.upper() in ("LONG","SHORT","BOTH"):
                    deep_set(cfg, key, value.upper()); return key
                try:
                    v = float(value) if "." in value else int(value)
                except Exception:
                    v = value
                deep_set(cfg, key, v); return key
            else:
                deep_set(cfg, key, value); return key
        except Exception:
            continue
    if last_key:
        deep_set(cfg, last_key, value)
    return last_key

def frange(start, stop, step):
    vals, x = [], start
    for _ in range(10000):
        if x > stop + 1e-12: break
        vals.append(round(x, 10))
        x += step
    return vals

def around(val, step, n=1):
    xs = [val]
    for k in range(1, n+1):
        xs.extend([round(val - k*step, 10), round(val + k*step, 10)])
    xs = sorted(set([x for x in xs if (isinstance(x, (int,float)) and x > 0)]))
    return xs

def default_plan():
    return [
        ("rays", {"tp": frange(3.25, 3.75, 0.05)}),
        ("rays", {"sl": frange(1.02, 1.12, 0.02)}),
        ("rays", {"min-mom": frange(0.019, 0.024, 0.001)}),
        ("rays", {"min-atr": [0, 0.0005, 0.0008, 0.0010, 0.0012]}),
        ("rays", {"side": ["LONG", "BOTH"]}),
        ("rays", {"top-n": [10, 12, 14, 16]}),
        ("rays", {"position_notional": frange(70, 130, 10)}),
        ("grid", {"tp": "around:0.02", "sl": "around:0.02", "min-mom": "around:0.001", "min-atr": "around:0.0002", "position_notional": "around:10", "top-n": "around:2"}),
    ]

def do_rays_step(cur_yaml: Path, base_cfg: dict, limit_bars: int, param: str, candidates, prefix: str, plots: str, log_csv: Path):
    cur, _ = get_current(base_cfg, param)
    cand = ensure_included(candidates, cur) if isinstance(candidates, (list, tuple)) else ([cur] if cur is not None else [])

    recs = []
    for v in cand:
        test_cfg = copy.deepcopy(base_cfg)
        set_param(test_cfg, param, v)
        tmp = Path("tune_tmp") / f"{prefix}_{param}_{str(v).replace('.','p')}.yaml"
        write_yaml(test_cfg, tmp)
        try:
            res = run_backtest(tmp, limit_bars, plots_dir="")
        except Exception as e:
            res = {"equity_end": float("-inf"), "profit_factor": 0.0, "max_dd": 1.0, "monotonicity": -1.0, "trades": 0, "error": str(e)}
        res.update({"param": param, "value": v, "yaml": str(tmp), "ts": datetime.utcnow().isoformat(timespec="seconds")})
        recs.append(res)
        append_csv(log_csv, [res], ["ts","param","value","equity_end","profit_factor","max_dd","monotonicity","trades","elapsed_sec","yaml"])
        print(f"[rays] {param}={v} -> equity_end={res.get('equity_end')} pf={res.get('profit_factor')} max_dd={res.get('max_dd')} mono={res.get('monotonicity')} trades={res.get('trades')}")

    good = [r for r in recs if r.get("equity_end", float("-inf")) != float("-inf")]
    if not good:
        last_err = recs[-1].get("error", "no error captured")
        raise RuntimeError(f"All candidates failed for param={param}. Last error:\n{last_err}")

    best = best_of(good)
    set_param(base_cfg, param, best["value"])
    best_yaml = Path(f"{prefix}_{param}_best.yaml")
    write_yaml(base_cfg, best_yaml)
    print(f"[rays] best {param}={best['value']} => equity_end={best['equity_end']} pf={best['profit_factor']} max_dd={best['max_dd']} mono={best['monotonicity']} -> saved {best_yaml}")
    return base_cfg, best_yaml

def realize_around(spec, current):
    if isinstance(spec, str) and spec.startswith("around:"):
        step = float(spec.split(":")[1])
        try:
            c = float(current)
        except Exception:
            return [current]
        return around(c, step, n=1)
    return spec

def do_grid_step(cur_yaml: Path, base_cfg: dict, limit_bars: int, params: dict, prefix: str, plots: str, log_csv: Path):
    cand_lists = {}
    for p, spec in params.items():
        cur, _ = get_current(base_cfg, p)
        vals = realize_around(spec, cur)
        if isinstance(vals, (list, tuple)):
            vals = ensure_included(vals, cur)
        else:
            vals = [cur] if cur is not None else []
        cand_lists[p] = vals

    keys = list(cand_lists.keys())
    grid = list(itertools.product(*[cand_lists[k] for k in keys]))
    print(f"[grid] combos={len(grid)} over {keys}")

    recs = []
    for vec in grid:
        test_cfg = copy.deepcopy(base_cfg)
        name = []
        for k, v in zip(keys, vec):
            set_param(test_cfg, k, v); name.append(f"{k}={v}")
        tmp = Path("tune_tmp") / f"{prefix}_grid_{'_'.join(str(x).replace('.','p') for x in vec)}.yaml"
        write_yaml(test_cfg, tmp)
        try:
            res = run_backtest(tmp, limit_bars, plots_dir="")
        except Exception as e:
            res = {"equity_end": float("-inf"), "profit_factor": 0.0, "max_dd": 1.0, "monotonicity": -1.0, "trades": 0, "error": str(e)}
        res.update({"param":"|".join(keys), "value":"|".join(map(str,vec)), "yaml": str(tmp), "ts": datetime.utcnow().isoformat(timespec="seconds")})
        recs.append(res)
        append_csv(log_csv, [res], ["ts","param","value","equity_end","profit_factor","max_dd","monotonicity","trades","elapsed_sec","yaml"])
        print(f"[grid] {' '.join(name)} -> equity_end={res.get('equity_end')} pf={res.get('profit_factor')} max_dd={res.get('max_dd')} mono={res.get('monotonicity')} trades={res.get('trades')}")

    good = [r for r in recs if r.get("equity_end", float("-inf")) != float("-inf")]
    if not good:
        last_err = recs[-1].get("error", "no error captured")
        raise RuntimeError(f"All grid combos failed. Last error:\n{last_err}")

    best = best_of(good)
    vec = best["value"].split("|")
    for k, v in zip(keys, vec):
        try:
            v2 = float(v)
            if v2.is_integer(): v2 = int(v2)
        except Exception:
            v2 = v
        set_param(base_cfg, k, v2)
    best_yaml = Path(f"{prefix}_grid_best.yaml")
    write_yaml(base_cfg, best_yaml)
    print(f"[grid] best {' '.join(f'{k}={v}' for k,v in zip(keys,vec))} => equity_end={best['equity_end']} pf={best['profit_factor']} max_dd={best['max_dd']} mono={best['monotonicity']} -> saved {best_yaml}")
    return base_cfg, best_yaml

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True, help="Base YAML config")
    ap.add_argument("--limit-bars", type=int, default=1440)
    ap.add_argument("--prefix", default="auto")
    ap.add_argument("--plots", default="", help="Optional: render plots for final best")
    args = ap.parse_args()

    Path("tune_tmp").mkdir(exist_ok=True)
    log_csv = Path(f"{args.prefix}_tuner_log.csv")

    # Sanity-run base YAML
    print(">>> Sanity run base config ...")
    try:
        _ = run_backtest(Path(args.cfg), args.limit_bars, plots_dir="")
        print("OK: base config produced metrics.")
    except Exception as e:
        print(str(e)); sys.exit(1)

    base_cfg = read_yaml(Path(args.cfg))
    plan = default_plan()
    cur_yaml = Path(args.cfg)

    for i, (mode, params) in enumerate(plan, 1):
        step_prefix = f"{args.prefix}_s{i}_{mode}"
        print(f"\n=== Step {i}/{len(plan)} :: {mode} ===")
        if mode == "rays":
            (pname, cand) = list(params.items())[0]
            base_cfg, cur_yaml = do_rays_step(cur_yaml, base_cfg, args.limit_bars, pname, cand, step_prefix, args.plots, log_csv)
        elif mode == "grid":
            base_cfg, cur_yaml = do_grid_step(cur_yaml, base_cfg, args.limit_bars, params, step_prefix, args.plots, log_csv)
        else:
            raise ValueError(f"Unknown mode {mode}")

    final_yaml = Path(f"{args.prefix}_final_best.yaml")
    write_yaml(base_cfg, final_yaml)
    print(f"\n=== DONE ===\nFinal best saved: {final_yaml}")

    if args.plots:
        try:
            cmd = [sys.executable, str(BACKTESTER), "--cfg", str(final_yaml), "--limit-bars", str(args.limit_bars), "--plots", args.plots]
            subprocess.run(cmd, check=False)
            print(f"[plots] rendered into: {args.plots}")
        except Exception as e:
            print(f"[plots] failed: {e}")

if __name__ == "__main__":
    main()
