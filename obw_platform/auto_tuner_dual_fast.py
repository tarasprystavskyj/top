#!/usr/bin/env python3
import argparse, copy, importlib.util, itertools, json, os, time, yaml
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import numpy as np
from backtester_dual_long_short_fast import simulate

CACHE = None
TS = None
CLOSE = None

def worker_init(npz_path):
    global CACHE, TS, CLOSE
    CACHE = np.load(npz_path)
    TS = CACHE['timestamp_s'].astype(np.int64)
    CLOSE = CACHE['close'].astype(np.float64)


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


def around(val, step, n=1):
    xs = [val]
    for k in range(1, n+1):
        xs += [round(val-k*step, 10), round(val+k*step, 10)]
    return sorted(set(xs))


def load_plan(plan_path, limit_bars):
    spec = importlib.util.spec_from_file_location('user_plan', plan_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.default_plan(limit_bars), bool(getattr(mod, 'GRID_VALUES_ARE_DELTAS', True))


def realize(spec, current, delta_mode=True):
    if isinstance(spec, str) and spec.startswith('around:'):
        return around(float(current), float(spec.split(':',1)[1]), 1)
    vals = spec if isinstance(spec, (list,tuple,set)) else [spec]
    out = []
    for v in vals:
        out.append(float(current)+float(v) if delta_mode and isinstance(v,(int,float)) else v)
    if current not in out: out.append(current)
    return out


def score(s, min_trades=50, w_pnl=1.0, w_mdd=80.0, mdd_target=0.25):
    # Priority order:
    # 1) profitability as % of starting capital
    # 2) no margin / liquidation events
    # 3) low MTM (unrealized-aware) MDD
    # 4) low realized-only MDD
    if int(s.get('margin_call_events_total', 0) or 0) > 0:
        return -1e18
    trades_total = int(s.get('trades_total', 0) or 0)
    if trades_total < min_trades:
        return -1e12 - (min_trades - trades_total) * 1000.0

    eq0 = float(s.get('equity_start_total', 0.0) or 0.0)
    pnl = float(s.get('realized_pnl_total', -1e9))
    return_pct = (pnl / eq0 * 100.0) if eq0 > 0 else -1e9

    mdd_mtm = abs(float(s.get('mdd_mtm_frac', s.get('mdd_total_frac', 0.0)) or 0.0)) * 100.0
    mdd_realized = abs(float(s.get('mdd_realized_frac', 0.0) or 0.0)) * 100.0

    # Lexicographic-like numeric score: return dominates, then MTM MDD, then realized MDD.
    return (return_pct * 1000.0 * w_pnl) - (mdd_mtm * 10.0 * w_mdd) - (mdd_realized * 5.0)


def eval_cfg(args_tuple):
    cfg, limit_bars, weights = args_tuple
    t0 = time.time()
    ts = TS[-limit_bars:] if limit_bars and limit_bars > 0 else TS
    cl = CLOSE[-limit_bars:] if limit_bars and limit_bars > 0 else CLOSE
    s = simulate(cfg, ts, cl)
    s['elapsed_sec'] = time.time()-t0
    s['score'] = score(s, **weights)
    return s


def main():
    ap = argparse.ArgumentParser(description='Fast in-process tuner for dual strategy using NPZ cache and workers')
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--npz', required=True)
    ap.add_argument('--plan', required=True)
    ap.add_argument('--limit-bars', type=int, default=0)
    ap.add_argument('--prefix', default='dual_fast')
    ap.add_argument('--jobs', type=int, default=max(1, (os.cpu_count() or 2)-1))
    ap.add_argument('--min-trades', type=int, default=50)
    ap.add_argument('--w-pnl', type=float, default=1.0)
    ap.add_argument('--w-mdd', type=float, default=80.0)
    ap.add_argument('--mdd-target', type=float, default=0.25)
    args = ap.parse_args()

    weights = {'min_trades': args.min_trades, 'w_pnl': args.w_pnl, 'w_mdd': args.w_mdd, 'mdd_target': args.mdd_target}
    base_cfg = yaml.safe_load(open(args.cfg, 'r'))
    plan, delta_mode = load_plan(args.plan, args.limit_bars)

    session = Path('_reports') / '_auto_tuner_dual_fast' / Path(args.plan).stem / f"{args.prefix}_{time.strftime('%Y%m%d_%H%M%S')}"
    session.mkdir(parents=True, exist_ok=False)
    log_csv = session / 'tuner_log.csv'

    worker_init(args.npz)
    baseline = eval_cfg((base_cfg, args.limit_bars, weights))
    baseline['param']='baseline'; baseline['value']='baseline'
    best_overall = dict(baseline)
    rows=[baseline]
    print('[baseline]', baseline)

    def append_rows(rows_list):
        import pandas as pd
        pd.DataFrame(rows_list).to_csv(log_csv, index=False)

    for idx, stage in enumerate(plan, 1):
        mode, params = (stage[0], stage[1]) if isinstance(stage, (list,tuple)) else next(iter(stage.items()))
        if mode == 'rays':
            pname, cand = list(params.items())[0]
            cur = deep_get(base_cfg, pname)
            vals = realize(cand, cur, delta_mode)
            tasks=[]
            payloads=[]
            with ProcessPoolExecutor(max_workers=args.jobs, initializer=worker_init, initargs=(args.npz,)) as ex:
                for v in vals:
                    cfg = copy.deepcopy(base_cfg); deep_set(cfg, pname, v)
                    payloads.append((v,cfg))
                    tasks.append(ex.submit(eval_cfg, (cfg, args.limit_bars, weights)))
                stage_rows=[]
                for (v,cfg), fut in zip(payloads, tasks):
                    r=fut.result(); r['param']=pname; r['value']=v; stage_rows.append(r)
                best=max(stage_rows, key=lambda x:x['score'])
                deep_set(base_cfg, pname, best['value'])
                rows.extend(stage_rows); append_rows(rows)
                if best['score'] > best_overall['score']: best_overall = dict(best)
                print('[rays]', pname, 'best=', best['value'], 'score=', best['score'])
        elif mode == 'grid':
            keys=list(params.keys())
            cand_lists=[]
            for k in keys:
                cand_lists.append(realize(params[k], deep_get(base_cfg,k), delta_mode))
            vecs=list(itertools.product(*cand_lists))
            with ProcessPoolExecutor(max_workers=args.jobs, initializer=worker_init, initargs=(args.npz,)) as ex:
                payloads=[]; tasks=[]
                for vec in vecs:
                    cfg=copy.deepcopy(base_cfg)
                    for k,v in zip(keys, vec): deep_set(cfg,k,v)
                    payloads.append((vec,cfg)); tasks.append(ex.submit(eval_cfg, (cfg, args.limit_bars, weights)))
                stage_rows=[]
                for (vec,cfg), fut in zip(payloads,tasks):
                    r=fut.result(); r['param']='|'.join(keys); r['value']='|'.join(map(str,vec)); stage_rows.append(r)
                best=max(stage_rows, key=lambda x:x['score'])
                for k,v in zip(keys, best['value'].split('|')):
                    vv=float(v); vv=int(vv) if vv.is_integer() else vv; deep_set(base_cfg,k,vv)
                rows.extend(stage_rows); append_rows(rows)
                if best['score'] > best_overall['score']: best_overall = dict(best)
                print('[grid]', 'best=', best['value'], 'score=', best['score'])
        else:
            raise ValueError(mode)

    final_yaml = session / 'final_best.yaml'
    final_yaml.write_text(yaml.safe_dump(base_cfg, sort_keys=False), encoding='utf-8')
    summary = {'session_dir': str(session), 'final_yaml': str(final_yaml), 'best_overall': best_overall, 'log_csv': str(log_csv)}
    (session/'tuner_summary.json').write_text(json.dumps(summary, indent=2, default=str), encoding='utf-8')
    print(json.dumps(summary, indent=2, default=str))

if __name__ == '__main__':
    main()
