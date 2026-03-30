#!/usr/bin/env python3
# tuner_plan_dual_30s_2h.py
# 2h starter plan for backtester_dual_long_short_mtm.py
#
# Budget assumption:
# - one run ~= 4.49s
# - 2h budget ~= ~1600 runs max
# - this plan targets ~1027 runs (+ baseline), leaving headroom
#
# IMPORTANT:
# - This plan assumes the backtester supports:
#     portfolio.position_notional_long
#     portfolio.position_notional_short
# - If you really mean 0.05% fee, use fee_rate: 0.0005.
#   fee_rate: 0.05 means 5%% per side in the current backtester.
# - In the current Python strategy, firstBuyUSDT / firstSellUSDT are still treated as quote-notional,
#   not base-asset quantity. If you want literal ENA quantity semantics, the strategy should be patched first.

GRID_VALUES_ARE_DELTAS = True


def default_plan(limit_bars: int = None):
    return [
        # ---- Phase 1: rays (~55 runs) ----
        ('rays', {'strategy_params_long.tpPercent':        [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_short.tpPercent':       [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_long.subSellTPPercent': [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_short.subSellTPPercent':[-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_long.callbackPercent':  [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_short.callbackPercent': [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_long.linearDropPercent':[-0.06, -0.03, 0.0, +0.03, +0.06]}),
        ('rays', {'strategy_params_short.linearRisePercent':[-0.06, -0.03, 0.0, +0.03, +0.06]}),
        ('rays', {'portfolio.position_notional_long':      [-1.0, -0.5, 0.0, +0.5, +1.0]}),
        ('rays', {'portfolio.position_notional_short':     [-6.0, -3.0, 0.0, +3.0, +6.0]}),
        ('rays', {'strategy_params_long.firstBuyUSDT':     [-1.0, -0.5, 0.0, +0.5, +1.0]}),
        ('rays', {'strategy_params_short.firstSellUSDT':   [-6.0, -3.0, 0.0, +3.0, +6.0]}),

        # ---- Phase 2: focused exit grid (~729 runs) ----
        ('grid', {
            'strategy_params_long.tpPercent':         'around:0.03',
            'strategy_params_short.tpPercent':        'around:0.03',
            'strategy_params_long.subSellTPPercent':  'around:0.05',
            'strategy_params_short.subSellTPPercent': 'around:0.05',
            'strategy_params_long.callbackPercent':   'around:0.03',
            'strategy_params_short.callbackPercent':  'around:0.03',
        }),

        # ---- Phase 3: size / pacing grid (~243 runs) ----
        ('grid', {
            'strategy_params_long.linearDropPercent':  'around:0.05',
            'strategy_params_short.linearRisePercent': 'around:0.05',
            'portfolio.position_notional_long':        'around:0.5',
            'portfolio.position_notional_short':       'around:2.0',
            'strategy_params_long.firstBuyUSDT':       'around:0.5',
        }),
    ]
