#!/usr/bin/env python3
# auto_tuner_rays2grid_v3_fix.py
# - Fix scoring: strong penalty for zero/low trades, use equity return (not absolute).
# - Escape hatch in grid: include open_on_heat toggle; if a candidate yields 0 trades, mark it FAIL.
# - CLI adds --min-trades and --target-trades.

import argparse, itertools, re, subprocess, sys, time, csv
from pathlib import Path
from datetime import datetime
import yaml, copy
from concurrent.futures import ProcessPoolExecutor, as_completed

BACKTESTER = Path("backtester_core_speed3_veto_universe_2.py")
INIT_CFG = None
GLOBAL_BEST_S = -1e18
GLOBAL_BEST_REC = None
BT_SLEEP_SEC = 0
BT_CACHE = {}
SESSION_DIR = None


DELTA_MODE_DEFAULT = True  # interpret grid lists as deltas around current by default
TUNE_ROOT = Path("_reports") / "tune"
TUNE_TMP_ROOT = TUNE_ROOT / "tmp"
PARAM_CLAMPS = {
    "strategy_params.tp_atr_mult": (0.1, 10.0),
    "strategy_params.sl_atr_mult": (0.01, 2.0),
    "strategy_params.min_atr_ratio": (0.0, 0.1),
    "strategy_params.min_momentum_sum": (0.0, 0.2),
    "strategy_params.heat_exit_threshold": (0.5, 1.5),
    "strategy_params.heat_exit_min_rr": (0.5, 3.0),
    "strategy_params.length": (3, 100),
    "strategy_params.volume_length": (4, 200),
    "strategy_params.macd_filter": (0, 1),
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
    import time, os
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
        ts = time.strftime("%Y%m%d_%H%M%S")
        base_label = label or "final"
        safe_label = (
            base_label.replace(os.sep, "_")
            .replace("/", "_")
            .replace("\\", "_")
        )
        tag = safe_label + "_" + uuid4().hex[:6]
        base_dir = (
            Path(SESSION_DIR) / "_reports" / "_bt_plots"
            if SESSION_DIR
            else Path("_reports") / "_bt_plots"
        )
        base_dir.mkdir(parents=True, exist_ok=True)
        plots_dir = base_dir / f"{tag}_{ts}"
        plots_dir.mkdir(parents=True, exist_ok=True)
        cmd += ["--plots", str(plots_dir)]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    stats = parse_metrics(out) or {}
    if not stats:
        raise RuntimeError(f"Could not parse metrics from backtester output. Tail: {out[-800:]}")
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
    start_ts = time.time()
    try:
        res = run_backtest(cfg_yaml_path, limit_bars, with_plots=False)
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
    # keep existing 'around:x' behavior
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


def _make_grid_candidates(pname, spec, current, delta_mode=DELTA_MODE_DEFAULT):
    vals = realize_around(spec, current)
    if isinstance(vals, (list, tuple, set)):
        vals = _as_list(vals)
        # If delta_mode: interpret as deltas around 'current'
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
        # always include current (seed) for safety
        try:
            curf = _clamp_param(pname, float(current))
            if curf not in out:
                out.append(curf)
        except Exception:
            if current not in out:
                out.append(current)
        # dedup & sort (numeric if possible)
        try:
            out = sorted(set(float(x) for x in out))
        except Exception:
            out = list(dict.fromkeys(out))
        return out
    else:
        # Single scalar spec: treat as absolute **if not delta_mode**, otherwise use [current]
        if delta_mode:
            # we consider single scalar as delta; add current+delta and current
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


def include_seed_values(values, pname, current_value):
    # include initial YAML seed (if present) and current stage value; de-duplicate
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
    # try numeric sort, fallback to str
    try:
        vals = sorted(set(float(x) for x in vals))
    except Exception:
        try:
            vals = sorted(set(vals))
        except Exception:
            vals = list(dict.fromkeys(vals))
    return list(vals)


def do_rays(base_cfg, limit_bars, pname, cand, prefix, session_dir, log_csv, weights, min_trades, target_trades, jobs):
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
        tasks.append((tmp, v, cfg))

    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            fut_map = {ex.submit(_eval_one, (tmp, limit_bars)): (tmp, v, cfg) for tmp, v, cfg in tasks}
            for fut in as_completed(fut_map):
                tmp, v, cfg = fut_map[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"equity_end":100.0,"profit_factor":0.0,"max_dd":0.0,"monotonicity":0.0,"trades":0,"error":str(e)}
                res.update({"param":pname,"value":v,"cfg_path":str(tmp),"yaml":str(tmp),"ts":datetime.utcnow().isoformat(timespec="seconds")})
                recs.append(res)
    else:
        for tmp, v, cfg in tasks:
            res = _eval_one((tmp, limit_bars))
            res.update({"param":pname,"value":v,"cfg_path":str(tmp),"yaml":str(tmp),"ts":datetime.utcnow().isoformat(timespec="seconds")})
            recs.append(res)

    best = pick_best(recs, weights, min_trades, target_trades)
    # regression guard: keep previous if no improvement
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
    print(f"[rays] BEST {pname}={best['value']} score={best['score']} (E={best.get('equity_end')} PF={best.get('profit_factor')} DD={best.get('max_dd')} T={best.get('trades')})")
    return base_cfg, recs


def do_grid(base_cfg, limit_bars, params, prefix, session_dir, log_csv, weights, min_trades, target_trades, jobs):    # Expand search lists with current+seed inclusion
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

    center_str = ", ".join(f"{k}={get_current(base_cfg, k)[0]}" for k in keys)
    print(f"[grid] center (from RAYS/baseline): {center_str}")
    for k in keys:
        print(f"[grid] {k} -> candidates: {cand_lists[k]}")

    import itertools, copy, csv
    from datetime import datetime

    grid = list(itertools.product(*[cand_lists[k] for k in keys]))
    if not grid:
        print("[grid] no candidate combinations; skipping")
        return base_cfg, []

    recs = []
    tasks = []
    local_best_s = -1e18

    def _fmt_val(val):
        if isinstance(val, float):
            return f"{val:.6f}"
        return str(val)

    def _print_new_best(vec, res, score):
        params_str = ", ".join(f"{k}={_fmt_val(v)}" for k, v in zip(keys, vec))
        metric_keys = [
            "equity_end",
            "profit_factor",
            "max_dd",
            "monotonicity",
            "trades",
            "apr",
            "daily_ret",
            "monthly_ret",
            "yearly_ret",
        ]
        metric_parts = []
        for mkey in metric_keys:
            val = res.get(mkey)
            if val is None:
                continue
            metric_parts.append(f"{mkey}={_fmt_val(val)}")
        if res.get("elapsed_sec") is not None:
            metric_parts.append(f"elapsed_sec={_fmt_val(res['elapsed_sec'])}")
        if res.get("trades_csv"):
            metric_parts.append(f"trades_csv={res['trades_csv']}")
        if res.get("yaml"):
            metric_parts.append(f"yaml={res['yaml']}")
        metrics_str = " ".join(metric_parts)
        print(f"[grid][new-best] params: {params_str}")
        print(f"[grid][new-best] report: score={score:.6f} {metrics_str}".rstrip())

    def _process_result(vec, res):
        nonlocal local_best_s
        try:
            if int(res.get("trades", 0)) == 0:
                print(f"[grid][skip] zero-trade candidate -> {vec}")
        except Exception:
            pass
        score = risk_averse_score(
            res,
            *weights,
            min_trades=min_trades,
            target_trades=target_trades,
        )
        res["score"] = score
        if score > local_best_s:
            local_best_s = score
            _print_new_best(vec, res, score)
        recs.append(res)
    first_key = keys[0]
    row_values = cand_lists[first_key]
    row_lookup = {val: idx for idx, val in enumerate(row_values)}
    row_count = len(row_values)
    combos_per_row = (len(grid) // row_count) if row_count else len(grid)
    if combos_per_row == 0:
        combos_per_row = 1
    row_counts = [0] * row_count
    row_done = [False] * row_count
    next_emit_row = 0
    progress_marks = []

    def _emit_progress_if_ready():
        nonlocal next_emit_row
        updated = False
        while next_emit_row < len(row_values) and row_done[next_emit_row]:
            pct = ((next_emit_row + 1) * combos_per_row) / len(grid) * 100.0
            progress_marks.append(f"{pct:.1f}%")
            next_emit_row += 1
            updated = True
        if updated:
            text = ", ".join(progress_marks)
            print(text)

    for vec in grid:
        cfg = copy.deepcopy(base_cfg)
        name = []
        for k, v in zip(keys, vec):
            set_param(cfg, k, v)
            name.append(f"{k}={v}")
        tmp = Path(session_dir) / "tune_tmp" / f"{prefix}_grid_{'_'.join(str(x).replace('.', 'p') for x in vec)}.yaml"
        write_yaml(cfg, tmp)
        tasks.append((tmp, vec, cfg))

    processed = 0
    progress_marks = []

    def _report_progress():
        if not grid:
            return
        pct = (processed / len(grid)) * 100.0
        progress_marks.append(f"{pct:.1f}%")
        print(f"[grid][progress] {', '.join(progress_marks)}")

    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            fut_map = {ex.submit(_eval_one, (tmp, limit_bars)): (tmp, vec, cfg) for tmp, vec, cfg in tasks}
            for fut in as_completed(fut_map):
                tmp, vec, cfg = fut_map[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"equity_end": 100.0, "profit_factor": 0.0, "max_dd": 0.0, "monotonicity": 0.0, "trades": 0, "error": str(e), "elapsed_sec": None}
                res.update({
                    "param": "|".join(keys),
                    "value": "|".join(map(str, vec)),
                    "cfg_path": str(tmp),
                    "yaml": str(tmp),
                    "ts": datetime.utcnow().isoformat(timespec="seconds"),
                })
                _process_result(vec, res)
                row_idx = row_lookup.get(vec[0])
                if row_idx is not None:
                    row_counts[row_idx] += 1
                    if row_counts[row_idx] >= combos_per_row:
                        row_done[row_idx] = True
                        _emit_progress_if_ready()
    else:
        for tmp, vec, cfg in tasks:
            res = _eval_one((tmp, limit_bars))
            res.update({
                "param": "|".join(keys),
                "value": "|".join(map(str, vec)),
                "cfg_path": str(tmp),
                "yaml": str(tmp),
                "ts": datetime.utcnow().isoformat(timespec="seconds"),
            })
            _process_result(vec, res)
            row_idx = row_lookup.get(vec[0])
            if row_idx is not None:
                row_counts[row_idx] += 1
                if row_counts[row_idx] >= combos_per_row:
                    row_done[row_idx] = True
                    _emit_progress_if_ready()

    best = pick_best(recs, weights, min_trades, target_trades)
    if not best:
        print("[grid] no candidates evaluated; skipping")
        return base_cfg, recs
    # regression guard: global (baseline or prev best)
    cur_vec = [str(get_current(base_cfg, k)[0]) for k in keys]
    cur_name = "|".join(cur_vec)
    cur_rec = next((r for r in recs if r.get('value') == cur_name), None)
    chosen = best
    if GLOBAL_BEST_S is not None and best.get('score', -1e18) < GLOBAL_BEST_S:
        if cur_rec is not None and cur_rec.get('score', -1e18) >= GLOBAL_BEST_S:
            chosen = cur_rec
        else:
            chosen = max([best, cur_rec or best], key=lambda r: r.get('score', -1e18))

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
    ap.add_argument("--prefix", default="t5m2880_fix")
    ap.add_argument("--w-equity", type=float, default=1.0)
    ap.add_argument("--w-pf", type=float, default=10.0)
    ap.add_argument("--w-dd", type=float, default=200.0)
    ap.add_argument("--w-mono", type=float, default=5.0)
    ap.add_argument("--dd-target", type=float, default=0.12)
    ap.add_argument("--min-trades", type=int, default=50)
    ap.add_argument("--target-trades", type=int, default=300)
    ap.add_argument("--plan", help="Path to external plan module (default_plan used if omitted)")
    ap.add_argument("--sleep-sec", type=float, default=0.0, help="pause between backtests in seconds")
    ap.add_argument("--jobs", type=int, default=1, help="number of parallel backtest jobs")
    args = ap.parse_args()

    global INIT_CFG
    INIT_CFG = read_yaml(Path(args.cfg))
    weights = (args.w_equity, args.w_pf, args.w_dd, args.w_mono, args.dd_target)
    global BT_SLEEP_SEC
    BT_SLEEP_SEC = args.sleep_sec

    prefix_path = Path(args.prefix)
    if prefix_path.is_absolute():
        prefix_path = Path(*prefix_path.parts[1:])
    file_prefix = prefix_path.name or "tuner"
    session_ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    base_parent = TUNE_ROOT / prefix_path.parent
    base_parent.mkdir(parents=True, exist_ok=True)
    base_session_dir = base_parent / f"{file_prefix}_{session_ts}"
    session_dir = base_session_dir
    counter = 1
    while session_dir.exists():
        session_dir = base_parent / f"{file_prefix}_{session_ts}_{counter}"
        counter += 1
    session_dir.mkdir(parents=True, exist_ok=False)
    global SESSION_DIR
    SESSION_DIR = session_dir


    base = read_yaml(Path(args.cfg))
    rays_results = []
    grid_results = []
    # ---- Baseline (original cfg) ----
    baseline_yaml = session_dir / f"{file_prefix}_baseline.yaml"
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
    log_csv = session_dir / f"{file_prefix}_tuner_log.csv"

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
        if hasattr(_mod, "GRID_VALUES_ARE_DELTAS"):
            global DELTA_MODE_DEFAULT
            DELTA_MODE_DEFAULT = bool(getattr(_mod, "GRID_VALUES_ARE_DELTAS"))
        plan = _mod.default_plan(args.limit_bars)
    else:
        plan = default_plan(args.limit_bars)


    cur_yaml = Path(args.cfg)
    for i, stage in enumerate(plan, 1):
        mode = params = None
        # Allow stages expressed as tuples/lists, dicts, or mappings with extra fields
        if isinstance(stage, (list, tuple)):
            if len(stage) >= 2:
                mode, params = stage[0], stage[1]
            elif len(stage) == 1 and isinstance(stage[0], dict) and len(stage[0]) == 1:
                mode, params = next(iter(stage[0].items()))
        elif isinstance(stage, dict):
            if len(stage) == 1:
                mode, params = next(iter(stage.items()))

        if mode is None or params is None:
            print(f"[plan] malformed stage {i}: {stage}; skipping")
            continue

        prefix = f"{file_prefix}_s{i}_{mode}"
        if not params:
            print(f"[{mode}] no parameters provided; skipping stage {i}")
            continue

        if mode == "rays":
            (pname, cand) = list(params.items())[0]
            base, rays_results = do_rays(
                base,
                args.limit_bars,
                pname,
                cand,
                prefix,
                session_dir,
                log_csv,
                weights,
                args.min_trades,
                args.target_trades,
                args.jobs,
            )
        elif mode == "grid":
            base, grid_results = do_grid(
                base,
                args.limit_bars,
                params,
                prefix,
                session_dir,
                log_csv,
                weights,
                args.min_trades,
                args.target_trades,
                args.jobs,
            )
        else:
            raise ValueError(mode)

    final = session_dir / f"{file_prefix}_final_best.yaml"
    write_yaml(base, final)
    print(f"DONE -> {final}")

    TOPK_RAYS_PLOTS = 3
    if rays_results:
        best = sorted(rays_results, key=lambda r: r.get("score", -1e18), reverse=True)[:TOPK_RAYS_PLOTS]
        for i, r in enumerate(best, 1):
            run_backtest(
                Path(r["cfg_path"]),
                args.limit_bars,
                with_plots=True,
                label=f"{file_prefix}_rays_final_{i}",
            )
    if grid_results:
        best_grid = pick_best(grid_results, weights, args.min_trades, args.target_trades)
        run_backtest(
            Path(best_grid["cfg_path"]),
            args.limit_bars,
            with_plots=True,
            label=f"{file_prefix}_grid_final",
        )
    prune_reports(args.prefix)
    tmp_dir = _tmp_dir_for_session(session_dir)
    if tmp_dir.exists():
        for p in tmp_dir.glob("*.yaml"):
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
