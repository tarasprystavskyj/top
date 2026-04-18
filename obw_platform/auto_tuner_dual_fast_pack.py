#!/usr/bin/env python3
from __future__ import annotations
import argparse, copy, importlib.util, itertools, json, os, time, yaml
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
from backtester_dual_long_short_fast_pack import simulate, pick_symbol_block

CACHE = None
SERIES = None
MARKET_SYMBOL = None


def parse_iso_to_epoch_s(s: str) -> int:
    import datetime as _dt
    dt = _dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    else:
        dt = dt.astimezone(_dt.timezone.utc)
    return int(dt.timestamp())


def worker_init(npz_path: str, symbol: str = ''):
    global CACHE, SERIES, MARKET_SYMBOL
    CACHE = np.load(npz_path, allow_pickle=True)
    MARKET_SYMBOL, ts_s, open_, high, low, close, volume, extras = pick_symbol_block(CACHE, symbol)
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


def score(s, min_trades=50, w_pnl=1.0, w_mdd=80.0, w_realized_mdd=5.0):
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
    return (return_pct * 1000.0 * w_pnl) - (mdd_mtm * 10.0 * w_mdd) - (mdd_realized * w_realized_mdd)


def _slice(mask, arr):
    if arr is None:
        return None
    return arr[mask]


def eval_cfg(args_tuple):
    cfg, limit_bars, weights, time_from_s, time_to_s = args_tuple
    t0 = time.time()
    ts = SERIES['timestamp_s']
    open_ = SERIES['open']
    high = SERIES['high']
    low = SERIES['low']
    close = SERIES['close']
    volume = SERIES['volume']
    extras = SERIES['extras']
    if time_from_s is not None:
        m = ts >= time_from_s
        ts = ts[m]; open_ = _slice(m, open_); high = _slice(m, high); low = _slice(m, low); close = close[m]; volume = _slice(m, volume)
        extras = {k: v[m] for k, v in extras.items()}
    if time_to_s is not None:
        m = ts <= time_to_s
        ts = ts[m]; open_ = _slice(m, open_); high = _slice(m, high); low = _slice(m, low); close = close[m]; volume = _slice(m, volume)
        extras = {k: v[m] for k, v in extras.items()}
    if limit_bars and limit_bars > 0:
        ts = ts[-limit_bars:]
        open_ = open_[-limit_bars:] if open_ is not None else None
        high = high[-limit_bars:] if high is not None else None
        low = low[-limit_bars:] if low is not None else None
        close = close[-limit_bars:]
        volume = volume[-limit_bars:] if volume is not None else None
        extras = {k: v[-limit_bars:] for k, v in extras.items()}
    s = simulate(cfg, ts, close, open_=open_, high=high, low=low, volume=volume, extras=extras, market_symbol=MARKET_SYMBOL)
    s['elapsed_sec'] = time.time() - t0
    s['score'] = score(s, **weights)
    return s


