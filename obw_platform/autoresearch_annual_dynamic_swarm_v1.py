#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import yaml

from backtester_dual_core_dynamic_v5 import pick_symbol_block, simulate

# Paradigm-inspired search ideas adapted to crypto pack search:
# - waves of challengers
# - one "from scratch" mutant per wave
# - validate on multiple windows, not a single lucky run
# - promote only finalists to the expensive full-year validation stage

_MUTATION_SPECS = {
    'strategy_params_long.tpPercent': (0.008, 0.055, 0.85),
    'strategy_params_short.tpPercent': (0.008, 0.055, 0.85),
    'strategy_params_long.callbackPercent': (0.004, 0.030, 0.90),
    'strategy_params_short.callbackPercent': (0.004, 0.030, 0.90),
    'strategy_params_long.subSellTPPercent': (0.008, 0.090, 0.95),
    'strategy_params_short.subSellTPPercent': (0.008, 0.090, 0.95),
    'strategy_params_long.baseOrderPctEq': (0.03, 0.28, 1.00),
    'strategy_params_short.baseOrderPctEq': (0.03, 0.28, 1.00),
    'strategy_params_long.linearDropPercent': (0.008, 0.090, 1.00),
    'strategy_params_short.linearRisePercent': (0.008, 0.090, 1.00),
    'strategy_params_long.maxLongInvestPct': (0.05, 0.55, 1.05),
    'strategy_params_short.maxShortInvestPct': (0.05, 0.55, 1.05),
    'strategy_params_long.mult2': (0.04, 0.45, 1.00),
    'strategy_params_short.mult2': (0.04, 0.45, 1.00),
    'strategy_params_long.drop1': (0.008, 0.080, 1.00),
    'strategy_params_short.rise1': (0.008, 0.080, 1.00),
}


