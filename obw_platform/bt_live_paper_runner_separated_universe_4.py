#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Dict, List

try:
    from runners.live_debug_bundle import (
        ensure_live_debug_bundle_db,
        install_stdio_capture,
        record_run_meta,
        snapshot_files,
        debug_event,
        finalize_bundle,
        exception_text,
    )
except Exception:
    def ensure_live_debug_bundle_db(db_path: str) -> None:
        return None
    def install_stdio_capture(db_path: str, run_id: str):
        return lambda: None
    def record_run_meta(db_path: str, run_id: str, argv=None, env=None, extra=None) -> None:
        return None
    def snapshot_files(db_path: str, run_id: str, paths):
        return None
    def debug_event(db_path: str, run_id: str, event_type: str, payload=None, level: str = 'INFO') -> None:
        return None
    def finalize_bundle(db_path: str, run_id: str, status: str = 'ok', error_text: str = '', results_dir: str = '', extra_files=None):
        return None
    def exception_text() -> str:
        return ''


def _read_text(path: str) -> str:
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _load_yaml_or_json(path: str) -> Dict[str, Any]:
    if path.endswith('.json'):
        import json
        return json.loads(_read_text(path))
    import yaml
    return yaml.safe_load(_read_text(path))


def _split_csv(s: str) -> List[str]:
    return [x.strip() for x in str(s or '').split(',') if x.strip()]


def _read_universe_file(path: str) -> List[str]:
    syms: List[str] = []
    if not path:
        return syms
    try:
        if not os.path.isabs(path):
            udir = os.path.join(os.path.dirname(__file__), 'universe')
            path = os.path.join(udir, os.path.basename(path))
        with open(path, 'r', encoding='utf-8') as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith('#'):
                    syms.append(ln)
    except Exception:
        pass
    return syms


def _merge_universe(cfg_uni: Dict[str, Any], file_syms: List[str], allow_cli: List[str], deny_cli: List[str]) -> Dict[str, Any]:
    uni = dict(cfg_uni or {})
    allow = set(uni.get('allow', []) or [])
    deny = set(uni.get('deny', []) or [])
    allow.update(file_syms)
    allow.update(allow_cli)
    deny.update(deny_cli)
    if file_syms and 'file' not in uni:
        uni['file'] = '<cli>'
    uni['allow'] = sorted(allow)
    uni['deny'] = sorted(deny)
    return uni


def _hint_restrict_symbols(cfg: Dict[str, Any], allow: List[str]) -> None:
    if allow:
        cfg.setdefault('universe', {})['allow'] = list(allow)
        cfg.setdefault('md_builder', {})['restrict_to_symbols'] = list(allow)
        cfg['symbols_whitelist'] = list(allow)
        os.environ['RS_UNIVERSE_ALLOW'] = ','.join(allow)
        os.environ['RS_SYMBOLS_WHITELIST'] = ','.join(allow)


def _run_backtest(cfg_path: str, limit_bars: int | None):
    bt_entry = os.path.join(os.getcwd(), 'backtester_core.py')
    if not os.path.exists(bt_entry):
        raise SystemExit('backtester_core.py not found. Run from the project root.')
    cmd = [sys.executable, bt_entry, '--cfg', cfg_path]
    if limit_bars and int(limit_bars) > 0:
        cmd += ['--limit-bars', str(int(limit_bars))]
    print('[backtest] exec:', ' '.join(cmd))
    os.execvp(sys.executable, cmd)


def _is_dual_cfg(cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get('strategy_class_long') and cfg.get('strategy_class_short'))


def _run_live(cfg: Dict[str, Any], args):
    if getattr(args, 'live_runner_module', ''):
        try:
            import importlib
            mod = importlib.import_module(str(args.live_runner_module))
            fn = getattr(mod, 'run_live')
            print(f'[live] using {args.live_runner_module}.run_live')
            return fn(cfg, args)
        except Exception as e:
            print(f'[live] {args.live_runner_module}.run_live not available:', e)
            print('[live] falling back to auto runner selection')
    if _is_dual_cfg(cfg):
        try:
            from runners.live_runner_dual import run_live as _run_live_dual
            print('[live] using runners.live_runner_dual.run_live')
            return _run_live_dual(cfg, args)
        except Exception as e:
            print('[live] runners.live_runner_dual.run_live not available:', e)
            print('[live] falling back to runners.live_runner.run_live')
    try:
        from runners.live_runner import run_live as _run_live_single
        print('[live] using runners.live_runner.run_live')
        return _run_live_single(cfg, args)
    except Exception as e:
        print('[live] runners.live_runner.run_live not available:', e)
        print('[live] Falling back to heartbeat... (CTRL+C to exit)')
        while True:
            print('.', end='', flush=True)
            time.sleep(max(1, int(getattr(args, 'poll_sec', 10) or 10)))


