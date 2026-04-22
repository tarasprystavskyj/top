from __future__ import annotations
import sqlite3, tempfile
from pathlib import Path
import numpy as np
from obw_platform.runners.virtual_exchange import VirtualExchange
from obw_platform.runners import live_runner_dual as lr
from obw_platform.runners.common import ensure_session_dbs
from obw_platform.backtester_dual_core_dynamic_v5 import simulate, pick_symbol_block
from tests import dummy_maker_strategy as dms

def write_npz(path: Path, bars: dict[str, list[dict]]):
    symbols=[]; offsets=[0]; ts=[]; open_=[]; high=[]; low=[]; close=[]; volume=[]; quote_volume=[]; lff=[]; lffb=[]; lffs=[]
    for sym, rows in bars.items():
        symbols.append(sym)
        for r in rows:
            t=int(np.datetime64(r['datetime_utc']).astype('datetime64[s]').astype(int))
            ts.append(t); open_.append(float(r['open'])); high.append(float(r['high'])); low.append(float(r['low'])); close.append(float(r['close']))
            volume.append(float(r.get('volume',0.0))); qv=float(r.get('quote_volume', float(r.get('volume',0.0))*float(r['close']))); quote_volume.append(qv)
            lff.append(float(r.get('limit_fill_fraction',1.0))); lffb.append(float(r.get('limit_fill_fraction_buy', r.get('limit_fill_fraction',1.0)))); lffs.append(float(r.get('limit_fill_fraction_sell', r.get('limit_fill_fraction',1.0))))
        offsets.append(len(ts))
    np.savez_compressed(path, symbols=np.asarray(symbols, dtype=object), offsets=np.asarray(offsets, dtype=np.int64), timestamp_s=np.asarray(ts, dtype=np.int64), open=np.asarray(open_, dtype=np.float64), high=np.asarray(high, dtype=np.float64), low=np.asarray(low, dtype=np.float64), close=np.asarray(close, dtype=np.float64), volume=np.asarray(volume, dtype=np.float64), quote_volume=np.asarray(quote_volume, dtype=np.float64), limit_fill_fraction=np.asarray(lff, dtype=np.float64), limit_fill_fraction_buy=np.asarray(lffb, dtype=np.float64), limit_fill_fraction_sell=np.asarray(lffs, dtype=np.float64))

class DummyFetcher:
    def __init__(self, ex): self.ex=ex; self.markets=ex.load_markets(); self.by_base={sym.split('/')[0]:sym for sym in self.markets}
    def resolve_symbol(self, s): return self.ex.resolve_symbol(s) or s
    def fetch_ticker_price(self, s): return float(self.ex.fetch_ticker(self.resolve_symbol(s))['last'])
    def fetch_mark_price(self, s): return self.fetch_ticker_price(s)

def _make_live_strat(next_level_price=95.0):
    class S:
        def __init__(self): self.sym='ENA/USDT:USDT'; self._states={self.sym:dms.State(avg_price=100.0, num_buys=1, pos_size=1.0, next_level_price=next_level_price)}
        def _get_state(self, sym): return self._states[sym]
        def export_state_snapshot(self, sym): st=self._states[sym]; return {'avg_price':st.avg_price,'num_buys':st.num_buys,'pos_size':st.pos_size,'next_level_price':st.next_level_price,'reset_pending':st.reset_pending,'trailing_active':st.trailing_active}
        def restore_state_snapshot(self, sym, snap): self._states[sym]=dms.State(avg_price=float(snap.get('avg_price') or 0.0), num_buys=int(snap.get('num_buys') or 0), pos_size=float(snap.get('pos_size') or 0.0), next_level_price=float(snap.get('next_level_price', next_level_price)))
        def manage_position(self, sym, row, pos, ctx=None):
            if float(pos.qty)<=1.0+1e-12 and float(row['close'])<=96.0:
                pos.qty=2.0; pos.entry=float(row['close']); self._states[sym].pos_size=2.0; self._states[sym].num_buys=2
            return None
        def sync_after_external_fill(self, sym, qty, entry, fill_price=None, delta_qty=None, event=''):
            st=self._states[sym]; st.pos_size=float(qty); st.avg_price=float(entry)
        def on_order_rejected(self, sym, event='', details=None): pass
    return S()

def test_backtester_maker_dca_retries_until_touch():
    dms.EVENTS.clear()
    bars={'ENA/USDT:USDT':[{'datetime_utc':'2026-01-01T00:00:00+00:00','open':100.0,'high':101.0,'low':99.0,'close':100.0,'volume':1000.0},{'datetime_utc':'2026-01-01T00:00:30+00:00','open':100.0,'high':98.0,'low':95.5,'close':96.0,'volume':1000.0},{'datetime_utc':'2026-01-01T00:01:00+00:00','open':96.0,'high':97.0,'low':94.9,'close':95.8,'volume':1000.0}]}
    with tempfile.TemporaryDirectory() as td:
        npz=Path(td)/'bars.npz'; write_npz(npz,bars); data=np.load(npz, allow_pickle=True)
        cfg={'strategy_class_long':'tests.dummy_maker_strategy.MakerLongStrategy','strategy_class_short':'tests.dummy_maker_strategy.FlatShortStrategy','portfolio':{'initial_equity_per_leg':100.0,'fee_rate':0.0,'max_notional_frac':1.0,'dynamic_slippage_model':{'kind':'constant','base_bp':0.0}},'dca_open_order_type':'limit','test_symbol':'ENA/USDT:USDT','test_next_level_price':95.0}
        market_symbol, ts_s, o, h, l, c, v, extras = pick_symbol_block(data, 'ENA/USDT:USDT')
        out = simulate(cfg, ts_s, c, open_=o, high=h, low=l, volume=v, extras=extras, market_symbol=market_symbol, export_curves=True)
        dca_events=[e for e in dms.EVENTS if e['event'] in {'dca_limit','dca'}]
        assert len(dca_events)==1 and abs(float(dca_events[0]['fill_price'])-95.0)<1e-9
        assert out['curves']['long_notional'].max() > out['curves']['long_notional'].iloc[0]

