#!/usr/bin/env python3
# Dual-cryptomine tuner rewritten for backtester_dual_long_short_mtm.py
import argparse, csv, importlib.util, itertools, json, os, shutil, subprocess, sys, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import copy
import yaml


def deep_get(d, key):
    cur = d
    for k in key.split('.'):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def deep_set(d, key, val):
    cur = d
    parts = key.split('.')
    for k in parts[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[parts[-1]] = val


def read_yaml(p: Path):
    return yaml.safe_load(p.read_text(encoding='utf-8'))


def write_yaml(obj, p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(obj, sort_keys=False, allow_unicode=True), encoding='utf-8')


def around(val, step, n=1):
    xs = [val]
    for k in range(1, n + 1):
        xs += [round(val - k * step, 10), round(val + k * step, 10)]
    return sorted(set(xs))


def realize_grid_values(spec, current, delta_mode=True):
    if isinstance(spec, str) and spec.startswith('around:'):
        step = float(spec.split(':', 1)[1])
        return around(float(current), step, n=1)
    vals = spec if isinstance(spec, (list, tuple, set)) else [spec]
    out = []
    for v in vals:
        if delta_mode and isinstance(v, (int, float)):
            out.append(float(current) + float(v))
        else:
            out.append(v)
    if current not in out:
        out.append(current)
    try:
        out = sorted(set(float(x) for x in out))
    except Exception:
        out = list(dict.fromkeys(out))
    return out


def load_plan(plan_path: str, limit_bars: int):
    spec = importlib.util.spec_from_file_location('user_plan', plan_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Cannot import plan from {plan_path}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    delta_mode = bool(getattr(mod, 'GRID_VALUES_ARE_DELTAS', True))
    if not hasattr(mod, 'default_plan'):
        raise AttributeError(f'Plan module {plan_path} has no default_plan(limit_bars)')
    return mod.default_plan(limit_bars), delta_mode


def score_summary(s, w_pnl=1.0, w_mdd=80.0, mdd_target=0.25, min_trades=50):
    margin_calls = int(s.get('margin_call_events_total', 0) or 0)
    if margin_calls > 0:
        return -1e18
    trades_total = int(s.get('trades_total', 0) or 0)
    if trades_total < min_trades:
        return -1e12 - (min_trades - trades_total) * 1000.0
    pnl_total = float(s.get('realized_pnl_total', -1e9))
    mdd_frac = abs(float(s.get('mdd_total_frac', 0.0) or 0.0))
    mdd_penalty = max(0.0, mdd_frac - mdd_target)
    monthly = float(s.get('monthly_return_total_%', 0.0) or 0.0)
    yearly = float(s.get('yearly_return_total_%', 0.0) or 0.0)
    return (w_pnl * pnl_total) + (0.10 * monthly) + (0.01 * yearly) - (w_mdd * mdd_penalty)


def _run_backtest(backtester: str, cfg_path: str, limit_bars: int, plots: bool = False):
    cmd = [sys.executable, backtester, '--cfg', cfg_path, '--limit-bars', str(limit_bars)]
    if plots:
        plots_dir = str(Path(cfg_path).with_suffix('')) + '_plots'
        cmd += ['--plots', plots_dir]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0
    out = (p.stdout or '') + '\n' + (p.stderr or '')
    if p.returncode != 0:
        raise RuntimeError(f'Backtester failed rc={p.returncode}\n{out[-1200:]}')
    summary_path = None
    report_dir = None
    for line in out.splitlines():
        if '[files]' in line and 'dual_summary=' in line:
            parts = line.split('dual_summary=', 1)[1]
            summary_path = parts.split()[0].strip()
        if '[reports] saved to ' in line:
            report_dir = line.split('[reports] saved to ', 1)[1].strip()
    if summary_path is None and report_dir is not None:
        cand = Path(report_dir) / 'dual_summary.json'
        if cand.exists():
            summary_path = str(cand)
    if summary_path is None:
        raise RuntimeError(f'Could not locate dual_summary.json in output\n{out[-1200:]}')
    summary = json.loads(Path(summary_path).read_text(encoding='utf-8'))
    summary['elapsed_sec'] = elapsed
    summary['summary_path'] = summary_path
    summary['report_dir'] = report_dir
    return summary


def _eval_one(args_tuple):
    backtester, cfg_path, limit_bars, weights = args_tuple
    try:
        s = _run_backtest(backtester, cfg_path, limit_bars, plots=False)
        s['score'] = score_summary(s, **weights)
        return s
    except Exception as e:
        return {'score': -1e18, 'error': str(e), 'elapsed_sec': 0.0}


def append_log_csv(path: Path, rows):
    if not rows:
        return
    cols = sorted({k for r in rows for k in r.keys()})
    exists = path.exists()
    with path.open('a', newline='', encoding='utf-8') as f:
        wr = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        if not exists:
            wr.writeheader()
        for r in rows:
            wr.writerow(r)


def stage_rays(backtester, base_cfg, limit_bars, pname, cand, session_dir, file_prefix, delta_mode, weights, jobs):
    cur = deep_get(base_cfg, pname)
    vals = realize_grid_values(cand, cur, delta_mode=delta_mode)
    tmp_dir = session_dir / 'tmp'; tmp_dir.mkdir(parents=True, exist_ok=True)
    tasks = []
    for v in vals:
        cfg = copy.deepcopy(base_cfg)
        deep_set(cfg, pname, v)
        y = tmp_dir / f'{file_prefix}_rays_{pname.replace(".","_")}_{str(v).replace(".","p")}.yaml'
        write_yaml(cfg, y)
        tasks.append((str(y), v))
    recs = []
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            fut_map = {ex.submit(_eval_one, (backtester, cfg_path, limit_bars, weights)): (cfg_path, v) for cfg_path, v in tasks}
            for fut in as_completed(fut_map):
                cfg_path, v = fut_map[fut]
                r = fut.result(); r['param'] = pname; r['value'] = v; r['cfg_path'] = cfg_path; r['ts'] = datetime.utcnow().isoformat(timespec='seconds'); recs.append(r)
    else:
        for cfg_path, v in tasks:
            r = _eval_one((backtester, cfg_path, limit_bars, weights)); r['param'] = pname; r['value'] = v; r['cfg_path'] = cfg_path; r['ts'] = datetime.utcnow().isoformat(timespec='seconds'); recs.append(r)
    best = max(recs, key=lambda r: r.get('score', -1e18))
    deep_set(base_cfg, pname, best['value'])
    write_yaml(base_cfg, session_dir / f'{file_prefix}_{pname.replace(".","_")}_best.yaml')
    return base_cfg, recs, best


def stage_grid(backtester, base_cfg, limit_bars, params, session_dir, file_prefix, delta_mode, weights, jobs):
    keys = list(params.keys())
    cand_lists = {}
    for k in keys:
        cur = deep_get(base_cfg, k)
        cand_lists[k] = realize_grid_values(params[k], cur, delta_mode=delta_mode)
    tmp_dir = session_dir / 'tmp'; tmp_dir.mkdir(parents=True, exist_ok=True)
    tasks = []
    for vec in itertools.product(*[cand_lists[k] for k in keys]):
        cfg = copy.deepcopy(base_cfg)
        for k, v in zip(keys, vec):
            deep_set(cfg, k, v)
        y = tmp_dir / f'{file_prefix}_grid_' + '_'.join(str(v).replace('.', 'p') for v in vec) + '.yaml'
        write_yaml(cfg, y)
        tasks.append((str(y), vec))
    recs = []
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            fut_map = {ex.submit(_eval_one, (backtester, cfg_path, limit_bars, weights)): (cfg_path, vec) for cfg_path, vec in tasks}
            for fut in as_completed(fut_map):
                cfg_path, vec = fut_map[fut]
                r = fut.result(); r['param'] = '|'.join(keys); r['value'] = '|'.join(map(str, vec)); r['cfg_path'] = cfg_path; r['ts'] = datetime.utcnow().isoformat(timespec='seconds'); recs.append(r)
    else:
        for cfg_path, vec in tasks:
            r = _eval_one((backtester, cfg_path, limit_bars, weights)); r['param'] = '|'.join(keys); r['value'] = '|'.join(map(str, vec)); r['cfg_path'] = cfg_path; r['ts'] = datetime.utcnow().isoformat(timespec='seconds'); recs.append(r)
    best = max(recs, key=lambda r: r.get('score', -1e18))
    for k, v in zip(keys, best['value'].split('|')):
        try:
            vv = float(v)
            vv = int(vv) if vv.is_integer() else vv
        except Exception:
            vv = v
        deep_set(base_cfg, k, vv)
    write_yaml(base_cfg, session_dir / f'{file_prefix}_grid_best.yaml')
    return base_cfg, recs, best


def main():
    ap = argparse.ArgumentParser(description='Dual rays+grid tuner rewritten for backtester_dual_long_short_mtm.py')
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--backtester', required=True)
    ap.add_argument('--plan', required=True)
    ap.add_argument('--limit-bars', type=int, required=True)
    ap.add_argument('--prefix', default='dual_tuner')
    ap.add_argument('--jobs', type=int, default=1)
    ap.add_argument('--w-pnl', type=float, default=1.0)
    ap.add_argument('--w-mdd', type=float, default=80.0)
    ap.add_argument('--mdd-target', type=float, default=0.25)
    ap.add_argument('--min-trades', type=int, default=50)
    args = ap.parse_args()

    weights = {'w_pnl': args.w_pnl, 'w_mdd': args.w_mdd, 'mdd_target': args.mdd_target, 'min_trades': args.min_trades}

    plan, delta_mode = load_plan(args.plan, args.limit_bars)
    base_cfg = read_yaml(Path(args.cfg))
    file_prefix = 'DUAL'
    session_dir = Path('_reports') / '_auto_tuner_dual' / Path(args.plan).stem / f"{args.prefix}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    session_dir.mkdir(parents=True, exist_ok=False)
    log_csv = session_dir / f'{file_prefix}_tuner_log.csv'

    baseline = _run_backtest(args.backtester, args.cfg, args.limit_bars, plots=False)
    baseline['score'] = score_summary(baseline, **weights)
    baseline['param'] = 'baseline'; baseline['value'] = 'baseline'; baseline['cfg_path'] = args.cfg; baseline['ts'] = datetime.utcnow().isoformat(timespec='seconds')
    append_log_csv(log_csv, [baseline])
    best_overall = dict(baseline)
    print(f"[baseline] pnl_total={baseline.get('realized_pnl_total')} mdd={baseline.get('mdd_total_%')} margin_calls={baseline.get('margin_call_events_total')} score={baseline.get('score')}")

    all_recs = [baseline]
    for idx, stage in enumerate(plan, 1):
        if isinstance(stage, (list, tuple)) and len(stage) >= 2:
            mode, params = stage[0], stage[1]
        elif isinstance(stage, dict) and len(stage) == 1:
            mode, params = next(iter(stage.items()))
        else:
            raise ValueError(f'Bad plan stage: {stage}')
        prefix = f'{file_prefix}_s{idx}_{mode}'
        if mode == 'rays':
            pname, cand = list(params.items())[0]
            base_cfg, recs, best = stage_rays(args.backtester, base_cfg, args.limit_bars, pname, cand, session_dir, prefix, delta_mode, weights, args.jobs)
        elif mode == 'grid':
            base_cfg, recs, best = stage_grid(args.backtester, base_cfg, args.limit_bars, params, session_dir, prefix, delta_mode, weights, args.jobs)
        else:
            raise ValueError(mode)
        append_log_csv(log_csv, recs)
        all_recs.extend(recs)
        if best.get('score', -1e18) > best_overall.get('score', -1e18):
            best_overall = dict(best)
        print(f"[{mode}] best score={best.get('score')} pnl_total={best.get('realized_pnl_total')} mdd={best.get('mdd_total_%')} margin_calls={best.get('margin_call_events_total')} value={best.get('value')}")

    final_yaml = session_dir / f'{file_prefix}_final_best.yaml'
    write_yaml(base_cfg, final_yaml)
    top_csv = session_dir / 'top_results.csv'
    top = sorted(all_recs, key=lambda r: r.get('score', -1e18), reverse=True)
    append_log_csv(top_csv, top[:50])
    summary = {
        'session_dir': str(session_dir),
        'final_yaml': str(final_yaml),
        'best_overall': best_overall,
        'log_csv': str(log_csv),
        'top_csv': str(top_csv),
    }
    with (session_dir / 'tuner_summary.json').open('w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, default=str)
    print(json.dumps(summary, indent=2, default=str))
    try:
        shutil.rmtree(session_dir / 'tmp', ignore_errors=True)
    except Exception:
        pass


if __name__ == '__main__':
    main()
