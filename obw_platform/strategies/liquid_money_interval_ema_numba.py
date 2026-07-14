"""Numba-friendly liquid-money interval strategy.

Research-only module. It does not call exchanges, read secrets, or place
orders. The model is intentionally compact so overnight tuners can evaluate
many parameter sets quickly:

- BUY/SELL signals are EMA crossovers.
- Rolling min/max is taken from prior bars, excluding the current bar.
- `interval_cap_pct` is the max exposure used inside the prior interval.
- If price breaks outside the prior interval, exposure can extend beyond
  `interval_cap_pct` up to 100%, preserving reserve capital for breakouts.
- Only one side is open at a time. Opposite signals reduce the current side
  first and do not reverse on the same signal.
"""
from __future__ import annotations

import numpy as np
from numba import njit


@njit(cache=True)
def build_prior_interval(high: np.ndarray, low: np.ndarray, lookback: int):
    n = len(high)
    out_hi = np.empty(n, dtype=np.float64)
    out_lo = np.empty(n, dtype=np.float64)
    max_idx = np.empty(n, dtype=np.int64)
    min_idx = np.empty(n, dtype=np.int64)
    max_head = 0
    max_tail = 0
    min_head = 0
    min_tail = 0
    for i in range(n):
        j = i - 1
        if j >= 0:
            while max_tail > max_head and high[max_idx[max_tail - 1]] <= high[j]:
                max_tail -= 1
            max_idx[max_tail] = j
            max_tail += 1
            while min_tail > min_head and low[min_idx[min_tail - 1]] >= low[j]:
                min_tail -= 1
            min_idx[min_tail] = j
            min_tail += 1

        start = i - lookback
        while max_tail > max_head and max_idx[max_head] < start:
            max_head += 1
        while min_tail > min_head and min_idx[min_head] < start:
            min_head += 1

        if max_tail <= max_head or min_tail <= min_head:
            out_hi[i] = high[i]
            out_lo[i] = low[i]
        else:
            out_hi[i] = high[max_idx[max_head]]
            out_lo[i] = low[min_idx[min_head]]
    return out_hi, out_lo


