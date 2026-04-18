from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

try:
    from .common import CCXTFetcher, ensure_orders_db, ensure_session_dbs, load_yaml_or_json, make_bot_id
    from .live_runner_dual import _attempt_entry, _maybe_apply_manage_result, pos_key
except Exception:
    ROOT = Path(__file__).resolve().parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from common import CCXTFetcher, ensure_orders_db, ensure_session_dbs, load_yaml_or_json, make_bot_id
    from live_runner_dual import _attempt_entry, _maybe_apply_manage_result, pos_key


@dataclass
class _SigSpec:
    kind: str
    side: str
    qty: Optional[float] = None
    qty_frac: Optional[float] = None
    reason: str = ''
    tp: Optional[float] = None
    sl: Optional[float] = None
    delta_qty: Optional[float] = None


class ScriptedDualStrategy:
    def __init__(self, steps: List[Dict[str, Any]]):
        self._states: Dict[str, Any] = {}
        self._steps = list(steps)
        self.current_bar_index = 0
        self.current_side = 'LONG'
        self.rejections: List[Tuple[str, str, Any]] = []
        self.syncs: List[Tuple[str, str, float, float]] = []

    def _state(self, sym: str):
        if sym not in self._states:
            self._states[sym] = SimpleNamespace(pos_size=0.0, avg_price=None, lots=[])
        return self._states[sym]

    def export_state_snapshot(self, sym: str):
        import copy
        return copy.deepcopy(self._states.get(sym))

    def restore_state_snapshot(self, sym: str, snapshot):
        import copy
        if snapshot is None:
            self._states.pop(sym, None)
        else:
            self._states[sym] = copy.deepcopy(snapshot)

    def on_order_rejected(self, sym: str, event: str = '', details=None):
        self.rejections.append((sym, event, details))

    def sync_after_external_fill(self, sym: str, *, qty: float, entry: float, fill_price=None, delta_qty=None, event: str = ''):
        st = self._state(sym)
        st.pos_size = float(qty)
        st.avg_price = float(entry) if qty else None
        self.syncs.append((sym, event, float(qty), float(entry) if qty else 0.0))

    def _find_step(self, kind: str, sym: str, side: str):
        for step in self._steps:
            if int(step.get('bar_index', -1)) != int(self.current_bar_index):
                continue
            if str(step.get('kind', '')).lower() != kind:
                continue
            if str(step.get('symbol')) != str(sym):
                continue
            if str(step.get('side', '')).upper() != str(side).upper():
                continue
            return step
        return None

    def entry_signal(self, is_opening, sym, row, ctx=None):
        side = self.current_side
        step = self._find_step('entry', sym, side)
        if not step:
            return None
        qty = step.get('qty')
        st = self._state(sym)
        if qty:
            st.pos_size = float(qty)
            st.avg_price = float(row['close'])
        return SimpleNamespace(side=side, qty=qty, tp=step.get('tp'), sl=step.get('sl'), reason=step.get('reason') or 'scenario_entry')

    def manage_position(self, sym, row, pos, ctx=None):
        side = self.current_side
        step = self._find_step('manage', sym, side)
        if not step:
            return None
        action = str(step.get('action', '')).upper()
        if action == 'DCA':
            delta = float(step.get('delta_qty') or 0.0)
            if delta <= 0:
                return None
            old_qty = float(pos.qty)
            pos.qty = old_qty + delta
            pos.entry = (float(pos.entry) * old_qty + float(row['close']) * delta) / max(float(pos.qty), 1e-12)
            st = self._state(sym)
            st.pos_size = float(pos.qty)
            st.avg_price = float(pos.entry)
            return None
        if action == 'TP_PARTIAL':
            return SimpleNamespace(action='TP_PARTIAL', qty_frac=float(step.get('qty_frac') or 0.0), reason=step.get('reason') or 'scenario_partial')
        if action in {'TP', 'SL', 'EXIT'}:
            return SimpleNamespace(action=action, reason=step.get('reason') or action.lower())
        return None