def _run_paper_api(cfg: Dict[str, Any], args):
    try:
        from runners.paper_api_runner import run_paper_api as _run_paper_api
        return _run_paper_api(cfg, args)
    except Exception as e:
        print('[paper-api] runners.paper_api_runner.run_paper_api not available:', e)
        print('[paper-api] Using minimal fallback loop with heartbeat only.')
        poll = int(getattr(args, 'poll_sec', 10) or 10)
        while True:
            print('.', end='', flush=True)
            time.sleep(poll)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['backtest', 'paper', 'live'], required=True)
    ap.add_argument('--paper-source', choices=['db', 'api'], default='api')
    ap.add_argument('--orders-db', default='')
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--db')
    ap.add_argument('--results-dir', default='')
    ap.add_argument('--limit-bars', type=int, default=0)
    ap.add_argument('--session-db', default='')
    ap.add_argument('--cache-out', default='')
    ap.add_argument('--hour-cache', choices=['off', 'save', 'load'], default='off')
    ap.add_argument('--env-file', default='')
    ap.add_argument('--exchange', default='bingx')
    ap.add_argument('--symbol-format', default='usdtm')
    ap.add_argument('--poll-sec', type=int, default=2)
    ap.add_argument('--bar-delay-sec', type=int, default=1)
    ap.add_argument('--limit_klines', type=int, default=180)
    ap.add_argument('--prewarm-bars', type=int, default=None)
    ap.add_argument('--prewarm-hours', type=int, default=None)
    ap.add_argument('--debug', action='store_true')
    ap.add_argument('--heat-report', action='store_true')
    ap.add_argument('--universe-file', default='')
    ap.add_argument('--allow-symbols', default='')
    ap.add_argument('--deny-symbols', default='')
    ap.add_argument('--live-runner-module', default='', help='Explicit live runner module path, e.g. runners.live_runner_dual_verbose_debug')
    args = ap.parse_args()

    cfg_name = os.path.splitext(os.path.basename(args.cfg))[0]
    cfg = _load_yaml_or_json(args.cfg)
    timeframe = str(cfg.get('timeframe') or 'na')
    if not args.results_dir:
        args.results_dir = os.path.join('_reports', '_live', f'livecfg_{cfg_name}_{timeframe}')
    os.makedirs(args.results_dir, exist_ok=True)
    if args.session_db:
        if not os.path.isabs(args.session_db) and os.path.dirname(args.session_db) == '':
            args.session_db = os.path.join(args.results_dir, args.session_db)
    else:
        args.session_db = os.path.join(args.results_dir, 'session.sqlite')
    if args.cache_out:
        if not os.path.isabs(args.cache_out) and os.path.dirname(args.cache_out) == '':
            args.cache_out = os.path.join(args.results_dir, args.cache_out)
    else:
        args.cache_out = os.path.join(args.results_dir, 'combined_cache_session.db')
    if not args.orders_db:
        args.orders_db = args.session_db
    print(f'[results] dir: {args.results_dir}')

    bundle_run_id = time.strftime('WRAP_%Y%m%d_%H%M%S')
    ensure_live_debug_bundle_db(args.session_db)
    restore_stdio = install_stdio_capture(args.session_db, bundle_run_id)
    record_run_meta(args.session_db, bundle_run_id, argv=sys.argv, env={
        'mode': args.mode,
        'paper_source': args.paper_source,
        'exchange': args.exchange,
        'results_dir': args.results_dir,
        'cfg': args.cfg,
    }, extra={'wrapper': os.path.basename(__file__)})
    snapshot_files(args.session_db, bundle_run_id, [__file__, args.cfg])
    debug_event(args.session_db, bundle_run_id, 'wrapper_start', {'mode': args.mode, 'results_dir': args.results_dir})

    cfg_uni = cfg.get('universe', {}) or {}
    file_syms = _read_universe_file(args.universe_file or cfg.get('universe_file', ''))
    allow_cli = _split_csv(args.allow_symbols)
    deny_cli = _split_csv(args.deny_symbols)
    uni = _merge_universe(cfg_uni, file_syms, allow_cli, deny_cli)
    cfg['universe'] = uni
    _hint_restrict_symbols(cfg, list(uni.get('allow', []) or []))

    try:
        if args.mode == 'backtest':
            _run_backtest(args.cfg, args.limit_bars)
        elif args.mode == 'paper':
            if args.paper_source == 'api':
                _run_paper_api(cfg, args)
            else:
                raise SystemExit('paper --paper-source=db is not implemented in this wrapper version')
        else:
            _run_live(cfg, args)
    except KeyboardInterrupt:
        debug_event(args.session_db, bundle_run_id, 'wrapper_stop', {'reason': 'keyboard_interrupt'})
        finalize_bundle(args.session_db, bundle_run_id, status='stopped', results_dir=args.results_dir)
        restore_stdio()
        raise
    except Exception:
        err = exception_text()
        debug_event(args.session_db, bundle_run_id, 'wrapper_error', {'traceback': err}, level='ERROR')
        finalize_bundle(args.session_db, bundle_run_id, status='error', error_text=err, results_dir=args.results_dir)
        restore_stdio()
        raise


if __name__ == '__main__':
    main()