@njit(cache=True)
def simulate_liquid_money_interval(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    prior_hi: np.ndarray,
    prior_lo: np.ndarray,
    fast_len: int,
    slow_len: int,
    interval_cap_pct: float,
    min_step_pct: float,
    gamma: float,
    fee_rate: float,
    slippage_per_side: float,
    initial_equity: float,
    leverage: float,
):
    n = len(close)
    if n < 3:
        return np.zeros(18, dtype=np.float64)

    alpha_fast = 2.0 / (fast_len + 1.0)
    alpha_slow = 2.0 / (slow_len + 1.0)
    fast_prev = close[0]
    slow_prev = close[0]
    fast = close[0]
    slow = close[0]

    side = 0  # 1 long, -1 short, 0 flat
    exposure_pct = 0.0
    avg_entry = 0.0
    realized = 0.0
    fees = 0.0
    trades = 0
    wins = 0
    skipped_full = 0
    max_exposure = 0.0

    equity_peak = initial_equity
    max_dd = 0.0
    min_mtm = 0.0
    gross_profit = 0.0
    gross_loss = 0.0

    for i in range(1, n):
        fast_prev = fast
        slow_prev = slow
        fast = alpha_fast * close[i] + (1.0 - alpha_fast) * fast
        slow = alpha_slow * close[i] + (1.0 - alpha_slow) * slow

        buy = fast_prev <= slow_prev and fast > slow
        sell = fast_prev >= slow_prev and fast < slow

        rng = prior_hi[i] - prior_lo[i]
        if rng <= 1e-12:
            rng = max(abs(close[i]) * 1e-6, 1e-12)

        long_score = (prior_hi[i] - close[i]) / rng
        short_score = (close[i] - prior_lo[i]) / rng
        if long_score < 0.0:
            long_score = 0.0
        if short_score < 0.0:
            short_score = 0.0

        # score 1.0 means the old interval boundary. Inside the interval this
        # maps to interval_cap_pct; outside it can continue up to 100%.
        long_target = interval_cap_pct * (long_score ** gamma)
        short_target = interval_cap_pct * (short_score ** gamma)
        if long_target > 100.0:
            long_target = 100.0
        if short_target > 100.0:
            short_target = 100.0

        px = close[i]

        if buy:
            if side < 0:
                target_remaining = 100.0 - long_target
                if target_remaining < 0.0:
                    target_remaining = 0.0
                reduce_pct = exposure_pct - target_remaining
                if reduce_pct < min_step_pct:
                    reduce_pct = min_step_pct
                if reduce_pct > exposure_pct:
                    reduce_pct = exposure_pct
                if reduce_pct > 0.0:
                    notional = initial_equity * leverage * reduce_pct / 100.0
                    ret = avg_entry / max(px, 1e-12) - 1.0
                    pnl = notional * (ret - fee_rate - slippage_per_side)
                    realized += pnl
                    fees += notional * (fee_rate + slippage_per_side)
                    trades += 1
                    if pnl > 0:
                        wins += 1
                        gross_profit += pnl
                    else:
                        gross_loss -= pnl
                    exposure_pct -= reduce_pct
                    if exposure_pct <= 1e-9:
                        side = 0
                        exposure_pct = 0.0
                        avg_entry = 0.0
            else:
                add_pct = long_target - exposure_pct
                if add_pct < min_step_pct:
                    add_pct = min_step_pct
                if add_pct > 100.0 - exposure_pct:
                    add_pct = 100.0 - exposure_pct
                if add_pct > 0.0:
                    old_notional = exposure_pct
                    new_notional = exposure_pct + add_pct
                    if side == 0:
                        avg_entry = px
                        side = 1
                    else:
                        avg_entry = (avg_entry * old_notional + px * add_pct) / max(new_notional, 1e-12)
                    exposure_pct = new_notional
                    trades += 1
                else:
                    skipped_full += 1

        if sell:
            if side > 0:
                target_remaining = 100.0 - short_target
                if target_remaining < 0.0:
                    target_remaining = 0.0
                reduce_pct = exposure_pct - target_remaining
                if reduce_pct < min_step_pct:
                    reduce_pct = min_step_pct
                if reduce_pct > exposure_pct:
                    reduce_pct = exposure_pct
                if reduce_pct > 0.0:
                    notional = initial_equity * leverage * reduce_pct / 100.0
                    ret = px / max(avg_entry, 1e-12) - 1.0
                    pnl = notional * (ret - fee_rate - slippage_per_side)
                    realized += pnl
                    fees += notional * (fee_rate + slippage_per_side)
                    trades += 1
                    if pnl > 0:
                        wins += 1
                        gross_profit += pnl
                    else:
                        gross_loss -= pnl
                    exposure_pct -= reduce_pct
                    if exposure_pct <= 1e-9:
                        side = 0
                        exposure_pct = 0.0
                        avg_entry = 0.0
            else:
                add_pct = short_target - exposure_pct
                if add_pct < min_step_pct:
                    add_pct = min_step_pct
                if add_pct > 100.0 - exposure_pct:
                    add_pct = 100.0 - exposure_pct
                if add_pct > 0.0:
                    old_notional = exposure_pct
                    new_notional = exposure_pct + add_pct
                    if side == 0:
                        avg_entry = px
                        side = -1
                    else:
                        avg_entry = (avg_entry * old_notional + px * add_pct) / max(new_notional, 1e-12)
                    exposure_pct = new_notional
                    trades += 1
                else:
                    skipped_full += 1

        unreal = 0.0
        if side > 0 and exposure_pct > 0.0:
            notional_open = initial_equity * leverage * exposure_pct / 100.0
            unreal = notional_open * (px / max(avg_entry, 1e-12) - 1.0 - fee_rate - slippage_per_side)
        elif side < 0 and exposure_pct > 0.0:
            notional_open = initial_equity * leverage * exposure_pct / 100.0
            unreal = notional_open * (avg_entry / max(px, 1e-12) - 1.0 - fee_rate - slippage_per_side)

        equity = initial_equity + realized + unreal
        if equity > equity_peak:
            equity_peak = equity
        dd = 100.0 * (equity - equity_peak) / max(equity_peak, 1e-12)
        if dd < max_dd:
            max_dd = dd
        mtm_pct = 100.0 * (realized + unreal) / max(initial_equity, 1e-12)
        if mtm_pct < min_mtm:
            min_mtm = mtm_pct
        if exposure_pct > max_exposure:
            max_exposure = exposure_pct

    final_unreal = 0.0
    if side != 0 and exposure_pct > 0.0:
        px = close[n - 1]
        notional_open = initial_equity * leverage * exposure_pct / 100.0
        if side > 0:
            final_unreal = notional_open * (px / max(avg_entry, 1e-12) - 1.0 - fee_rate - slippage_per_side)
        else:
            final_unreal = notional_open * (avg_entry / max(px, 1e-12) - 1.0 - fee_rate - slippage_per_side)

    out = np.zeros(18, dtype=np.float64)
    out[0] = realized
    out[1] = final_unreal
    out[2] = 100.0 * (realized + final_unreal) / max(initial_equity, 1e-12)
    out[3] = max_dd
    out[4] = min_mtm
    out[5] = trades
    out[6] = wins
    out[7] = 100.0 * wins / max(trades, 1)
    out[8] = gross_profit / max(gross_loss, 1e-12)
    out[9] = max_exposure
    out[10] = exposure_pct
    out[11] = side
    out[12] = fees
    out[13] = skipped_full
    out[14] = fast_len
    out[15] = slow_len
    out[16] = interval_cap_pct
    out[17] = gamma
    return out


