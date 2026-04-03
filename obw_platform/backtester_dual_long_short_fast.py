#!/usr/bin/env python3
import argparse, json, math, os, time, yaml
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional
from pathlib import Path

@dataclass
class SideState:
    side: str
    first_usdt: float
    tp_percent: float
    callback_percent: float
    margin_call_limit: int
    linear_step_percent: float
    auto_merge: bool
    subsell_tp_percent: float
    step1: float
    step2: float
    step3: float
    step4: float
    step5: float
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
    pos_cost_usdt: float = 0.0
    pos_size: float = 0.0
    avg_price: Optional[float] = None
    num_fills: int = 0
    last_fill_price: Optional[float] = None
    next_level_price: Optional[float] = None
    lots_qty: List[float] = field(default_factory=list)
    lots_price: List[float] = field(default_factory=list)
    trailing_ref: Optional[float] = None
    reset_pending: bool = False
    pending_new_entry: Optional[float] = None

    def tf_seconds(self):
        s = str(self.timeframe).strip().lower()
        if s.endswith('m'): return max(1, int(round(float(s[:-1]) * 60.0)))
        if s.endswith('h'): return max(1, int(round(float(s[:-1]) * 3600.0)))
        if s.endswith('d'): return max(1, int(round(float(s[:-1]) * 86400.0)))
        return max(1, int(round(float(s) * 60.0)))

    def allow_bar(self, ts_s: int):
        if not self.use_even_bars: return True
        dt = np.datetime64(int(ts_s), 's').astype('datetime64[s]').astype(object)
        year = dt.year
        anchor = int(np.datetime64(f'{year}-01-01T00:00:00').astype('datetime64[s]').astype(int))
        bars_from_anchor = int((ts_s - anchor) // self.tf_seconds())
        return (bars_from_anchor % 2) == 0

    def get_step_for_next_level(self):
        nf = self.num_fills + 1
        return self.step1 if nf == 2 else self.step2 if nf == 3 else self.step3 if nf == 4 else self.step4 if nf == 5 else self.step5 if nf == 6 else self.linear_step_percent

    def get_mult_for_next_level(self):
        nf = self.num_fills + 1
        return self.mult2 if nf == 2 else self.mult3 if nf == 3 else self.mult4 if nf == 4 else self.mult5 if nf == 5 else 1.0

    def next_level(self, last_fill_price: float):
        step = self.get_step_for_next_level()
        return last_fill_price * (1.0 - step / 100.0) if self.side == 'LONG' else last_fill_price * (1.0 + step / 100.0)

    def entry_tp_sl(self, entry_price: float):
        tp = entry_price * (1.0 + self.tp_percent / 100.0) if self.side == 'LONG' else entry_price * (1.0 - self.tp_percent / 100.0)
        return tp, None

    def maybe_open_first(self, price: float, pos_notional: float):
        self.reset_pending = False
        self.trailing_ref = None
        self.pending_new_entry = None
        qty0_state = self.first_usdt / max(price, 1e-12)
        self.pos_cost_usdt = self.first_usdt
        self.pos_size = qty0_state
        self.avg_price = price
        self.num_fills = 1
        self.last_fill_price = price
        self.next_level_price = self.next_level(self.last_fill_price)
        self.lots_qty = [qty0_state]
        self.lots_price = [price]
        self.pos_qty = pos_notional / max(price, 1e-12)
        self.pos_entry = price

    def sync_after_backtester_mismatch(self):
        if self.pos_size > 0 and self.pos_qty > 0 and abs(self.pos_qty - self.pos_size) / max(self.pos_size, 1e-12) > 1e-6:
            ratio = self.pos_qty / self.pos_size
            self.lots_qty = [q * ratio for q in self.lots_qty]
            self.pos_size = self.pos_qty
        if self.pending_new_entry is not None:
            self.pos_entry = self.pending_new_entry
            self.avg_price = self.pending_new_entry
            self.pending_new_entry = None
        if self.avg_price is None:
            self.avg_price = self.pos_entry
        if self.pos_size <= 0:
            self.pos_size = self.pos_qty

    def unrealized(self, price: float, fee: float, slippage: float):
        if self.pos_qty <= 0: return 0.0
        gross_ret = ((price - self.pos_entry) / max(self.pos_entry,1e-12)) if self.side == 'LONG' else ((self.pos_entry - price) / max(self.pos_entry,1e-12))
        net_ret = gross_ret - 2*slippage - 2*fee
        return net_ret * (self.pos_entry * self.pos_qty)

    def gross_notional(self):
        return self.pos_entry * self.pos_qty if self.pos_qty > 0 else 0.0

    def step(self, price: float, fee: float, slippage: float):
        """Advance one bar. Returns dict with realized_pnl, trades, wins, partial/full close flags."""
        res = {'realized_pnl': 0.0, 'trades': 0, 'wins': 0, 'closed_full': False, 'closed_qty': 0.0}
        if self.pos_qty <= 0:
            return res
        self.sync_after_backtester_mismatch()
        max_budget = self.initial_capital * self.max_budget_frac

        tp_price = self.avg_price * (1.0 + self.tp_percent / 100.0) if self.side == 'LONG' else self.avg_price * (1.0 - self.tp_percent / 100.0)
        tp_hit = price >= tp_price if self.side == 'LONG' else price <= tp_price
        if tp_hit:
            if self.callback_percent and self.callback_percent > 0:
                if self.side == 'LONG':
                    self.trailing_ref = price if self.trailing_ref is None else max(self.trailing_ref, price)
                    trail_stop = self.trailing_ref * (1.0 - self.callback_percent / 100.0)
                    fire = price <= trail_stop
                else:
                    self.trailing_ref = price if self.trailing_ref is None else min(self.trailing_ref, price)
                    trail_stop = self.trailing_ref * (1.0 + self.callback_percent / 100.0)
                    fire = price >= trail_stop
                if fire:
                    notional = self.pos_entry * self.pos_qty
                    gross_ret = ((price - self.pos_entry) / max(self.pos_entry,1e-12)) if self.side == 'LONG' else ((self.pos_entry - price) / max(self.pos_entry,1e-12))
                    pnl = (gross_ret - 2*slippage - 2*fee) * notional
                    res['realized_pnl'] += pnl; res['trades'] += 1; res['wins'] += 1 if pnl > 0 else 0
                    res['closed_full'] = True; res['closed_qty'] = self.pos_qty
                    self.pos_qty = 0.0; self.pos_entry = 0.0
                    self.pos_cost_usdt = 0.0; self.pos_size = 0.0; self.avg_price = None; self.num_fills = 0
                    self.last_fill_price = None; self.next_level_price = None; self.lots_qty = []; self.lots_price = []
                    self.trailing_ref = None; self.reset_pending = True
                    return res
            else:
                notional = self.pos_entry * self.pos_qty
                gross_ret = ((price - self.pos_entry) / max(self.pos_entry,1e-12)) if self.side == 'LONG' else ((self.pos_entry - price) / max(self.pos_entry,1e-12))
                pnl = (gross_ret - 2*slippage - 2*fee) * notional
                res['realized_pnl'] += pnl; res['trades'] += 1; res['wins'] += 1 if pnl > 0 else 0
                res['closed_full'] = True; res['closed_qty'] = self.pos_qty
                self.pos_qty = 0.0; self.pos_entry = 0.0
                self.pos_cost_usdt = 0.0; self.pos_size = 0.0; self.avg_price = None; self.num_fills = 0
                self.last_fill_price = None; self.next_level_price = None; self.lots_qty = []; self.lots_price = []
                self.trailing_ref = None; self.reset_pending = True
                return res
        if not tp_hit:
            self.trailing_ref = None

        # DCA
        fills = 0
        while self.num_fills < self.margin_call_limit and fills < self.max_fills_per_bar and self.next_level_price is not None:
            hit = price <= self.next_level_price if self.side == 'LONG' else price >= self.next_level_price
            if not hit: break
            add_usdt = self.first_usdt * self.get_mult_for_next_level()
            if self.max_budget_frac < 0.999999 and (self.pos_cost_usdt + add_usdt) > max_budget:
                break
            fill_price = price
            qty_add = add_usdt / max(fill_price, 1e-12)
            new_cost = self.pos_entry * self.pos_qty + add_usdt
            new_qty = self.pos_qty + qty_add
            self.pos_qty = new_qty
            self.pos_entry = new_cost / max(new_qty, 1e-12)
            self.pos_cost_usdt += add_usdt
            self.pos_size = new_qty
            self.avg_price = self.pos_entry
            self.lots_qty.append(qty_add)
            self.lots_price.append(fill_price)
            self.num_fills += 1
            self.last_fill_price = fill_price
            self.next_level_price = self.next_level(self.last_fill_price)
            fills += 1

        # Partial LIFO
        subs = 0
        while self.num_fills > 5 and self.lots_qty and subs < self.max_subsells_per_bar:
            qty_last = self.lots_qty[-1]
            entry_last = self.lots_price[-1]
            last_lot_tp = entry_last * (1.0 + self.subsell_tp_percent / 100.0) if self.side == 'LONG' else entry_last * (1.0 - self.subsell_tp_percent / 100.0)
            hit = price >= last_lot_tp if self.side == 'LONG' else price <= last_lot_tp
            if not hit: break
            qty_total = self.pos_qty
            qty_close = min(qty_last, qty_total)
            if qty_total <= 0 or qty_close <= 0: break
            qty_frac = max(0.0, min(1.0, qty_close / max(qty_total, 1e-12)))
            notional_entry = qty_close * entry_last
            gross_ret = ((price - entry_last) / max(entry_last,1e-12)) if self.side == 'LONG' else ((entry_last - price) / max(entry_last,1e-12))
            pnl = (gross_ret - 2*slippage - 2*fee) * notional_entry
            res['realized_pnl'] += pnl; res['trades'] += 1; res['wins'] += 1 if pnl > 0 else 0
            total_cost = sum(q*p for q,p in zip(self.lots_qty, self.lots_price))
            profit = qty_close * ((price-entry_last) if self.side == 'LONG' else (entry_last-price))
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
            self.pos_size = self.pos_qty
            if self.lots_qty:
                self.last_fill_price = self.lots_price[-1]
                self.next_level_price = self.next_level(self.last_fill_price)
            else:
                self.last_fill_price = None; self.next_level_price = None
            subs += 1
            break
        return res


def build_side(cfg, params_key, side):
    sp = cfg.get(params_key, {}) or {}
    return SideState(
        side=side,
        first_usdt=float(sp.get('firstBuyUSDT' if side=='LONG' else 'firstSellUSDT', 5.0)),
        tp_percent=float(sp.get('tpPercent', 1.1)),
        callback_percent=float(sp.get('callbackPercent', 0.2)),
        margin_call_limit=int(sp.get('marginCallLimit', 244)),
        linear_step_percent=float(sp.get('linearDropPercent' if side=='LONG' else 'linearRisePercent', 0.5)),
        auto_merge=bool(sp.get('autoMerge', True)),
        subsell_tp_percent=float(sp.get('subSellTPPercent', 1.3)),
        step1=float(sp.get('drop1' if side=='LONG' else 'rise1', 0.3)),
        step2=float(sp.get('drop2' if side=='LONG' else 'rise2', 0.4)),
        step3=float(sp.get('drop3' if side=='LONG' else 'rise3', 0.6)),
        step4=float(sp.get('drop4' if side=='LONG' else 'rise4', 0.8)),
        step5=float(sp.get('drop5' if side=='LONG' else 'rise5', 0.8)),
        mult2=float(sp.get('mult2', 1.5)), mult3=float(sp.get('mult3', 1.0)), mult4=float(sp.get('mult4', 2.0)), mult5=float(sp.get('mult5', 3.5)),
        max_fills_per_bar=int(sp.get('maxFillsPerBar', 6)),
        max_subsells_per_bar=int(sp.get('maxSubSellsPerBar', 10)),
        max_budget_frac=float(sp.get('maxBudgetFrac', 0.60)),
        initial_capital=float(cfg.get('portfolio', {}).get('initial_equity_total', cfg.get('portfolio', {}).get('initial_equity_per_leg', 100.0)*2.0)),
        use_even_bars=bool(sp.get('useEvenBars', True)),
        timeframe=str(cfg.get('timeframe', '1m')),
    )


def simulate(cfg, ts_s, close):
    portfolio = cfg.get('portfolio', {})
    initial_equity_total = float(portfolio.get('initial_equity_total', portfolio.get('initial_equity_per_leg', 100.0)*2.0))
    pos_notional_long = float(portfolio.get('position_notional_long', portfolio.get('position_notional', 5.0)))
    pos_notional_short = float(portfolio.get('position_notional_short', portfolio.get('position_notional', 5.0)))
    fee = float(portfolio.get('fee_rate', 0.0))
    slippage = float(portfolio.get('slippage_per_side', 0.0))
    max_notional_frac = float(portfolio.get('max_notional_frac', 1.0))

    long = build_side(cfg, 'strategy_params_long', 'LONG')
    short = build_side(cfg, 'strategy_params_short', 'SHORT')

    realized_long = realized_short = 0.0
    trades_long = trades_short = wins_long = wins_short = 0
    margin_call_events_total = bars_in_margin_call = 0
    prev_in_margin = False
    total_equity_mtm = []
    total_equity_realized = []
    max_gross_long = max_gross_short = max_gross_total = max_effective = 0.0
    margin_call_excess_max = 0.0

    for i in range(len(close)):
        px = float(close[i]); ts = int(ts_s[i])
        # exits / updates
        if long.allow_bar(ts):
            r = long.step(px, fee, slippage)
            realized_long += r['realized_pnl']; trades_long += r['trades']; wins_long += r['wins']
        if short.allow_bar(ts):
            r = short.step(px, fee, slippage)
            realized_short += r['realized_pnl']; trades_short += r['trades']; wins_short += r['wins']

        unreal_long = long.unrealized(px, fee, slippage)
        unreal_short = short.unrealized(px, fee, slippage)
        equity_realized_total = initial_equity_total + realized_long + realized_short
        equity_mtm_total = equity_realized_total + unreal_long + unreal_short
        gross_long = long.gross_notional(); gross_short = short.gross_notional(); gross_total = gross_long + gross_short
        effective = abs(gross_long - gross_short)
        allowed = max_notional_frac * max(equity_mtm_total, 0.0)
        margin_call_excess = max(0.0, effective - allowed)
        in_margin = margin_call_excess > 0.0
        if in_margin: bars_in_margin_call += 1
        if in_margin and not prev_in_margin: margin_call_events_total += 1
        prev_in_margin = in_margin
        margin_call_excess_max = max(margin_call_excess_max, margin_call_excess)
        max_gross_long = max(max_gross_long, gross_long); max_gross_short = max(max_gross_short, gross_short)
        max_gross_total = max(max_gross_total, gross_total); max_effective = max(max_effective, effective)
        total_equity_mtm.append(equity_mtm_total)
        total_equity_realized.append(equity_realized_total)

        # entries / restarts only when flat for each side and allowed bar and margin permits
        if long.pos_qty <= 0 and long.allow_bar(ts):
            prospective = abs(pos_notional_long - short.gross_notional())
            if prospective <= allowed + 1e-12:
                long.maybe_open_first(px, pos_notional_long)
        if short.pos_qty <= 0 and short.allow_bar(ts):
            prospective = abs(long.gross_notional() - pos_notional_short)
            if prospective <= allowed + 1e-12:
                short.maybe_open_first(px, pos_notional_short)

    mdd_frac = 0.0
    mdd_realized_frac = 0.0
    if len(total_equity_mtm) > 1:
        arr = np.asarray(total_equity_mtm, dtype=np.float64)
        peaks = np.maximum.accumulate(arr)
        dd = (arr - peaks) / peaks
        mdd_frac = float(dd.min())
    if len(total_equity_realized) > 1:
        arr_r = np.asarray(total_equity_realized, dtype=np.float64)
        peaks_r = np.maximum.accumulate(arr_r)
        dd_r = (arr_r - peaks_r) / peaks_r
        mdd_realized_frac = float(dd_r.min())
    total_days = len(close) * (long.tf_seconds() / 86400.0)
    equity_end_realized_total = initial_equity_total + realized_long + realized_short
    daily_ret = monthly_ret = yearly_ret = 0.0
    if initial_equity_total > 0 and total_days > 0 and equity_end_realized_total > 0:
        tr = equity_end_realized_total / initial_equity_total
        daily_ret = tr ** (1.0 / total_days) - 1.0
        monthly_ret = tr ** (30.0 / total_days) - 1.0
        yearly_ret = tr ** (365.0 / total_days) - 1.0
    return {
        'equity_start_total': initial_equity_total,
        'equity_end_realized_total': equity_end_realized_total,
        'equity_end_realized_long': initial_equity_total / 2.0 + realized_long,
        'equity_end_realized_short': initial_equity_total / 2.0 + realized_short,
        'realized_pnl_long': realized_long,
        'realized_pnl_short': realized_short,
        'realized_pnl_total': realized_long + realized_short,
        'trades_long': trades_long,
        'trades_short': trades_short,
        'trades_total': trades_long + trades_short,
        'win_rate_long_%': wins_long * 100.0 / max(1, trades_long) if trades_long else 0.0,
        'win_rate_short_%': wins_short * 100.0 / max(1, trades_short) if trades_short else 0.0,
        'mdd_total_frac': mdd_frac,
        'mdd_total_%': mdd_frac * 100.0,
        'mdd_mtm_frac': mdd_frac,
        'mdd_mtm_%': mdd_frac * 100.0,
        'mdd_realized_frac': mdd_realized_frac,
        'mdd_realized_%': mdd_realized_frac * 100.0,
        'daily_return_total_%': daily_ret * 100.0,
        'monthly_return_total_%': monthly_ret * 100.0,
        'yearly_return_total_%': yearly_ret * 100.0,
        'margin_call_events_total': margin_call_events_total,
        'bars_in_margin_call': bars_in_margin_call,
        'margin_call_excess_max': margin_call_excess_max,
        'max_gross_notional_long': max_gross_long,
        'max_gross_notional_short': max_gross_short,
        'max_gross_notional_total': max_gross_total,
        'max_effective_notional_total': max_effective,
        'max_invested_long_in_firstBuyUSDT': (max_gross_long / max(long.first_usdt, 1e-12)) if long.first_usdt else None,
        'max_invested_short_in_firstSellUSDT': (max_gross_short / max(short.first_usdt, 1e-12)) if short.first_usdt else None,
        'firstBuyUSDT': long.first_usdt,
        'firstSellUSDT': short.first_usdt,
    }


def main():
    ap = argparse.ArgumentParser(description='Fast dual long+short backtester specialized for one symbol and NPZ cache')
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--npz', required=True)
    ap.add_argument('--limit-bars', type=int, default=0)
    ap.add_argument('--time-from', default='')
    ap.add_argument('--time-to', default='')
    args = ap.parse_args()
    t0 = time.time()
    cfg = yaml.safe_load(open(args.cfg, 'r'))
    data = np.load(args.npz)
    ts_s = data['timestamp_s'].astype(np.int64)
    close = data['close'].astype(np.float64)
    if args.time_from:
        tf = int(np.datetime64(args.time_from).astype('datetime64[s]').astype(int))
        mask = ts_s >= tf
        ts_s = ts_s[mask]; close = close[mask]
    if args.time_to:
        tt = int(np.datetime64(args.time_to).astype('datetime64[s]').astype(int))
        mask = ts_s <= tt
        ts_s = ts_s[mask]; close = close[mask]
    if args.limit_bars and args.limit_bars > 0:
        ts_s = ts_s[-args.limit_bars:]; close = close[-args.limit_bars:]
    summary = simulate(cfg, ts_s, close)
    summary['elapsed_sec'] = time.time() - t0
    print(json.dumps(summary, indent=2, default=str))

if __name__ == '__main__':
    main()
