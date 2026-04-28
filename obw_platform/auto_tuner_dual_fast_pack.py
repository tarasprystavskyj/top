#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import csv
import importlib.util
import itertools
import json
import os
import time
import yaml
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np

# Prefer core import. It is lighter than importing the CLI wrapper.
try:
    from backtester_dual_core_dynamic_v5 import simulate, pick_symbol_block
except ImportError:
    try:
        from backtester_dual_long_short_fast_pack_v2 import simulate, pick_symbol_block
    except ImportError:
        from backtester_dual_long_short_fast_pack import simulate, pick_symbol_block


CACHE = None
SERIES = None
MARKET_SYMBOL = None


def install_numpy_pickle_compat():
    """Compatibility for NPZ files saved by newer NumPy and loaded by old NumPy/Python 3.8."""
    import sys
    try:
        import numpy.core as _np_core
        sys.modules.setdefault('numpy._core', _np_core)
    except Exception:
        pass
    for _sub in ('multiarray', 'umath', 'numeric', 'fromnumeric', 'shape_base', 'records'):
        try:
            _mod = __import__('numpy.core.' + _sub, fromlist=['*'])
            sys.modules.setdefault('numpy._core.' + _sub, _mod)
        except Exception:
            pass


def parse_iso_to_epoch_s(s: str) -> int:
    import datetime as _dt
    dt = _dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    else:
        dt = dt.astimezone(_dt.timezone.utc)
    return int(dt.timestamp())


def _slice(mask, arr):
    if arr is None:
        return None
    return arr[mask]


def _pre_slice_series(ts, open_, high, low, close, volume, extras, time_from_s, time_to_s, limit_bars):
    """Slice once per worker, not once per candidate."""
    if time_from_s is not None:
        m = ts >= int(time_from_s)
        ts = ts[m]
        open_ = _slice(m, open_)
        high = _slice(m, high)
        low = _slice(m, low)
        close = close[m]
        volume = _slice(m, volume)
        extras = {k: v[m] for k, v in extras.items()}
    if time_to_s is not None:
        m = ts <= int(time_to_s)
        ts = ts[m]
        open_ = _slice(m, open_)
        high = _slice(m, high)
        low = _slice(m, low)
        close = close[m]
        volume = _slice(m, volume)
        extras = {k: v[m] for k, v in extras.items()}
    if limit_bars and int(limit_bars) > 0:
        n = int(limit_bars)
        ts = ts[-n:]
        open_ = open_[-n:] if open_ is not None else None
        high = high[-n:] if high is not None else None
        low = low[-n:] if low is not None else None
        close = close[-n:]
        volume = volume[-n:] if volume is not None else None
        extras = {k: v[-n:] for k, v in extras.items()}
    return ts, open_, high, low, close, volume, extras


def worker_init(npz_path: str, symbol: str = '', time_from_s=None, time_to_s=None, limit_bars: int = 0):
    """Load NPZ and slice the selected window once per worker process.

    Previous tuner versions created a new ProcessPoolExecutor for every stage and
    reloaded/decompressed the full NPZ in every worker for every stage. That is the
    main runtime killer for overnight sweeps.
    """
    global CACHE, SERIES, MARKET_SYMBOL
    install_numpy_pickle_compat()
    CACHE = np.load(npz_path, allow_pickle=True)
    MARKET_SYMBOL, ts_s, open_, high, low, close, volume, extras = pick_symbol_block(CACHE, symbol)
    ts_s, open_, high, low, close, volume, extras = _pre_slice_series(
        ts_s, open_, high, low, close, volume, extras, time_from_s, time_to_s, limit_bars
    )
    SERIES = {
        'timestamp_s': ts_s,
        'open': open_,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume,
        'extras': extras,
    }


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


def around(val: float, step: float, n: int = 1):
    xs = [val]
    for k in range(1, n + 1):
        xs += [round(val - k * step, 10), round(val + k * step, 10)]
    return sorted(set(xs))


