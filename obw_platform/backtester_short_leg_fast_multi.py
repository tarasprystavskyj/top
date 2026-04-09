#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rank all symbols in a multi-symbol fast NPZ using only the short leg logic."""
from __future__ import annotations
import argparse, importlib, math, yaml, json
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

@dataclass
class ShortState:
    first_usdt: float
    tp_percent: float
    callback_percent: float
    margin_call_limit: int
    linear_rise_percent: float
    auto_merge: bool
    subsell_tp_percent: float
    rise1: float
    rise2: float
    rise3: float
    rise4: float
    rise5: float
    mult2: float
    mult3: float
    mult4: float
    mult5: float
    max_fills_per_bar: int
    max_subsells_per_bar: int
    max_budget_frac: float
    initial_capital: float
    use_even_bars: bool
    timeframe: str
    pos_qty: float = 0.0
    pos_entry: float = 0.0
    pos_proceeds_usdt: float = 0.0
    avg_price: float | None = None
    num_fills: int = 0
    last_fill_price: float | None = None
    next_level_price: float | None = None
    lots_qty: list = field(default_factory=list)
    lots_price: list = field(default_factory=list)
    trailing_active: bool = False
    trailing_min: float | None = None
    reset_pending: bool = False
    pending_new_entry: float | None = None

    def tf_seconds(self):
        s = self.timeframe.lower().strip()
        if s.endswith('m'): return max(1, int(round(float(s[:-1]) * 60.0)))
        if s.endswith('h'): return max(1, int(round(float(s[:-1]) * 3600.0)))
        return 60

    def allow_bar(self, ts_s: int):
        if not self.use_even_bars:
            return True
        tf = self.tf_seconds()
        bars_from_anchor = int(ts_s // tf)
        return (bars_from_anchor % 2) == 0

    def get_step(self):
        nf = self.num_fills + 1
        return self.rise1 if nf == 2 else self.rise2 if nf == 3 else self.rise3 if nf == 4 else self.rise4 if nf == 5 else self.rise5 if nf == 6 else self.linear_rise_percent

    def get_mult(self):
        nf = self.num_fills + 1
        return self.mult2 if nf == 2 else self.mult3 if nf == 3 else self.mult4 if nf == 4 else self.mult5 if nf == 5 else 1.0

    def next_level(self, last_fill_price: float):
        return last_fill_price * (1.0 + self.get_step() / 100.0)

    def maybe_open_first(self, price: float):
        qty0_state = self.first_usdt / max(price, 1e-12)
        self.pos_proceeds_usdt = self.first_usdt
        self.pos_qty = qty0_state
        self.pos_entry = price
        self.avg_price = price
        self.num_fills = 1
        self.last_fill_price = price
        self.next_level_price = self.next_level(price)
        self.lots_qty = [qty0_state]
        self.lots_price = [price]
        self.trailing_active = False
        self.trailing_min = None
        self.reset_pending = False
        self.pending_new_entry = None

    def gross_notional(self):
        return self.pos_entry * self.pos_qty if self.pos_qty > 0 else 0.0

    def unrealized(self, price: float, fee: float, slippage: float):
        if self.pos_qty <= 0: return 0.0
        gross_ret = (self.pos_entry - price) / max(self.pos_entry, 1e-12)
        net_ret = gross_ret - 2*slippage - 2*fee
        return net_ret * (self.pos_entry * self.pos_qty)

    def step(self, price: float, fee: float, slippage: float):
        res = {'realized_pnl': 0.0, 'trades': 0, 'wins': 0}
        if self.pos_qty <= 0:
            return res
        if self.pending_new_entry is not None:
            self.pos_entry = self.pending_new_entry
            self.avg_price = self.pending_new_entry
            self.pending_new_entry = None
        if self.avg_price is None:
            self.avg_price = self.pos_entry
        max_budget = self.initial_capital * self.max_budget_frac
        tp_price = self.avg_price * (1.0 - self.tp_percent / 100.0)
        tp_hit = price <= tp_price
        if tp_hit:
            if self.callback_percent > 0:
                self.trailing_active = True
                self.trailing_min = price if self.trailing_min is None else min(self.trailing_min, price)
                trail_stop = self.trailing_min * (1.0 + self.callback_percent / 100.0)
                if price >= trail_stop:
                    notional = self.pos_entry * self.pos_qty
                    gross_ret = (self.pos_entry - price) / max(self.pos_entry, 1e-12)
                    pnl = (gross_ret - 2*slippage - 2*fee) * notional
                    res['realized_pnl'] += pnl; res['trades'] += 1; res['wins'] += 1 if pnl > 0 else 0
                    self.pos_qty = 0.0; self.pos_entry = 0.0; self.pos_proceeds_usdt = 0.0; self.avg_price = None
                    self.num_fills = 0; self.last_fill_price = None; self.next_level_price = None
                    self.lots_qty.clear(); self.lots_price.clear(); self.trailing_active = False; self.trailing_min = None
                    self.reset_pending = True
                    return res
            else:
                notional = self.pos_entry * self.pos_qty
                gross_ret = (self.pos_entry - price) / max(self.pos_entry, 1e-12)
                pnl = (gross_ret - 2*slippage - 2*fee) * notional
                res['realized_pnl'] += pnl; res['trades'] += 1; res['wins'] += 1 if pnl > 0 else 0
                self.pos_qty = 0.0; self.pos_entry = 0.0; self.pos_proceeds_usdt = 0.0; self.avg_price = None
                self.num_fills = 0; self.last_fill_price = None; self.next_level_price = None
                self.lots_qty.clear(); self.lots_price.clear(); self.trailing_active = False; self.trailing_min = None
                self.reset_pending = True
                return res
        else:
            self.trailing_active = False; self.trailing_min = None

        fills = 0
        while self.num_fills < self.margin_call_limit and fills < self.max_fills_per_bar and self.next_level_price is not None and price >= self.next_level_price:
            add_usdt = self.first_usdt * self.get_mult()
            if self.max_budget_frac < 0.999999 and (self.pos_proceeds_usdt + add_usdt) > max_budget:
                break
            fill_price = price
            qty_add = add_usdt / max(fill_price, 1e-12)
            new_proceeds = self.pos_entry * self.pos_qty + add_usdt
            new_qty = self.pos_qty + qty_add
            self.pos_entry = new_proceeds / max(new_qty, 1e-12)
            self.pos_qty = new_qty
            self.pos_proceeds_usdt += add_usdt
            self.avg_price = self.pos_entry
            self.lots_qty.append(qty_add)
            self.lots_price.append(fill_price)
            self.num_fills += 1
            self.last_fill_price = fill_price
            self.next_level_price = self.next_level(fill_price)
            fills += 1

        subs = 0
        while self.num_fills > 5 and self.lots_qty and subs < self.max_subsells_per_bar:
            qty_last = self.lots_qty[-1]; entry_last = self.lots_price[-1]
            last_tp = entry_last * (1.0 - self.subsell_tp_percent / 100.0)
            if price > last_tp:
                break
            qty_total = self.pos_qty; qty_close = min(qty_last, qty_total)
            if qty_close <= 0:
                break
            notional_entry = entry_last * qty_close
            gross_ret = (entry_last - price) / max(entry_last, 1e-12)
            pnl = (gross_ret - 2*slippage - 2*fee) * notional_entry
            res['realized_pnl'] += pnl; res['trades'] += 1; res['wins'] += 1 if pnl > 0 else 0
            total_cost = sum(q*p for q,p in zip(self.lots_qty, self.lots_price))
            profit = qty_close * (entry_last - price)
            remaining_qty = qty_total - qty_close
            remaining_cost = total_cost - qty_close * entry_last
            if self.auto_merge and remaining_qty > 0:
                remaining_cost -= profit
            if remaining_qty > 0:
                self.pending_new_entry = remaining_cost / max(remaining_qty, 1e-12)
                self.avg_price = self.pending_new_entry
            self.pos_entry = entry_last
            self.lots_qty.pop(); self.lots_price.pop(); self.num_fills = max(self.num_fills - 1, 0)
            self.pos_qty = max(0.0, qty_total - qty_close)
            if self.lots_qty:
                self.last_fill_price = self.lots_price[-1]
                self.next_level_price = self.next_level(self.last_fill_price)
            else:
                self.last_fill_price = None; self.next_level_price = None
            subs += 1
            break
        return res


def build_state(cfg):
    sp = cfg.get('strategy_params_short', {}) or {}
    pf = cfg.get('portfolio', {}) or {}
    return ShortState(
        first_usdt=float(sp.get('firstSellUSDT', 5.0)),
        tp_percent=float(sp.get('tpPercent', 1.1)),
        callback_percent=float(sp.get('callbackPercent', 0.2)),
        margin_call_limit=int(sp.get('marginCallLimit', 244)),
        linear_rise_percent=float(sp.get('linearRisePercent', 0.5)),
        auto_merge=bool(sp.get('autoMerge', True)),
        subsell_tp_percent=float(sp.get('subSellTPPercent', 1.3)),
        rise1=float(sp.get('rise1', 0.3)), rise2=float(sp.get('rise2', 0.4)), rise3=float(sp.get('rise3', 0.6)), rise4=float(sp.get('rise4', 0.8)), rise5=float(sp.get('rise5', 0.8)),
        mult2=float(sp.get('mult2', 1.5)), mult3=float(sp.get('mult3', 1.0)), mult4=float(sp.get('mult4', 2.0)), mult5=float(sp.get('mult5', 3.5)),
        max_fills_per_bar=int(sp.get('maxFillsPerBar', 6)),
        max_subsells_per_bar=int(sp.get('maxSubSellsPerBar', 10)),
        max_budget_frac=float(sp.get('maxBudgetFrac', 0.6)),
        initial_capital=float(pf.get('initial_equity_per_leg', 100.0)),
        use_even_bars=bool(sp.get('useEvenBars', True)),
        timeframe=str(cfg.get('timeframe', '2m')),
    )


def simulate_short_only(cfg, ts, close):
    pf = cfg.get('portfolio', {}) or {}
    initial_equity = float(pf.get('initial_equity_per_leg', 100.0))
    fee = float(pf.get('fee_rate', 0.0))
    slippage = float(pf.get('slippage_per_side', 0.0))
    st = build_state(cfg)
    realized = 0.0; trades = 0; wins = 0
    eq_real = []; eq_mtm = []
    for i in range(len(close)):
        px = float(close[i]); t = int(ts[i])
        if st.allow_bar(t):
            r = st.step(px, fee, slippage)
            realized += r['realized_pnl']; trades += r['trades']; wins += r['wins']
        if st.pos_qty <= 0 and st.allow_bar(t):
            st.maybe_open_first(px)
        eq_real.append(initial_equity + realized)
        eq_mtm.append(initial_equity + realized + st.unrealized(px, fee, slippage))
    arr_r = np.asarray(eq_real, dtype=np.float64)
    arr_m = np.asarray(eq_mtm, dtype=np.float64)
    mdd_r = float(((arr_r - np.maximum.accumulate(arr_r)) / np.maximum.accumulate(arr_r)).min()) if len(arr_r) > 1 else 0.0
    mdd_m = float(((arr_m - np.maximum.accumulate(arr_m)) / np.maximum.accumulate(arr_m)).min()) if len(arr_m) > 1 else 0.0
    return {
        'realized_pnl': realized,
        'equity_end_realized': initial_equity + realized,
        'return_pct': (realized / initial_equity * 100.0) if initial_equity else 0.0,
        'trades': trades,
        'win_rate_pct': wins * 100.0 / max(1, trades),
        'mdd_realized_pct': mdd_r * 100.0,
        'mdd_mtm_pct': mdd_m * 100.0,
        'max_gross_notional': st.gross_notional(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--npz', required=True)
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--out', default='short_leg_fast_multi_ranking.csv')
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.cfg, 'r'))
    data = np.load(args.npz, allow_pickle=True)
    symbols = data['symbols']
    offsets = data['offsets']
    ts_all = data['timestamp_s']
    close_all = data['close']

    rows = []
    for i, sym in enumerate(symbols):
        a, b = int(offsets[i]), int(offsets[i+1])
        ts = ts_all[a:b]
        close = close_all[a:b]
        r = simulate_short_only(cfg, ts, close)
        r['symbol'] = str(sym)
        r['bars'] = len(ts)
        rows.append(r)
        if (i+1) % 50 == 0:
            print(f'[progress] {i+1}/{len(symbols)}')
    df = pd.DataFrame(rows).sort_values(['realized_pnl', 'return_pct'], ascending=[False, False])
    df.to_csv(args.out, index=False)
    print(df.head(args.top).to_string(index=False))
    print(f'\n[files] {args.out}')

if __name__ == '__main__':
    main()
