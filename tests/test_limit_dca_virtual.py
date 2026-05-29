import os
import tempfile
from types import SimpleNamespace

from obw_platform.runners.virtual_exchange import VirtualExchange
from obw_platform.runners import live_runner_dual as lr
from obw_platform.runners.common import ensure_orders_db, ensure_session_dbs
from tests.test_maker_dca_flow import write_npz


class DummyFetcher:
    def __init__(self, ex):
        self.ex = ex
        self.markets = ex.load_markets()
        self.by_base = {sym.split('/')[0]: sym for sym in self.markets}

    def resolve_symbol(self, s):
        return s

    def fetch_ticker_price(self, s):
        return float(self.ex.fetch_ticker(s)['last'])


class DummyState:
    def __init__(self):
        self.avg_price = 100.0
        self.num_buys = 1
        self.pos_size = 1.0
        self.next_level_price = 95.0
        self.reset_pending = False
        self.trailing_active = False


class DummyStrat:
    def __init__(self):
        self._states = {'ENA/USDT:USDT': DummyState()}

    def _get_state(self, sym):
        return self._states[sym]

    def _entry_tp_sl(self, entry):
        return entry * 1.01, None

    def manage_position(self, sym, row, pos, ctx=None):
        st = self._states[sym]
        # trigger one DCA when close is low enough and current qty is 1
        if float(pos.qty) <= 1.0 + 1e-12 and float(row['close']) <= 95.0:
            pos.qty = 2.0
            pos.entry = 97.5
            st.pos_size = 2.0
            st.num_buys = 2
            st.next_level_price = 90.0
        return None

    def sync_after_external_fill(self, sym, qty, entry, fill_price, delta_qty, event='dca'):
        st = self._states[sym]
        st.pos_size = qty
        st.avg_price = entry


def _bars_for_limit_retry():
    return {
        'ENA/USDT:USDT': [
            {'datetime_utc': '2026-01-01T00:00:00+00:00', 'open': 100.0, 'high': 101.0, 'low': 96.0, 'close': 94.0, 'volume': 1000.0, 'quote_volume': 94000.0},
            {'datetime_utc': '2026-01-01T00:00:30+00:00', 'open': 94.0, 'high': 97.0, 'low': 94.0, 'close': 95.0, 'volume': 1200.0, 'quote_volume': 114000.0},
        ]
    }


def test_virtual_exchange_limit_order_fills_on_touch_next_bar():
    bars = {
        'ENA/USDT:USDT': [
            {'datetime_utc': '2026-01-01T00:00:00+00:00', 'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0, 'volume': 1000.0, 'quote_volume': 100000.0},
            {'datetime_utc': '2026-01-01T00:00:30+00:00', 'open': 100.0, 'high': 100.0, 'low': 95.0, 'close': 96.0, 'volume': 1000.0, 'quote_volume': 96000.0},
        ]
    }
    with tempfile.TemporaryDirectory() as td:
        npz = os.path.join(td, 'bars.npz')
        write_npz(npz, bars)
        ex = VirtualExchange(npz_path=npz, initial_balance=1000.0, seed=1)
        od = ex.create_order('ENA/USDT:USDT', 'limit', 'buy', 1.0, 97.0, {'positionSide': 'LONG'})
        assert od['status'] == 'open'
        ex.advance(1)
        od2 = ex.fetch_order(od['id'], 'ENA/USDT:USDT')
        assert od2['status'] == 'closed'
        assert abs(float(od2['average']) - 97.0) < 1e-9


def test_runner_dca_limit_cancel_and_retry_on_next_bar():
    with tempfile.TemporaryDirectory() as td:
        npz = os.path.join(td, 'bars.npz')
        write_npz(npz, _bars_for_limit_retry())
        ex = VirtualExchange(npz_path=npz, initial_balance=1000.0, seed=2)
        fetcher = DummyFetcher(ex)
        strat = DummyStrat()
        cfg = {'legacy_strategy_adapter': {'enabled': True, 'legacy_dca_order_type': 'limit'}}
        pending = {}
        positions = {
            'ENA/USDT:USDT|LONG': {
                'symbol': 'ENA/USDT:USDT',
                'side': 'LONG',
                'qty': 1.0,
                'entry': 100.0,
                'ts_open': '2026-01-01T00:00:00+00:00',
                'run_id': 'R1',
                'order_id': 'local1',
            }
        }
        sess, _ = ensure_session_dbs(td)
        ensure_orders_db(sess)
        row0 = ex.current_bar('ENA/USDT:USDT')
        ok = lr._maybe_apply_manage_result(fetcher, 'ENA/USDT:USDT|LONG', positions['ENA/USDT:USDT|LONG'], row0, strat, positions, td, 'hedge', sess, 'bot1', cfg=cfg, pending_entries=pending, run_id='R1')
        assert ok is False
        assert 'ENA/USDT:USDT|LONG' in pending
        # state must be restored until actual fill
        assert abs(strat._get_state('ENA/USDT:USDT').pos_size - 1.0) < 1e-12
        # move to next bar: previous pending fills because the bar touches the limit
        ex.advance(1)
        row1 = ex.current_bar('ENA/USDT:USDT')
        lr._sync_pending_entry_orders(fetcher, pending, positions, td, sess, 'bot1', strat, strat, row1['datetime_utc'])
        assert 'ENA/USDT:USDT|LONG' not in pending
        assert abs(positions['ENA/USDT:USDT|LONG']['qty'] - 2.0) < 1e-12
        assert abs(strat._get_state('ENA/USDT:USDT').pos_size - 2.0) < 1e-12