def main():
    ap = argparse.ArgumentParser(description='Fast tuner for dual pack strategy using NPZ cache and workers')
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
    ap.add_argument('--debug', action='store_true', help='Verbose progress output')
    args = ap.parse_args()

    weights = {
        'min_trades': args.min_trades,
        'w_pnl': args.w_pnl,
        'w_mdd': args.w_mdd,
        'w_realized_mdd': args.w_realized_mdd,
    }
    time_from_s = parse_iso_to_epoch_s(args.time_from) if args.time_from else None
    time_to_s = parse_iso_to_epoch_s(args.time_to) if args.time_to else None
    base_cfg = yaml.safe_load(open(args.cfg, 'r', encoding='utf-8'))
    plan, delta_mode = load_plan(args.plan, args.limit_bars)

    session = Path('_reports') / '_auto_tuner_dual_fast_pack' / Path(args.plan).stem / f"{args.prefix}_{time.strftime('%Y%m%d_%H%M%S')}"
    session.mkdir(parents=True, exist_ok=False)
    log_csv = session / 'tuner_log.csv'

    if args.debug:
        print(f'[cfg] npz={args.npz} symbol={args.symbol or "<auto>"} jobs={args.jobs} limit_bars={args.limit_bars}', flush=True)
    worker_init(args.npz, args.symbol)
    baseline = eval_cfg((base_cfg, args.limit_bars, weights, time_from_s, time_to_s))
    baseline['param'] = 'baseline'
    baseline['value'] = 'baseline'
    best_overall = dict(baseline)
    rows = [baseline]
    if args.debug:
        print('[baseline]', baseline, flush=True)

    def append_rows(rows_list):
        import pandas as pd
        pd.DataFrame(rows_list).to_csv(log_csv, index=False)

    for stage in plan:
        mode, params = (stage[0], stage[1]) if isinstance(stage, (list, tuple)) else next(iter(stage.items()))
        if mode == 'rays':
            pname, cand = list(params.items())[0]
            cur = deep_get(base_cfg, pname)
            vals = realize(cand, cur, delta_mode)
            payloads = []
            with ProcessPoolExecutor(max_workers=args.jobs, initializer=worker_init, initargs=(args.npz, args.symbol)) as ex:
                futs = []
                for v in vals:
                    cfg = copy.deepcopy(base_cfg)
                    deep_set(cfg, pname, v)
                    payloads.append((v, cfg))
                    futs.append(ex.submit(eval_cfg, (cfg, args.limit_bars, weights, time_from_s, time_to_s)))
                stage_rows = []
                for (v, _cfg), fut in zip(payloads, futs):
                    r = fut.result()
                    r['param'] = pname
                    r['value'] = v
                    stage_rows.append(r)
            best = max(stage_rows, key=lambda x: x['score'])
            deep_set(base_cfg, pname, best['value'])
            rows.extend(stage_rows)
            append_rows(rows)
            if best['score'] > best_overall['score']:
                best_overall = dict(best)
            if args.debug:
                print('[rays]', pname, 'best=', best['value'], 'score=', best['score'], flush=True)
        elif mode == 'grid':
            keys = list(params.keys())
            cand_lists = [realize(params[k], deep_get(base_cfg, k), delta_mode) for k in keys]
            vecs = list(itertools.product(*cand_lists))
            payloads = []
            with ProcessPoolExecutor(max_workers=args.jobs, initializer=worker_init, initargs=(args.npz, args.symbol)) as ex:
                futs = []
                for vec in vecs:
                    cfg = copy.deepcopy(base_cfg)
                    for k, v in zip(keys, vec):
                        deep_set(cfg, k, v)
                    payloads.append((vec, cfg))
                    futs.append(ex.submit(eval_cfg, (cfg, args.limit_bars, weights, time_from_s, time_to_s)))
                stage_rows = []
                for (vec, _cfg), fut in zip(payloads, futs):
                    r = fut.result()
                    r['param'] = '|'.join(keys)
                    r['value'] = '|'.join(map(str, vec))
                    stage_rows.append(r)
            best = max(stage_rows, key=lambda x: x['score'])
            for k, v in zip(keys, best['value'].split('|')):
                vv = float(v)
                vv = int(vv) if vv.is_integer() else vv
                deep_set(base_cfg, k, vv)
            rows.extend(stage_rows)
            append_rows(rows)
            if best['score'] > best_overall['score']:
                best_overall = dict(best)
            if args.debug:
                print('[grid]', 'best=', best['value'], 'score=', best['score'], flush=True)
        else:
            raise ValueError(mode)

    final_yaml = session / 'final_best.yaml'
    final_yaml.write_text(yaml.safe_dump(base_cfg, sort_keys=False), encoding='utf-8')
    summary = {
        'session_dir': str(session),
        'final_yaml': str(final_yaml),
        'best_overall': best_overall,
        'log_csv': str(log_csv),
    }
    (session / 'tuner_summary.json').write_text(json.dumps(summary, indent=2, default=str), encoding='utf-8')
    print(json.dumps(summary, indent=2, default=str), flush=True)


if __name__ == '__main__':
    main()