@njit(cache=True)
def simulate_liquid_money_interval_stubborn_only_win(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    prior_hi: np.ndarray,
    prior_lo: np.ndarray,
    fast_len: int,
    slow_len: int,
    interval_cap_pct: float,
    min_step_pct: float,
    gamma: float,
    min_profit_pct: float,
    max_hold_bars: int,
    loss_cut_mtm_pct: float,
    fee_rate: float,
    slippage_per_side: float,
    initial_equity: float,
    leverage: float,
):
    n = len(close)
    if n < 3:
        return np.zeros(22, dtype=np.float64)

    alpha_fast = 2.0 / (fast_len + 1.0)
    alpha_slow = 2.0 / (slow_len + 1.0)
    fast_prev = close[0]
    slow_prev = close[0]
    fast = close[0]
    slow = close[0]

    side = 0
    exposure_pct = 0.0
    avg_entry = 0.0
    entry_bar = 0
    realized = 0.0
    fees = 0.0
    trades = 0
    wins = 0
    forced_losses = 0
    stubborn_adds = 0
    max_exposure = 0.0
    equity_peak = initial_equity
    max_dd = 0.0
    min_mtm = 0.0
    gross_profit = 0.0
    gross_loss = 0.0

    for i in range(1, n):
        fast_prev = fast
        slow_prev = slow
        fast = alpha_fast * close[i] + (1.0 - alpha_fast) * fast
        slow = alpha_slow * close[i] + (1.0 - alpha_slow) * slow

        buy = fast_prev <= slow_prev and fast > slow
        sell = fast_prev >= slow_prev and fast < slow

        rng = prior_hi[i] - prior_lo[i]
        if rng <= 1e-12:
            rng = max(abs(close[i]) * 1e-6, 1e-12)
        long_score = (prior_hi[i] - close[i]) / rng
        short_score = (close[i] - prior_lo[i]) / rng
        if long_score < 0.0:
            long_score = 0.0
        if short_score < 0.0:
            short_score = 0.0
        long_target = interval_cap_pct * (long_score ** gamma)
        short_target = interval_cap_pct * (short_score ** gamma)
        if long_target > 100.0:
            long_target = 100.0
        if short_target > 100.0:
            short_target = 100.0

        px = close[i]
        unreal = 0.0
        if side > 0 and exposure_pct > 0.0:
            notional_open = initial_equity * leverage * exposure_pct / 100.0
            unreal = notional_open * (px / max(avg_entry, 1e-12) - 1.0 - fee_rate - slippage_per_side)
        elif side < 0 and exposure_pct > 0.0:
            notional_open = initial_equity * leverage * exposure_pct / 100.0
            unreal = notional_open * (avg_entry / max(px, 1e-12) - 1.0 - fee_rate - slippage_per_side)

        mtm_pct_now = 100.0 * (realized + unreal) / max(initial_equity, 1e-12)
        force_loss = False
        if side != 0 and exposure_pct > 0.0:
            held = i - entry_bar
            if max_hold_bars > 0 and held >= max_hold_bars:
                force_loss = True
            if loss_cut_mtm_pct < 0.0 and mtm_pct_now <= loss_cut_mtm_pct:
                force_loss = True

        if force_loss:
            notional = initial_equity * leverage * exposure_pct / 100.0
            if side > 0:
                ret = px / max(avg_entry, 1e-12) - 1.0
            else:
                ret = avg_entry / max(px, 1e-12) - 1.0
            pnl = notional * (ret - fee_rate - slippage_per_side)
            realized += pnl
            fees += notional * (fee_rate + slippage_per_side)
            trades += 1
            forced_losses += 1
            if pnl > 0.0:
                wins += 1
                gross_profit += pnl
            else:
                gross_loss -= pnl
            side = 0
            exposure_pct = 0.0
            avg_entry = 0.0

        if buy:
            if side < 0:
                profitable = px < avg_entry * (1.0 - min_profit_pct / 100.0)
                if profitable:
                    reduce_pct = min_step_pct
                    if reduce_pct > exposure_pct:
                        reduce_pct = exposure_pct
                    if reduce_pct > 0.0:
                        notional = initial_equity * leverage * reduce_pct / 100.0
                        ret = avg_entry / max(px, 1e-12) - 1.0
                        pnl = notional * (ret - fee_rate - slippage_per_side)
                        realized += pnl
                        fees += notional * (fee_rate + slippage_per_side)
                        trades += 1
                        if pnl > 0.0:
                            wins += 1
                            gross_profit += pnl
                        else:
                            gross_loss -= pnl
                        exposure_pct -= reduce_pct
                        if exposure_pct <= 1e-9:
                            side = 0
                            exposure_pct = 0.0
                            avg_entry = 0.0
                else:
                    add_pct = short_target - exposure_pct
                    if add_pct < min_step_pct:
                        add_pct = min_step_pct
                    if add_pct > 100.0 - exposure_pct:
                        add_pct = 100.0 - exposure_pct
                    if add_pct > 0.0:
                        avg_entry = (avg_entry * exposure_pct + px * add_pct) / max(exposure_pct + add_pct, 1e-12)
                        exposure_pct += add_pct
                        trades += 1
                        stubborn_adds += 1
            else:
                add_pct = long_target - exposure_pct
                if add_pct < min_step_pct:
                    add_pct = min_step_pct
                if add_pct > 100.0 - exposure_pct:
                    add_pct = 100.0 - exposure_pct
                if add_pct > 0.0:
                    if side == 0:
                        avg_entry = px
                        side = 1
                        entry_bar = i
                    else:
                        avg_entry = (avg_entry * exposure_pct + px * add_pct) / max(exposure_pct + add_pct, 1e-12)
                    exposure_pct += add_pct
                    trades += 1

        if sell:
            if side > 0:
                profitable = px > avg_entry * (1.0 + min_profit_pct / 100.0)
                if profitable:
                    reduce_pct = min_step_pct
                    if reduce_pct > exposure_pct:
                        reduce_pct = exposure_pct
                    if reduce_pct > 0.0:
                        notional = initial_equity * leverage * reduce_pct / 100.0
                        ret = px / max(avg_entry, 1e-12) - 1.0
                        pnl = notional * (ret - fee_rate - slippage_per_side)
                        realized += pnl
                        fees += notional * (fee_rate + slippage_per_side)
                        trades += 1
                        if pnl > 0.0:
                            wins += 1
                            gross_profit += pnl
                        else:
                            gross_loss -= pnl
                        exposure_pct -= reduce_pct
                        if exposure_pct <= 1e-9:
                            side = 0
                            exposure_pct = 0.0
                            avg_entry = 0.0
                else:
                    add_pct = long_target - exposure_pct
                    if add_pct < min_step_pct:
                        add_pct = min_step_pct
                    if add_pct > 100.0 - exposure_pct:
                        add_pct = 100.0 - exposure_pct
                    if add_pct > 0.0:
                        avg_entry = (avg_entry * exposure_pct + px * add_pct) / max(exposure_pct + add_pct, 1e-12)
                        exposure_pct += add_pct
                        trades += 1
                        stubborn_adds += 1
            else:
                add_pct = short_target - exposure_pct
                if add_pct < min_step_pct:
                    add_pct = min_step_pct
                if add_pct > 100.0 - exposure_pct:
                    add_pct = 100.0 - exposure_pct
                if add_pct > 0.0:
                    if side == 0:
                        avg_entry = px
                        side = -1
                        entry_bar = i
                    else:
                        avg_entry = (avg_entry * exposure_pct + px * add_pct) / max(exposure_pct + add_pct, 1e-12)
                    exposure_pct += add_pct
                    trades += 1

        unreal = 0.0
        if side > 0 and exposure_pct > 0.0:
            notional_open = initial_equity * leverage * exposure_pct / 100.0
            unreal = notional_open * (px / max(avg_entry, 1e-12) - 1.0 - fee_rate - slippage_per_side)
        elif side < 0 and exposure_pct > 0.0:
            notional_open = initial_equity * leverage * exposure_pct / 100.0
            unreal = notional_open * (avg_entry / max(px, 1e-12) - 1.0 - fee_rate - slippage_per_side)

        equity = initial_equity + realized + unreal
        if equity > equity_peak:
            equity_peak = equity
        dd = 100.0 * (equity - equity_peak) / max(equity_peak, 1e-12)
        if dd < max_dd:
            max_dd = dd
        mtm_pct = 100.0 * (realized + unreal) / max(initial_equity, 1e-12)
        if mtm_pct < min_mtm:
            min_mtm = mtm_pct
        if exposure_pct > max_exposure:
            max_exposure = exposure_pct

    final_unreal = 0.0
    if side != 0 and exposure_pct > 0.0:
        px = close[n - 1]
        notional_open = initial_equity * leverage * exposure_pct / 100.0
        if side > 0:
            final_unreal = notional_open * (px / max(avg_entry, 1e-12) - 1.0 - fee_rate - slippage_per_side)
        else:
            final_unreal = notional_open * (avg_entry / max(px, 1e-12) - 1.0 - fee_rate - slippage_per_side)

    out = np.zeros(22, dtype=np.float64)
    out[0] = realized
    out[1] = final_unreal
    out[2] = 100.0 * (realized + final_unreal) / max(initial_equity, 1e-12)
    out[3] = max_dd
    out[4] = min_mtm
    out[5] = trades
    out[6] = wins
    out[7] = 100.0 * wins / max(trades, 1)
    out[8] = gross_profit / max(gross_loss, 1e-12)
    out[9] = max_exposure
    out[10] = exposure_pct
    out[11] = side
    out[12] = fees
    out[13] = forced_losses
    out[14] = stubborn_adds
    out[15] = fast_len
    out[16] = slow_len
    out[17] = interval_cap_pct
    out[18] = gamma
    out[19] = min_profit_pct
    out[20] = max_hold_bars
    out[21] = loss_cut_mtm_pct
    return out