def load_plan(plan_path: str, limit_bars: int):
    spec = importlib.util.spec_from_file_location('user_plan', plan_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.default_plan(limit_bars), bool(getattr(mod, 'GRID_VALUES_ARE_DELTAS', True))


def realize(spec, current, delta_mode=True):
    if isinstance(spec, str) and spec.startswith('around:'):
        return around(float(current), float(spec.split(':', 1)[1]), 1)
    vals = spec if isinstance(spec, (list, tuple, set)) else [spec]
    out = []
    for v in vals:
        out.append(float(current) + float(v) if delta_mode and isinstance(v, (int, float)) else v)
    if current not in out:
        out.append(current)
    return out


def _final_mtm_pnl(s: dict) -> float:
    if s.get('total_pnl_mtm') is not None:
        return float(s.get('total_pnl_mtm') or 0.0)
    # fallback for older backtester cores
    return float(s.get('realized_pnl_total', 0.0) or 0.0) + float(s.get('unrealized_pnl_total', 0.0) or 0.0)


def score(s, min_trades=50, w_pnl=1.0, w_mdd=80.0, w_realized_mdd=5.0, score_mode='mtm'):
    if s.get('error'):
        return -1e30
    if int(s.get('margin_call_events_total', 0) or 0) > 0:
        return -1e18
    trades_total = int(s.get('trades_total', 0) or 0)
    if trades_total < min_trades:
        return -1e12 - (min_trades - trades_total) * 1000.0

    eq0 = float(s.get('equity_start_total', 0.0) or 0.0)
    if str(score_mode).lower() in {'realized', 'realized_pnl'}:
        pnl = float(s.get('realized_pnl_total', -1e9))
    else:
        pnl = _final_mtm_pnl(s)

    return_pct = (pnl / eq0 * 100.0) if eq0 > 0 else -1e9
    mdd_mtm = abs(float(s.get('mdd_mtm_frac', s.get('mdd_total_frac', 0.0)) or 0.0)) * 100.0
    mdd_realized = abs(float(s.get('mdd_realized_frac', 0.0) or 0.0)) * 100.0
    return (return_pct * 1000.0 * w_pnl) - (mdd_mtm * 10.0 * w_mdd) - (mdd_realized * w_realized_mdd)


def eval_cfg(args_tuple):
    cfg, weights = args_tuple
    t0 = time.time()
    try:
        ts = SERIES['timestamp_s']
        open_ = SERIES['open']
        high = SERIES['high']
        low = SERIES['low']
        close = SERIES['close']
        volume = SERIES['volume']
        extras = SERIES['extras']

        s = simulate(
            cfg, ts, close,
            open_=open_, high=high, low=low, volume=volume,
            extras=extras, market_symbol=MARKET_SYMBOL
        )
        s['elapsed_sec'] = time.time() - t0
        s['total_pnl_mtm_eval'] = _final_mtm_pnl(s)
        s['score'] = score(s, **weights)
        return s
    except Exception as e:
        return {
            'error': repr(e),
            'elapsed_sec': time.time() - t0,
            'score': -1e30,
        }


def _jsonify_cell(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    try:
        return json.dumps(v, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        return str(v)


def write_rows_csv(path: Path, rows: list):
    keys = []
    for r in rows:
        for k in r.keys():
            if k not in keys:
                keys.append(k)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: _jsonify_cell(r.get(k)) for k in keys})


def _run_stage_payloads(executor, payloads, weights):
    futs = [executor.submit(eval_cfg, (cfg, weights)) for _, cfg in payloads]
    stage_rows = []
    for (value, _cfg), fut in zip(payloads, futs):
        r = fut.result()
        r['value'] = value
        stage_rows.append(r)
    return stage_rows


def main():
    ap = argparse.ArgumentParser(description='Fast tuner for dual pack strategy using NPZ cache and persistent workers')
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--npz', required=True)
    ap.add_argument('--symbol', default='')
    ap.add_argument('--plan', required=True)
    ap.add_argument('--limit-bars', type=int, default=0)
    ap.add_argument('--prefix', default='dual_fast_pack')
    ap.add_argument('--time-from', default=None)
    ap.add_argument('--time-to', default=None)
    ap.add_argument('--jobs', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument('--min-trades', type=int, default=50)
    ap.add_argument('--w-pnl', type=float, default=1.0)
    ap.add_argument('--w-mdd', type=float, default=80.0)
    ap.add_argument('--w-realized-mdd', type=float, default=5.0)
    ap.add_argument('--score-mode', choices=['mtm', 'realized'], default='mtm',
                    help='Primary optimization PnL. Default mtm = realized + final unrealized.')
    ap.add_argument('--fresh-executor-per-stage', action='store_true',
                    help='Debug fallback only. Slow: reloads NPZ each stage.')
    ap.add_argument('--debug', action='store_true', help='Verbose progress output')
    args = ap.parse_args()

    weights = {
        'min_trades': args.min_trades,
        'w_pnl': args.w_pnl,
        'w_mdd': args.w_mdd,
        'w_realized_mdd': args.w_realized_mdd,
        'score_mode': args.score_mode,
    }
    time_from_s = parse_iso_to_epoch_s(args.time_from) if args.time_from else None
    time_to_s = parse_iso_to_epoch_s(args.time_to) if args.time_to else None
    base_cfg = yaml.safe_load(open(args.cfg, 'r', encoding='utf-8'))
    plan, delta_mode = load_plan(args.plan, args.limit_bars)

    session = Path('_reports') / '_auto_tuner_dual_fast_pack' / Path(args.plan).stem / f"{args.prefix}_{time.strftime('%Y%m%d_%H%M%S')}"
    session.mkdir(parents=True, exist_ok=False)
    log_csv = session / 'tuner_log.csv'

    if args.debug:
        print(f'[cfg] npz={args.npz} symbol={args.symbol or "<auto>"} jobs={args.jobs} limit_bars={args.limit_bars} score_mode={args.score_mode}', flush=True)

    t_load0 = time.time()
    worker_init(args.npz, args.symbol, time_from_s, time_to_s, args.limit_bars)
    load_elapsed = time.time() - t_load0

    baseline = eval_cfg((base_cfg, weights))
    baseline['param'] = 'baseline'
    baseline['value'] = 'baseline'
    best_overall = dict(baseline)
    rows = [baseline]
    write_rows_csv(log_csv, rows)

    if args.debug:
        print('[baseline]', json.dumps({
            'score': baseline.get('score'),
            'mtm': baseline.get('total_pnl_mtm_eval'),
            'realized': baseline.get('realized_pnl_total'),
            'mdd_mtm_%': baseline.get('mdd_mtm_%'),
            'trades': baseline.get('trades_total'),
            'elapsed_sec': baseline.get('elapsed_sec'),
            'load_elapsed_sec': load_elapsed,
        }, ensure_ascii=False), flush=True)

    def run_one_stage(executor, mode, params, stage_idx):
        nonlocal base_cfg, best_overall, rows
        stage_t0 = time.time()

        if mode == 'rays':
            pname, cand = list(params.items())[0]
            cur = deep_get(base_cfg, pname)
            vals = realize(cand, cur, delta_mode)
            payloads = []
            for v in vals:
                cfg = copy.deepcopy(base_cfg)
                deep_set(cfg, pname, v)
                payloads.append((v, cfg))

            stage_rows = _run_stage_payloads(executor, payloads, weights)
            for r in stage_rows:
                r['param'] = pname
                r['stage_idx'] = stage_idx
                r['stage_mode'] = 'rays'

            best = max(stage_rows, key=lambda x: x['score'])
            deep_set(base_cfg, pname, best['value'])

        elif mode == 'grid':
            keys = list(params.keys())
            cand_lists = [realize(params[k], deep_get(base_cfg, k), delta_mode) for k in keys]
            vecs = list(itertools.product(*cand_lists))
            payloads = []
            for vec in vecs:
                cfg = copy.deepcopy(base_cfg)
                for k, v in zip(keys, vec):
                    deep_set(cfg, k, v)
                payloads.append((vec, cfg))

            stage_rows = _run_stage_payloads(executor, payloads, weights)
            for r in stage_rows:
                r['param'] = '|'.join(keys)
                r['value'] = '|'.join(map(str, r['value'])) if not isinstance(r['value'], str) else r['value']
                r['stage_idx'] = stage_idx
                r['stage_mode'] = 'grid'

            best = max(stage_rows, key=lambda x: x['score'])
            for k, v in zip(keys, str(best['value']).split('|')):
                vv = float(v)
                vv = int(vv) if vv.is_integer() else vv
                deep_set(base_cfg, k, vv)
        else:
            raise ValueError(mode)

        rows.extend(stage_rows)
        write_rows_csv(log_csv, rows)

        if best['score'] > best_overall['score']:
            best_overall = dict(best)

        if args.debug:
            print(f'[{mode}] stage={stage_idx} candidates={len(stage_rows)} best_value={best.get("value")} score={best.get("score")} mtm={best.get("total_pnl_mtm_eval")} realized={best.get("realized_pnl_total")} stage_sec={time.time()-stage_t0:.2f}', flush=True)

    if args.fresh_executor_per_stage:
        # Slow but useful for debugging worker lifecycle issues.
        for stage_idx, stage in enumerate(plan, start=1):
            mode, params = (stage[0], stage[1]) if isinstance(stage, (list, tuple)) else next(iter(stage.items()))
            with ProcessPoolExecutor(
                max_workers=args.jobs,
                initializer=worker_init,
                initargs=(args.npz, args.symbol, time_from_s, time_to_s, args.limit_bars),
            ) as ex:
                run_one_stage(ex, mode, params, stage_idx)
    else:
        # Fast path: persistent worker pool. NPZ is loaded once per worker for the whole tuning run.
        with ProcessPoolExecutor(
            max_workers=args.jobs,
            initializer=worker_init,
            initargs=(args.npz, args.symbol, time_from_s, time_to_s, args.limit_bars),
        ) as ex:
            for stage_idx, stage in enumerate(plan, start=1):
                mode, params = (stage[0], stage[1]) if isinstance(stage, (list, tuple)) else next(iter(stage.items()))
                run_one_stage(ex, mode, params, stage_idx)

    final_yaml = session / 'final_best.yaml'
    final_yaml.write_text(yaml.safe_dump(base_cfg, sort_keys=False), encoding='utf-8')
    summary = {
        'session_dir': str(session),
        'final_yaml': str(final_yaml),
        'best_overall': best_overall,
        'log_csv': str(log_csv),
        'score_mode': args.score_mode,
        'npz_load_elapsed_main_sec': load_elapsed,
        'persistent_executor': not args.fresh_executor_per_stage,
    }
    (session / 'tuner_summary.json').write_text(json.dumps(summary, indent=2, default=str), encoding='utf-8')
    print(json.dumps(summary, indent=2, default=str), flush=True)


if __name__ == '__main__':
    main()
