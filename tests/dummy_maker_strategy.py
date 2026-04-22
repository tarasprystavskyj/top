from __future__ import annotations
from types import SimpleNamespace
EVENTS = []
class State:
    def __init__(self, *, avg_price=0.0, num_buys=0, pos_size=0.0, next_level_price=95.0):
        self.avg_price = avg_price
        self.num_buys = num_buys
        self.pos_size = pos_size
        self.next_level_price = next_level_price
        self.reset_pending = False
        self.trailing_active = False

class _Base:
    def __init__(self, cfg):
        self.sym = cfg.get('test_symbol', 'ENA/USDT:USDT')
        self._states = {self.sym: State(next_level_price=float(cfg.get('test_next_level_price', 95.0)))}
        self._opened = False
    def _get_state(self, sym): return self._states[sym]
    def export_state_snapshot(self, sym):
        st = self._states[sym]
        return {'avg_price': st.avg_price, 'num_buys': st.num_buys, 'pos_size': st.pos_size, 'next_level_price': st.next_level_price, 'reset_pending': st.reset_pending, 'trailing_active': st.trailing_active}
    def restore_state_snapshot(self, sym, snap):
        if snap is None:
            self._states.pop(sym, None); return
        st = State(next_level_price=float(snap.get('next_level_price', 95.0)))
        st.avg_price = float(snap.get('avg_price') or 0.0)
        st.num_buys = int(snap.get('num_buys') or 0)
        st.pos_size = float(snap.get('pos_size') or 0.0)
        st.reset_pending = bool(snap.get('reset_pending', False))
        st.trailing_active = bool(snap.get('trailing_active', False))
        self._states[sym] = st
    def _entry_tp_sl(self, entry): return entry * 1.01, None
    def sync_after_external_fill(self, sym, qty, entry, fill_price=None, delta_qty=None, event=''):
        st = self._states[sym]
        st.pos_size = float(qty)
        st.avg_price = float(entry)
        if delta_qty and delta_qty > 0: st.num_buys += 1
        if qty <= 0: st.num_buys = 0
        EVENTS.append({'sym': sym, 'event': event, 'qty': float(qty), 'entry': float(entry), 'fill_price': None if fill_price is None else float(fill_price), 'delta_qty': None if delta_qty is None else float(delta_qty)})

class MakerLongStrategy(_Base):
    def entry_signal(self, can_open, sym, row, ctx=None):
        if can_open and self._states[sym].pos_size <= 1e-12 and not self._opened:
            self._opened = True
            return SimpleNamespace(qty=1.0)
        return None
    def manage_position(self, sym, row, pos, ctx=None):
        st = self._states[sym]
        if float(pos.qty) <= 1.0 + 1e-12 and float(row['close']) <= 96.0:
            pos.qty = 2.0
            pos.entry = float(row['close'])
            st.pos_size = 2.0
            st.num_buys = 2
        return None

class FlatShortStrategy(_Base):
    def entry_signal(self, can_open, sym, row, ctx=None): return None
    def manage_position(self, sym, row, pos, ctx=None): return None