@njit(cache=True)
def simulate_liquid_money_interval_stubborn_only_win_live_like(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    prior_hi: np.ndarray,
    prior_lo: np.ndarray,
    fast_len: int,
    slow_len: int,
    interval_cap_pct: float,
    min_step_notional_usdt: float,
    gamma: float,
    min_profit_pct: float,
    max_hold_bars: int,
    loss_cut_mtm_pct: float,
    fee_rate: float,
    slippage_per_side: float,
    initial_equity: float,
    long_leverage: float,
    short_leverage: float,
):
    n = len(close)
    if n < 3:
        return np.zeros(26, dtype=np.float64)

    alpha_fast = 2.0 / (fast_len + 1.0)
    alpha_slow = 2.0 / (slow_len + 1.0)
    fast = close[0]
    slow = close[0]

    side = 0
    exposure_pct = 0.0
    avg_entry = 0.0
    entry_bar = 0
    realized = 0.0
    fees = 0.0
    trades = 0
    wins = 0
    forced_losses = 0
    stubborn_adds = 0
    max_exposure = 0.0
    equity_peak = initial_equity
    equity_peak_i = 0
    max_stagnation_bars = 0
    max_dd = 0.0
    min_mtm = 0.0
    gross_profit = 0.0
    gross_loss = 0.0

    for i in range(1, n):
        fast_prev = fast
        slow_prev = slow
        fast = alpha_fast * close[i] + (1.0 - alpha_fast) * fast
        slow = alpha_slow * close[i] + (1.0 - alpha_slow) * slow

        buy = fast_prev <= slow_prev and fast > slow
        sell = fast_prev >= slow_prev and fast < slow

        rng = prior_hi[i] - prior_lo[i]
        if rng <= 1e-12:
            rng = max(abs(close[i]) * 1e-6, 1e-12)
        long_score = (prior_hi[i] - close[i]) / rng
        short_score = (close[i] - prior_lo[i]) / rng
        if long_score < 0.0:
            long_score = 0.0
        if short_score < 0.0:
            short_score = 0.0
        long_target = interval_cap_pct * (long_score ** gamma)
        short_target = interval_cap_pct * (short_score ** gamma)
        if long_target > 100.0:
            long_target = 100.0
        if short_target > 100.0:
            short_target = 100.0

        px = close[i]
        unreal = 0.0
        if side > 0 and exposure_pct > 0.0:
            notional_open = initial_equity * long_leverage * exposure_pct / 100.0
            unreal = notional_open * (px / max(avg_entry, 1e-12) - 1.0 - fee_rate - slippage_per_side)
        elif side < 0 and exposure_pct > 0.0:
            notional_open = initial_equity * short_leverage * exposure_pct / 100.0
            unreal = notional_open * (avg_entry / max(px, 1e-12) - 1.0 - fee_rate - slippage_per_side)

        mtm_pct_now = 100.0 * (realized + unreal) / max(initial_equity, 1e-12)
        force_loss = False
        if side != 0 and exposure_pct > 0.0:
            held = i - entry_bar
            if max_hold_bars > 0 and held >= max_hold_bars:
                force_loss = True
            if loss_cut_mtm_pct < 0.0 and mtm_pct_now <= loss_cut_mtm_pct:
                force_loss = True

        if force_loss:
            lev = long_leverage if side > 0 else short_leverage
            notional = initial_equity * lev * exposure_pct / 100.0
            if side > 0:
                ret = px / max(avg_entry, 1e-12) - 1.0
            else:
                ret = avg_entry / max(px, 1e-12) - 1.0
            pnl = notional * (ret - fee_rate - slippage_per_side)
            realized += pnl
            fees += notional * (fee_rate + slippage_per_side)
            trades += 1
            forced_losses += 1
            if pnl > 0.0:
                wins += 1
                gross_profit += pnl
            else:
                gross_loss -= pnl
            side = 0
            exposure_pct = 0.0
            avg_entry = 0.0

        if buy:
            if side < 0:
                profitable = px < avg_entry * (1.0 - min_profit_pct / 100.0)
                min_step_pct = 100.0 * min_step_notional_usdt / max(initial_equity * short_leverage, 1e-12)
                if profitable:
                    reduce_pct = min_step_pct
                    if reduce_pct > exposure_pct:
                        reduce_pct = exposure_pct
                    if reduce_pct > 0.0:
                        notional = initial_equity * short_leverage * reduce_pct / 100.0
                        ret = avg_entry / max(px, 1e-12) - 1.0
                        pnl = notional * (ret - fee_rate - slippage_per_side)
                        realized += pnl
                        fees += notional * (fee_rate + slippage_per_side)
                        trades += 1
                        if pnl > 0.0:
                            wins += 1
                            gross_profit += pnl
                        else:
                            gross_loss -= pnl
                        exposure_pct -= reduce_pct
                        if exposure_pct <= 1e-9:
                            side = 0
                            exposure_pct = 0.0
                            avg_entry = 0.0
                else:
                    add_pct = short_target - exposure_pct
                    if add_pct < min_step_pct:
                        add_pct = min_step_pct
                    if add_pct > 100.0 - exposure_pct:
                        add_pct = 100.0 - exposure_pct
                    if add_pct > 0.0:
                        avg_entry = (avg_entry * exposure_pct + px * add_pct) / max(exposure_pct + add_pct, 1e-12)
                        exposure_pct += add_pct
                        trades += 1
                        stubborn_adds += 1
            else:
                min_step_pct = 100.0 * min_step_notional_usdt / max(initial_equity * long_leverage, 1e-12)
                add_pct = long_target - exposure_pct
                if add_pct < min_step_pct:
                    add_pct = min_step_pct
                if add_pct > 100.0 - exposure_pct:
                    add_pct = 100.0 - exposure_pct
                if add_pct > 0.0:
                    if side == 0:
                        avg_entry = px
                        side = 1
                        entry_bar = i
                    else:
                        avg_entry = (avg_entry * exposure_pct + px * add_pct) / max(exposure_pct + add_pct, 1e-12)
                    exposure_pct += add_pct
                    trades += 1

        if sell:
            if side > 0:
                profitable = px > avg_entry * (1.0 + min_profit_pct / 100.0)
                min_step_pct = 100.0 * min_step_notional_usdt / max(initial_equity * long_leverage, 1e-12)
                if profitable:
                    reduce_pct = min_step_pct
                    if reduce_pct > exposure_pct:
                        reduce_pct = exposure_pct
                    if reduce_pct > 0.0:
                        notional = initial_equity * long_leverage * reduce_pct / 100.0
                        ret = px / max(avg_entry, 1e-12) - 1.0
                        pnl = notional * (ret - fee_rate - slippage_per_side)
                        realized += pnl
                        fees += notional * (fee_rate + slippage_per_side)
                        trades += 1
                        if pnl > 0.0:
                            wins += 1
                            gross_profit += pnl
                        else:
                            gross_loss -= pnl
                        exposure_pct -= reduce_pct
                        if exposure_pct <= 1e-9:
                            side = 0
                            exposure_pct = 0.0
                            avg_entry = 0.0
                else:
                    add_pct = long_target - exposure_pct
                    if add_pct < min_step_pct:
                        add_pct = min_step_pct
                    if add_pct > 100.0 - exposure_pct:
                        add_pct = 100.0 - exposure_pct
                    if add_pct > 0.0:
                        avg_entry = (avg_entry * exposure_pct + px * add_pct) / max(exposure_pct + add_pct, 1e-12)
                        exposure_pct += add_pct
                        trades += 1
                        stubborn_adds += 1
            else:
                min_step_pct = 100.0 * min_step_notional_usdt / max(initial_equity * short_leverage, 1e-12)
                add_pct = short_target - exposure_pct
                if add_pct < min_step_pct:
                    add_pct = min_step_pct
                if add_pct > 100.0 - exposure_pct:
                    add_pct = 100.0 - exposure_pct
                if add_pct > 0.0:
                    if side == 0:
                        avg_entry = px
                        side = -1
                        entry_bar = i
                    else:
                        avg_entry = (avg_entry * exposure_pct + px * add_pct) / max(exposure_pct + add_pct, 1e-12)
                    exposure_pct += add_pct
                    trades += 1

        unreal = 0.0
        if side > 0 and exposure_pct > 0.0:
            notional_open = initial_equity * long_leverage * exposure_pct / 100.0
            unreal = notional_open * (px / max(avg_entry, 1e-12) - 1.0 - fee_rate - slippage_per_side)
        elif side < 0 and exposure_pct > 0.0:
            notional_open = initial_equity * short_leverage * exposure_pct / 100.0
            unreal = notional_open * (avg_entry / max(px, 1e-12) - 1.0 - fee_rate - slippage_per_side)

        equity = initial_equity + realized + unreal
        if equity > equity_peak:
            equity_peak = equity
            equity_peak_i = i
        else:
            stagnation_bars = i - equity_peak_i
            if stagnation_bars > max_stagnation_bars:
                max_stagnation_bars = stagnation_bars
        dd = 100.0 * (equity - equity_peak) / max(equity_peak, 1e-12)
        if dd < max_dd:
            max_dd = dd
        mtm_pct = 100.0 * (realized + unreal) / max(initial_equity, 1e-12)
        if mtm_pct < min_mtm:
            min_mtm = mtm_pct
        if exposure_pct > max_exposure:
            max_exposure = exposure_pct

    final_unreal = 0.0
    if side != 0 and exposure_pct > 0.0:
        px = close[n - 1]
        lev = long_leverage if side > 0 else short_leverage
        notional_open = initial_equity * lev * exposure_pct / 100.0
        if side > 0:
            final_unreal = notional_open * (px / max(avg_entry, 1e-12) - 1.0 - fee_rate - slippage_per_side)
        else:
            final_unreal = notional_open * (avg_entry / max(px, 1e-12) - 1.0 - fee_rate - slippage_per_side)

    out = np.zeros(26, dtype=np.float64)
    out[0] = realized
    out[1] = final_unreal
    out[2] = 100.0 * (realized + final_unreal) / max(initial_equity, 1e-12)
    out[3] = max_dd
    out[4] = min_mtm
    out[5] = trades
    out[6] = wins
    out[7] = 100.0 * wins / max(trades, 1)
    out[8] = gross_profit / max(gross_loss, 1e-12)
    out[9] = max_exposure
    out[10] = exposure_pct
    out[11] = side
    out[12] = fees
    out[13] = forced_losses
    out[14] = stubborn_adds
    out[15] = fast_len
    out[16] = slow_len
    out[17] = interval_cap_pct
    out[18] = gamma
    out[19] = min_profit_pct
    out[20] = max_hold_bars
    out[21] = loss_cut_mtm_pct
    out[22] = max_stagnation_bars
    out[23] = min_step_notional_usdt
    out[24] = long_leverage
    out[25] = short_leverage
    return out