def _deep_get(d: Dict[str, Any], dotted: str):
    cur = d
    for part in dotted.split('.'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _deep_set(d: Dict[str, Any], dotted: str, value: Any):
    cur = d
    parts = dotted.split('.')
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _clamp_positive(x: float, floor: float = 1e-9) -> float:
    return max(float(floor), float(x))


def _mutate_value(rng: random.Random, base: float, step_min: float, step_max: float, scale: float) -> float:
    step = rng.uniform(step_min, step_max) * scale
    direction = -1.0 if rng.random() < 0.5 else 1.0
    return _clamp_positive(base + direction * step)


def mutate_cfg(base_cfg: Dict[str, Any], rng: random.Random, intensity: float = 1.0, from_scratch: bool = False):
    cfg = copy.deepcopy(base_cfg)
    edits = []
    keys = list(_MUTATION_SPECS.keys())
    rng.shuffle(keys)
    n_changes = rng.randint(6, 11) if not from_scratch else rng.randint(11, min(16, len(keys)))
    scale_mult = (2.0 if from_scratch else 1.0) * intensity
    for key in keys[:n_changes]:
        cur = _deep_get(cfg, key)
        if not isinstance(cur, (int, float)):
            continue
        lo, hi, scale = _MUTATION_SPECS[key]
        new_v = _mutate_value(rng, float(cur), lo, hi, scale * scale_mult)
        _deep_set(cfg, key, round(new_v, 6))
        edits.append((key, float(cur), float(new_v)))
    return cfg, edits


def load_npz(npz_path: str, symbol: str = ''):
    data = np.load(npz_path, allow_pickle=True)
    return pick_symbol_block(data, symbol)


def slice_arrays(ts, open_, high, low, close, volume, extras, start_s: int, end_s: int):
    m = (ts >= start_s) & (ts < end_s)
    return (
        ts[m],
        open_[m] if open_ is not None else None,
        high[m] if high is not None else None,
        low[m] if low is not None else None,
        close[m],
        volume[m] if volume is not None else None,
        {k: v[m] for k, v in extras.items()},
    )


def build_windows(ts: np.ndarray, window_days: int, n_windows: int):
    if len(ts) == 0:
        return []
    start = int(ts[0])
    end = int(ts[-1])
    sec_per_day = 86400
    win = int(window_days * sec_per_day)
    span = max(0, end - start - win)
    if n_windows <= 1 or span <= 0:
        starts = [start]
    else:
        starts = np.linspace(start, start + span, n_windows).astype(np.int64).tolist()
    out = []
    for s in starts:
        e = int(s + win)
        out.append({
            'start_s': int(s),
            'end_s': int(min(e, end + 1)),
            'start_iso': pd.to_datetime(int(s), unit='s', utc=True).isoformat(),
            'end_iso': pd.to_datetime(int(min(e, end + 1)), unit='s', utc=True).isoformat(),
        })
    return out


@dataclass
class EvalSummary:
    pnl: float
    mdd_pct: float
    rmdd_pct: float
    trades: float
    margin_calls: int
    score: float
    daily_return_pct: float
    elapsed_sec: float


def eval_cfg(cfg, ts, open_, high, low, close, volume, extras, market_symbol, model_override) -> Tuple[Dict[str, Any], EvalSummary]:
    t0 = time.time()
    out = simulate(
        cfg, ts, close,
        open_=open_, high=high, low=low, volume=volume, extras=extras,
        market_symbol=market_symbol, model_override=model_override, export_curves=False,
    )
    elapsed = time.time() - t0
    pnl = float(out.get('realized_pnl_total', 0.0) or 0.0)
    mdd_pct = abs(float(out.get('mdd_mtm_%', 0.0) or 0.0))
    rmdd_pct = abs(float(out.get('mdd_realized_%', 0.0) or 0.0))
    trades = float(out.get('trades_total', 0.0) or 0.0)
    margin_calls = int(out.get('margin_call_events_total', 0) or 0)
    eq0 = float(out.get('equity_start_total', 200.0) or 200.0)
    days = max(1e-9, (int(ts[-1]) - int(ts[0])) / 86400.0) if len(ts) > 1 else 1.0
    daily_return_pct = (pnl / max(eq0, 1e-9)) * 100.0 / days
    score = daily_return_pct - 0.10 * mdd_pct - 0.03 * rmdd_pct
    if margin_calls > 0:
        score -= 1000.0 * margin_calls
    if mdd_pct > 45.0:
        score -= 50.0 + (mdd_pct - 45.0) * 2.0
    if trades < 100:
        score -= (100.0 - trades) * 0.03
    out['score'] = float(score)
    out['daily_return_pct'] = float(daily_return_pct)
    out['elapsed_sec'] = float(elapsed)
    return out, EvalSummary(
        pnl=pnl, mdd_pct=mdd_pct, rmdd_pct=rmdd_pct, trades=trades,
        margin_calls=margin_calls, score=float(score), daily_return_pct=float(daily_return_pct), elapsed_sec=float(elapsed)
    )


def evaluate_on_windows(cfg, data_tuple, windows, market_symbol, model_override):
    ts, open_, high, low, close, volume, extras = data_tuple
    rows = []
    raw_results = []
    for w in windows:
        sub = slice_arrays(ts, open_, high, low, close, volume, extras, w['start_s'], w['end_s'])
        if len(sub[0]) < 100:
            continue
        out, s = eval_cfg(cfg, *sub, market_symbol, model_override)
        row = {
            **w,
            'bars': int(len(sub[0])),
            'realized_pnl_total': s.pnl,
            'mdd_mtm_pct': s.mdd_pct,
            'mdd_realized_pct': s.rmdd_pct,
            'trades_total': s.trades,
            'margin_call_events_total': s.margin_calls,
            'score': s.score,
            'daily_return_pct': s.daily_return_pct,
            'elapsed_sec': s.elapsed_sec,
        }
        rows.append(row)
        raw_results.append(out)
    if not rows:
        return {'windows': [], 'aggregate': {'robust_score': -1e18}}
    df = pd.DataFrame(rows)
    robust_score = (
        0.45 * float(df['score'].mean())
        + 0.35 * float(df['score'].median())
        + 0.20 * float(df['score'].min())
        - 0.25 * float(df['score'].std(ddof=0) if len(df) > 1 else 0.0)
    )
    agg = {
        'robust_score': float(robust_score),
        'mean_score': float(df['score'].mean()),
        'median_score': float(df['score'].median()),
        'min_score': float(df['score'].min()),
        'std_score': float(df['score'].std(ddof=0) if len(df) > 1 else 0.0),
        'mean_daily_return_pct': float(df['daily_return_pct'].mean()),
        'median_daily_return_pct': float(df['daily_return_pct'].median()),
        'min_daily_return_pct': float(df['daily_return_pct'].min()),
        'mean_pnl': float(df['realized_pnl_total'].mean()),
        'median_pnl': float(df['realized_pnl_total'].median()),
        'mean_mdd_mtm_pct': float(df['mdd_mtm_pct'].mean()),
        'max_mdd_mtm_pct': float(df['mdd_mtm_pct'].max()),
        'margin_call_events_total': int(df['margin_call_events_total'].sum()),
        'windows_evaluated': int(len(df)),
        'elapsed_sec_sum': float(df['elapsed_sec'].sum()),
    }
    return {'windows': rows, 'aggregate': agg, 'raw_results': raw_results}


def diff_cfg(base_cfg: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    diffs = []
    for key in _MUTATION_SPECS:
        a = _deep_get(base_cfg, key)
        b = _deep_get(cfg, key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(float(a) - float(b)) > 1e-12:
            diffs.append({'key': key, 'base': float(a), 'new': float(b), 'delta': float(b) - float(a)})
    return diffs


def search(base_cfg, seed_cfgs, data_tuple, market_symbol, model_override, *, waves: int, variants_per_wave: int, topk_finalists: int, seed: int, windows: List[Dict[str, Any]]):
    rng = random.Random(seed)

    population = []
    journals = []

    # initial candidates
    initial_named = [('baseline', copy.deepcopy(base_cfg))]
    for idx, scfg in enumerate(seed_cfgs):
        initial_named.append((f'seed_cfg_{idx+1}', copy.deepcopy(scfg)))

    for name, cfg in initial_named:
        ev = evaluate_on_windows(cfg, data_tuple, windows, market_symbol, model_override)
        population.append({'name': name, 'cfg': cfg, 'window_eval': ev})

    population.sort(key=lambda x: x['window_eval']['aggregate']['robust_score'], reverse=True)
    incumbent = copy.deepcopy(population[0])
    best_window = copy.deepcopy(population[0])

    for wave in range(1, waves + 1):
        wave_rows = []
        candidates = []
        # current incumbent stays in candidate pool
        candidates.append({'name': f'wave_{wave}_incumbent', 'cfg': copy.deepcopy(incumbent['cfg']), 'edits': []})
        parents = [copy.deepcopy(incumbent['cfg']), copy.deepcopy(base_cfg)] + [copy.deepcopy(c['cfg']) for c in population[:2]]
        for variant in range(variants_per_wave):
            from_scratch = (variant == variants_per_wave - 1)
            parent = copy.deepcopy(base_cfg if from_scratch else rng.choice(parents))
            intensity = 1.0 + 0.25 * max(0, wave - 1)
            cfg, edits = mutate_cfg(parent, rng, intensity=intensity, from_scratch=from_scratch)
            candidates.append({
                'name': f'wave_{wave}_variant_{variant}',
                'cfg': cfg,
                'edits': edits,
                'from_scratch': from_scratch,
            })
        for cand in candidates:
            ev = evaluate_on_windows(cand['cfg'], data_tuple, windows, market_symbol, model_override)
            row = {
                'wave': wave,
                'name': cand['name'],
                'from_scratch': bool(cand.get('from_scratch', False)),
                'robust_score': ev['aggregate']['robust_score'],
                'mean_daily_return_pct': ev['aggregate'].get('mean_daily_return_pct'),
                'min_daily_return_pct': ev['aggregate'].get('min_daily_return_pct'),
                'mean_pnl': ev['aggregate'].get('mean_pnl'),
                'mean_mdd_mtm_pct': ev['aggregate'].get('mean_mdd_mtm_pct'),
                'margin_call_events_total': ev['aggregate'].get('margin_call_events_total'),
                'edits': cand.get('edits', []),
            }
            wave_rows.append(row)
            population.append({'name': cand['name'], 'cfg': cand['cfg'], 'window_eval': ev, 'edits': cand.get('edits', [])})
            if ev['aggregate']['robust_score'] > best_window['window_eval']['aggregate']['robust_score']:
                best_window = {'name': cand['name'], 'cfg': copy.deepcopy(cand['cfg']), 'window_eval': ev, 'edits': cand.get('edits', [])}
        population.sort(key=lambda x: x['window_eval']['aggregate']['robust_score'], reverse=True)
        incumbent = copy.deepcopy(population[0])
        journals.extend(wave_rows)

    # Deduplicate finalists by param diff signature.
    seen = set()
    finalists = []
    for cand in population:
        dif = tuple((d['key'], round(d['new'], 6)) for d in diff_cfg(base_cfg, cand['cfg']))
        if dif in seen:
            continue
        seen.add(dif)
        finalists.append(cand)
        if len(finalists) >= topk_finalists:
            break

    full_year = []
    ts, open_, high, low, close, volume, extras = data_tuple
    for cand in finalists:
        out, _summary = eval_cfg(cand['cfg'], ts, open_, high, low, close, volume, extras, market_symbol, model_override)
        full_year.append({
            'name': cand['name'],
            'cfg': cand['cfg'],
            'window_eval': cand['window_eval'],
            'full_year': out,
            'diff_vs_base': diff_cfg(base_cfg, cand['cfg']),
        })

    def final_rank_key(item):
        fy = item['full_year']
        we = item['window_eval']['aggregate']
        return (
            0.65 * float(fy['score'])
            + 0.25 * float(we['robust_score'])
            + 0.10 * float(we.get('min_score', -1e9))
        )

    full_year.sort(key=final_rank_key, reverse=True)
    champion = full_year[0] if full_year else None
    return {
        'windows': windows,
        'journal': journals,
        'baseline_window_eval': population[[p['name'] for p in population].index('baseline')]['window_eval'] if any(p['name']=='baseline' for p in population) else None,
        'best_window_candidate': {
            'name': best_window['name'],
            'window_eval': best_window['window_eval'],
            'diff_vs_base': diff_cfg(base_cfg, best_window['cfg']),
        },
        'full_year_finalists': [{
            'name': item['name'],
            'window_aggregate': item['window_eval']['aggregate'],
            'full_year': item['full_year'],
            'diff_vs_base': item['diff_vs_base'],
        } for item in full_year],
        'champion': {
            'name': champion['name'],
            'cfg': champion['cfg'],
            'window_aggregate': champion['window_eval']['aggregate'],
            'full_year': champion['full_year'],
            'diff_vs_base': champion['diff_vs_base'],
        } if champion else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--seed-cfg', action='append', default=[])
    ap.add_argument('--npz', required=True)
    ap.add_argument('--symbol', default='')
    ap.add_argument('--window-days', type=int, default=30)
    ap.add_argument('--n-windows', type=int, default=4)
    ap.add_argument('--waves', type=int, default=2)
    ap.add_argument('--variants-per-wave', type=int, default=6)
    ap.add_argument('--topk-finalists', type=int, default=3)
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--dynamic-slippage-json', required=True)
    ap.add_argument('--report-out', required=True)
    ap.add_argument('--best-cfg-out', required=True)
    args = ap.parse_args()

    base_cfg = yaml.safe_load(Path(args.cfg).read_text(encoding='utf-8'))
    seed_cfgs = [yaml.safe_load(Path(p).read_text(encoding='utf-8')) for p in args.seed_cfg]
    market_symbol, ts, open_, high, low, close, volume, extras = load_npz(args.npz, args.symbol)
    data_tuple = (ts, open_, high, low, close, volume, extras)
    windows = build_windows(ts, args.window_days, args.n_windows)
    model_override = json.loads(args.dynamic_slippage_json)

    out = search(
        base_cfg, seed_cfgs, data_tuple, market_symbol, model_override,
        waves=args.waves, variants_per_wave=args.variants_per_wave,
        topk_finalists=args.topk_finalists, seed=args.seed, windows=windows,
    )

    Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report_out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    if out.get('champion'):
        Path(args.best_cfg_out).write_text(yaml.safe_dump(out['champion']['cfg'], sort_keys=False, allow_unicode=True), encoding='utf-8')
        champion = out['champion']
        summary = {
            'champion': champion['name'],
            'full_year_pnl': champion['full_year']['realized_pnl_total'],
            'full_year_daily_return_pct': champion['full_year']['daily_return_pct'],
            'full_year_mdd_mtm_pct': champion['full_year']['mdd_mtm_%'],
            'window_robust_score': champion['window_aggregate']['robust_score'],
        }
    else:
        summary = {'champion': None}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