def test_live_runner_limit_dca_cancel_retry_and_fill():
    bars={'ENA/USDT:USDT':[{'datetime_utc':'2026-01-01T00:00:00+00:00','open':100.0,'high':100.0,'low':95.5,'close':96.0,'volume':1000.0},{'datetime_utc':'2026-01-01T00:00:30+00:00','open':96.0,'high':97.0,'low':94.8,'close':95.5,'volume':1000.0}]}
    with tempfile.TemporaryDirectory() as td:
        npz=Path(td)/'bars.npz'; write_npz(npz,bars)
        ex=VirtualExchange(npz_path=str(npz), mode='hedge', initial_balance=1000.0, order_ttl_bars=10, seed=2)
        fetcher=DummyFetcher(ex); strat=_make_live_strat(95.0); cfg={'dca_open_order_type':'limit'}; pending={}; positions={'ENA/USDT:USDT|LONG':{'symbol':'ENA/USDT:USDT','side':'LONG','qty':1.0,'entry':100.0,'ts_open':'2026-01-01T00:00:00+00:00','run_id':'R1','order_id':'local1'}}
        sess,_=ensure_session_dbs(td)
        row0=ex.current_bar('ENA/USDT:USDT'); ok0=lr._maybe_apply_manage_result(fetcher,'ENA/USDT:USDT|LONG',positions['ENA/USDT:USDT|LONG'],row0,strat,positions,td,'hedge',sess,'bot1','R1',cfg=cfg,pending_entries=pending)
        assert ok0 is False and 'ENA/USDT:USDT|LONG' in pending
        ex.advance(1); row1=ex.current_bar('ENA/USDT:USDT'); lr._sync_pending_entry_orders(fetcher,pending,positions,td,sess,'bot1',strat,strat,row1['datetime_utc'],run_id='R1')
        assert 'ENA/USDT:USDT|LONG' not in pending
        ok1=lr._maybe_apply_manage_result(fetcher,'ENA/USDT:USDT|LONG',positions['ENA/USDT:USDT|LONG'],row1,strat,positions,td,'hedge',sess,'bot1','R1',cfg=cfg,pending_entries=pending)
        assert ok1 is True and abs(float(positions['ENA/USDT:USDT|LONG']['qty'])-2.0)<1e-12
        assert any(o['status']=='canceled' for o in ex.orders.values())

def test_live_runner_limit_dca_partial_fill_then_cancel_remainder():
    bars={'ENA/USDT:USDT':[{'datetime_utc':'2026-01-01T00:00:00+00:00','open':100.0,'high':100.0,'low':95.0,'close':96.0,'volume':1000.0,'limit_fill_fraction_buy':0.5},{'datetime_utc':'2026-01-01T00:00:30+00:00','open':96.0,'high':96.5,'low':95.2,'close':95.8,'volume':1000.0}]}
    with tempfile.TemporaryDirectory() as td:
        npz=Path(td)/'bars.npz'; write_npz(npz,bars)
        ex=VirtualExchange(npz_path=str(npz), mode='hedge', initial_balance=1000.0, order_ttl_bars=10, seed=3)
        fetcher=DummyFetcher(ex); strat=_make_live_strat(95.0); cfg={'dca_open_order_type':'limit'}; pending={}; positions={'ENA/USDT:USDT|LONG':{'symbol':'ENA/USDT:USDT','side':'LONG','qty':1.0,'entry':100.0,'ts_open':'2026-01-01T00:00:00+00:00','run_id':'R1','order_id':'local1'}}
        sess,_=ensure_session_dbs(td)
        row0=ex.current_bar('ENA/USDT:USDT'); ok0=lr._maybe_apply_manage_result(fetcher,'ENA/USDT:USDT|LONG',positions['ENA/USDT:USDT|LONG'],row0,strat,positions,td,'hedge',sess,'bot1','R1',cfg=cfg,pending_entries=pending)
        assert ok0 is False and abs(float(positions['ENA/USDT:USDT|LONG']['qty'])-1.5)<1e-12
        ex.advance(1); row1=ex.current_bar('ENA/USDT:USDT'); lr._sync_pending_entry_orders(fetcher,pending,positions,td,sess,'bot1',strat,strat,row1['datetime_utc'],run_id='R1')
        assert pending == {} and abs(float(positions['ENA/USDT:USDT|LONG']['qty'])-1.5)<1e-12

def test_session_sqlite_real_scenario_extracts_open_close_open_sequence():
    con=sqlite3.connect('/mnt/data/session(14).sqlite')
    rows=con.execute("select ts_open, side, status, ts_close, close_reason from open_positions where run_id='LIVE_DUAL_20260419_064128' order by ts_open asc").fetchall(); con.close()
    sides=[r[1].upper() for r in rows]
    assert 'LONG' in sides and 'SHORT' in sides
    closed_short=[r for r in rows if str(r[1]).upper()=='SHORT' and str(r[2]).upper()=='CLOSED']
    assert closed_short and 'TRAILING' in str(closed_short[0][4]).upper()
