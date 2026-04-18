from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path('/mnt/data/top-main/obw_platform').resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runners.common import CCXTFetcher  # noqa: E402
from runners.exchange_trace_layer import (  # noqa: E402
    ExchangeTraceProxy,
    ReplayExchange,
    ensure_exchange_trace_db,
    export_trace_to_json,
    load_trace_steps_from_db,
)
from runners.live_runner_dual_scenario_runner import run_scenario  # noqa: E402


class DummyExchange:
    id = 'dummy'
    name = 'Dummy'

    def ping(self, x):
        return {'pong': x}

    def boom(self):
        raise RuntimeError('boom')


def _make_npz(path: Path, symbol: str = 'COMP/USDT:USDT', bars: int = 10):
    ts0 = 1712880000
    ts = np.arange(ts0, ts0 + bars * 60, 60, dtype=np.int64)
    close = np.linspace(10.0, 10.5, bars).astype(np.float64)
    open_ = close.copy(); high = close * 1.001; low = close * 0.999; volume = np.full_like(close, 15000.0)
    np.savez_compressed(path, symbol=symbol, timestamp_s=ts, open=open_, high=high, low=low, close=close, volume=volume)
    return symbol


def test_exchange_trace_proxy_logs_success_and_error(tmp_path):
    db = tmp_path / 'trace.sqlite'
    ensure_exchange_trace_db(str(db))
    ex = ExchangeTraceProxy(DummyExchange(), str(db), source='unit', scenario_id='s1')
    assert ex.ping(7)['pong'] == 7
    with pytest.raises(RuntimeError):
        ex.boom()
    con = sqlite3.connect(db)
    rows = con.execute('select method,status,error_text from exchange_api_log order by seq').fetchall()
    con.close()
    assert rows[0] == ('ping', 'ok', '')
    assert rows[1][0] == 'boom' and rows[1][1] == 'error'
    assert 'boom' in rows[1][2]


def test_replay_exchange_replays_recorded_steps(tmp_path):
    db = tmp_path / 'trace.sqlite'
    ensure_exchange_trace_db(str(db))
    ex = ExchangeTraceProxy(DummyExchange(), str(db), source='unit', scenario_id='case1')
    ex.ping(11)
    with pytest.raises(RuntimeError):
        ex.boom()
    steps = load_trace_steps_from_db(str(db), scenario_id='case1')
    rx = ReplayExchange(steps=steps, strict=True)
    assert rx.ping(11) == {'pong': 11}
    with pytest.raises(RuntimeError):
        rx.boom()


def test_export_trace_to_json_roundtrip(tmp_path):
    db = tmp_path / 'trace.sqlite'
    ensure_exchange_trace_db(str(db))
    ex = ExchangeTraceProxy(DummyExchange(), str(db), source='unit', scenario_id='export1')
    ex.ping(3)
    out = tmp_path / 'scenario.json'
    export_trace_to_json(str(db), str(out), scenario_id='export1')
    payload = json.loads(out.read_text())
    assert payload['scenario_id'] == 'export1'
    assert payload['exchange_steps'][0]['method'] == 'ping'


def test_ccxtfetcher_replay_exchange_from_env(tmp_path, monkeypatch):
    npz = tmp_path / 'bars.npz'
    symbol = _make_npz(npz)
    trace_db = tmp_path / 'trace.sqlite'
    ensure_exchange_trace_db(str(trace_db))
    ex = ExchangeTraceProxy(DummyExchange(), str(trace_db), source='unit', scenario_id='fetcher1')
    ex.ping('abc')
    monkeypatch.setenv('VIRTUAL_EXCHANGE_NPZ', str(npz))
    monkeypatch.setenv('VIRTUAL_EXCHANGE_SYMBOLS', symbol)
    monkeypatch.setenv('REPLAY_EXCHANGE_SCENARIO_DB', str(trace_db))
    monkeypatch.setenv('REPLAY_EXCHANGE_SCENARIO_ID', 'fetcher1')
    monkeypatch.setenv('REPLAY_EXCHANGE_BACKEND', 'virtual')
    fetcher = CCXTFetcher(exchange='replay', symbol_format='usdtm', debug=False)
    assert fetcher.ex.ping('abc') == {'pong': 'abc'}
    ticker = fetcher.ex.fetch_ticker(symbol)
    assert float(ticker['last']) > 0


