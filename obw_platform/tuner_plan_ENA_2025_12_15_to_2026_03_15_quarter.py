#!/usr/bin/env python3
# ENA / quarter robustness plan
# Intended window: 2025-12-15 .. 2026-03-15
# Bias: robustness across changing regimes; prefer stable configs with 0 margin calls.
GRID_VALUES_ARE_DELTAS = True

def default_plan(limit_bars: int = None):
    return [
        ('rays', {'strategy_params_long.tpPercent':         [-0.03, -0.015, 0.0, +0.015, +0.03]}),
        ('rays', {'strategy_params_short.tpPercent':        [-0.03, -0.015, 0.0, +0.015, +0.03]}),
        ('rays', {'strategy_params_long.subSellTPPercent':  [-0.06, -0.03, 0.0, +0.03, +0.06]}),
        ('rays', {'strategy_params_short.subSellTPPercent': [-0.06, -0.03, 0.0, +0.03, +0.06]}),
        ('rays', {'strategy_params_long.callbackPercent':   [-0.04, -0.02, 0.0, +0.02, +0.04]}),
        ('rays', {'strategy_params_short.callbackPercent':  [-0.04, -0.02, 0.0, +0.02, +0.04]}),
        ('rays', {'strategy_params_long.firstBuyUSDT':      [-0.75, -0.35, 0.0, +0.35, +0.75]}),
        ('rays', {'strategy_params_short.firstSellUSDT':    [-0.75, -0.35, 0.0, +0.35, +0.75]}),
        ('rays', {'strategy_params_long.linearDropPercent': [-0.02, -0.01, 0.0, +0.01, +0.02]}),
        ('rays', {'strategy_params_short.linearRisePercent':[-0.02, -0.01, 0.0, +0.01, +0.02]}),
        ('rays', {'strategy_params_long.drop1': [-0.03, -0.015, 0.0, +0.015, +0.03]}),
        ('rays', {'strategy_params_long.drop2': [-0.03, -0.015, 0.0, +0.015, +0.03]}),
        ('rays', {'strategy_params_long.drop3': [-0.05, -0.025, 0.0, +0.025, +0.05]}),
        ('rays', {'strategy_params_short.rise1': [-0.03, -0.015, 0.0, +0.015, +0.03]}),
        ('rays', {'strategy_params_short.rise2': [-0.03, -0.015, 0.0, +0.015, +0.03]}),
        ('rays', {'strategy_params_short.rise3': [-0.05, -0.025, 0.0, +0.025, +0.05]}),
        ('rays', {'strategy_params_long.mult2': [-0.20, -0.10, 0.0, +0.10, +0.20]}),
        ('rays', {'strategy_params_short.mult2': [-0.20, -0.10, 0.0, +0.10, +0.20]}),
        ('rays', {'strategy_params_long.mult4': [-0.30, -0.15, 0.0, +0.15, +0.30]}),
        ('rays', {'strategy_params_short.mult4': [-0.30, -0.15, 0.0, +0.15, +0.30]}),
        ('grid', {
            'strategy_params_long.tpPercent':          'around:0.01',
            'strategy_params_short.tpPercent':         'around:0.01',
            'strategy_params_long.callbackPercent':    'around:0.015',
            'strategy_params_short.callbackPercent':   'around:0.015',
            'strategy_params_long.linearDropPercent':  'around:0.01',
            'strategy_params_short.linearRisePercent': 'around:0.01',
        }),
        ('grid', {
            'strategy_params_long.firstBuyUSDT':       'around:0.35',
            'strategy_params_short.firstSellUSDT':     'around:0.35',
            'strategy_params_long.subSellTPPercent':   'around:0.02',
            'strategy_params_short.subSellTPPercent':  'around:0.02',
            'strategy_params_long.mult2':              'around:0.10',
            'strategy_params_short.mult2':             'around:0.10',
        }),
    ]