def run_scenario(cfg: Dict[str, Any], scenario: Dict[str, Any], *, results_dir: str) -> Dict[str, Any]:
    os.environ['REPLAY_EXCHANGE_SCENARIO_JSON'] = str(scenario.get('exchange_trace_json') or '')
    os.environ['REPLAY_EXCHANGE_SCENARIO_DB'] = str(scenario.get('exchange_trace_db') or '')
    os.environ['REPLAY_EXCHANGE_SCENARIO_ID'] = str(scenario.get('exchange_trace_scenario_id') or '')
    os.environ['REPLAY_EXCHANGE_BACKEND'] = str(scenario.get('backend', 'virtual') or 'virtual')
    results = Path(results_dir)
    results.mkdir(parents=True, exist_ok=True)
    session_db, _cache_db = ensure_session_dbs(str(results))
    ensure_orders_db(session_db)
    os.environ['EXCHANGE_TRACE_DB'] = session_db
    os.environ['EXCHANGE_TRACE_SCENARIO_ID'] = 'scenario-run'
    fetcher = CCXTFetcher(exchange='replay', symbol_format='usdtm', debug=bool(scenario.get('debug', False)))
    strategy = ScriptedDualStrategy(list(scenario.get('strategy_steps') or []))
    positions: Dict[str, Dict[str, Any]] = {}
    symbol = str(scenario['symbol'])
    bars = int(scenario.get('bars') or 0)
    if bars <= 0:
        bars = len(fetcher.ex.backend.data[symbol]) if hasattr(fetcher.ex, 'backend') and hasattr(fetcher.ex.backend, 'data') else 0
    bot_id = make_bot_id(str(results), 'replay', cfg.get('timeframe', '1m'))
    opened = closed = 0
    for idx in range(bars):
        if hasattr(fetcher.ex, 'set_cursor'):
            fetcher.ex.set_cursor(symbol, idx)
        bar = fetcher.ex.current_bar(symbol)
        row = {
            'datetime_utc': __import__('pandas').to_datetime(bar['timestamp'], unit='ms', utc=True).isoformat(),
            'open': bar['open'], 'high': bar['high'], 'low': bar['low'], 'close': bar['close'], 'volume': bar['volume'],
        }
        strategy.current_bar_index = idx
        for side in ('LONG', 'SHORT'):
            strategy.current_side = side
            if pos_key(symbol, side) in positions:
                before = len(positions)
                _maybe_apply_manage_result(fetcher, pos_key(symbol, side), positions[pos_key(symbol, side)], row, strategy, positions, str(results), 'hedge', session_db, bot_id, 'scenario-run')
                if len(positions) < before:
                    closed += 1
            else:
                if _attempt_entry(fetcher, symbol, side, strategy, row, positions, str(results), 'hedge', session_db, bot_id, 'scenario-run', notional_long=10.0, notional_short=10.0):
                    opened += 1
    con = sqlite3.connect(session_db)
    api_calls = con.execute('SELECT count(*) FROM exchange_api_log').fetchone()[0] if _table_exists(con, 'exchange_api_log') else 0
    orders = con.execute('SELECT count(*) FROM orders').fetchone()[0]
    con.close()
    return {
        'opened': opened,
        'closed': closed,
        'positions_left': len(positions),
        'api_calls': int(api_calls),
        'orders': int(orders),
        'rejections': len(strategy.rejections),
        'syncs': len(strategy.syncs),
        'session_db': session_db,
    }


def _table_exists(con, table: str) -> bool:
    row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(row)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description='Run live_runner_dual against a scripted strategy and replay exchange scenario')
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--scenario', required=True)
    ap.add_argument('--results-dir', required=True)
    args = ap.parse_args()
    cfg = load_yaml_or_json(args.cfg)
    with open(args.scenario, 'r', encoding='utf-8') as f:
        scenario = json.load(f)
    summary = run_scenario(cfg, scenario, results_dir=args.results_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