def test_scenario_runner_replays_live_runner_case(tmp_path, monkeypatch):
    npz = tmp_path / 'bars.npz'
    symbol = _make_npz(npz, bars=8)
    # build trace with create_order filled, fetch_order filled, then close filled, then fetch_order filled
    trace_db = tmp_path / 'trace.sqlite'
    ensure_exchange_trace_db(str(trace_db))
    con = sqlite3.connect(trace_db)
    cur = con.cursor()
    rows = [
        ('2026-01-01T00:00:00+00:00','unit','case2',1,'create_order','[]','{}','ok', json.dumps({'id':'ord1','orderId':'ord1','clientOrderId':'ord1','symbol':symbol,'type':'market','side':'buy','amount':1.0,'remaining':0.0,'filled':1.0,'average':10.0,'price':10.0,'status':'closed','timestamp':1712880000000,'datetime':'2024-04-12T00:00:00+00:00','reduceOnly':False,'info':{'positionSide':'LONG','reduceOnly':False}}), '',1.0,'Replay','replay'),
        ('2026-01-01T00:00:00+00:00','unit','case2',2,'fetch_order','[]','{}','ok', json.dumps({'id':'ord1','orderId':'ord1','clientOrderId':'ord1','symbol':symbol,'type':'market','side':'buy','amount':1.0,'remaining':0.0,'filled':1.0,'average':10.0,'price':10.0,'status':'closed','timestamp':1712880000000,'datetime':'2024-04-12T00:00:00+00:00','reduceOnly':False,'info':{'positionSide':'LONG','reduceOnly':False}}), '',1.0,'Replay','replay'),
        ('2026-01-01T00:01:00+00:00','unit','case2',3,'fetch_positions','[]','{}','ok', json.dumps([{'symbol':symbol,'side':'long','contracts':1.0,'entryPrice':10.0,'entry':10.0,'unrealizedPnl':0.0,'info':{'positionSide':'LONG','availableAmt':1.0,'contracts':1.0}}]), '',1.0,'Replay','replay'),
        ('2026-01-01T00:01:00+00:00','unit','case2',4,'create_order','[]','{}','ok', json.dumps({'id':'ord2','orderId':'ord2','clientOrderId':'ord2','symbol':symbol,'type':'market','side':'sell','amount':1.0,'remaining':0.0,'filled':1.0,'average':10.2,'price':10.2,'status':'closed','timestamp':1712880060000,'datetime':'2024-04-12T00:01:00+00:00','reduceOnly':True,'info':{'positionSide':'LONG','reduceOnly':True}}), '',1.0,'Replay','replay'),
        ('2026-01-01T00:01:00+00:00','unit','case2',5,'fetch_order','[]','{}','ok', json.dumps({'id':'ord2','orderId':'ord2','clientOrderId':'ord2','symbol':symbol,'type':'market','side':'sell','amount':1.0,'remaining':0.0,'filled':1.0,'average':10.2,'price':10.2,'status':'closed','timestamp':1712880060000,'datetime':'2024-04-12T00:01:00+00:00','reduceOnly':True,'info':{'positionSide':'LONG','reduceOnly':True}}), '',1.0,'Replay','replay'),
        ('2026-01-01T00:01:00+00:00','unit','case2',6,'fetch_positions','[]','{}','ok', json.dumps([]), '',1.0,'Replay','replay'),
    ]
    cur.executemany('INSERT INTO exchange_api_log(ts_utc,source,scenario_id,seq,method,args_json,kwargs_json,status,result_json,error_text,elapsed_ms,exchange_name,exchange_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    con.commit(); con.close()
    monkeypatch.setenv('VIRTUAL_EXCHANGE_NPZ', str(npz))
    monkeypatch.setenv('VIRTUAL_EXCHANGE_SYMBOLS', symbol)
    cfg = {'timeframe': '1m'}
    scenario = {
        'symbol': symbol,
        'bars': 3,
        'backend': 'virtual',
        'exchange_trace_db': str(trace_db),
        'exchange_trace_scenario_id': 'case2',
        'strategy_steps': [
            {'bar_index': 0, 'kind': 'entry', 'symbol': symbol, 'side': 'LONG', 'qty': 1.0, 'reason': 'entry0'},
            {'bar_index': 1, 'kind': 'manage', 'symbol': symbol, 'side': 'LONG', 'action': 'TP', 'reason': 'tp_hit'},
        ],
    }
    summary = run_scenario(cfg, scenario, results_dir=str(tmp_path / 'results'))
    assert summary['opened'] == 1
    assert summary['closed'] == 1
    assert summary['orders'] >= 2
    assert summary['api_calls'] >= 6
