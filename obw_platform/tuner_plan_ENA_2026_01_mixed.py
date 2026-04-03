#!/usr/bin/env python3
# ENA / mixed January regime
# Intended window: 2026-01-01 .. 2026-01-31
# Bias: roughly half up / half down, so both legs should stay balanced.
GRID_VALUES_ARE_DELTAS = True

def default_plan(limit_bars: int = None):
    return [
        ('rays', {'strategy_params_long.tpPercent':         [-0.04, -0.02, 0.0, +0.02, +0.04]}),
        ('rays', {'strategy_params_short.tpPercent':        [-0.04, -0.02, 0.0, +0.02, +0.04]}),
        ('rays', {'strategy_params_long.subSellTPPercent':  [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_short.subSellTPPercent': [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_long.callbackPercent':   [-0.05, -0.025, 0.0, +0.025, +0.05]}),
        ('rays', {'strategy_params_short.callbackPercent':  [-0.05, -0.025, 0.0, +0.025, +0.05]}),
        ('rays', {'strategy_params_long.linearDropPercent': [-0.03, -0.015, 0.0, +0.015, +0.03]}),
        ('rays', {'strategy_params_short.linearRisePercent':[-0.03, -0.015, 0.0, +0.015, +0.03]}),
        ('rays', {'strategy_params_long.firstBuyUSDT':      [-1.0, -0.5, 0.0, +0.5, +1.0]}),
        ('rays', {'strategy_params_short.firstSellUSDT':    [-1.0, -0.5, 0.0, +0.5, +1.0]}),
        ('rays', {'strategy_params_long.drop1': [-0.05, -0.025, 0.0, +0.025, +0.05]}),
        ('rays', {'strategy_params_long.drop2': [-0.05, -0.025, 0.0, +0.025, +0.05]}),
        ('rays', {'strategy_params_short.rise1': [-0.05, -0.025, 0.0, +0.025, +0.05]}),
        ('rays', {'strategy_params_short.rise2': [-0.05, -0.025, 0.0, +0.025, +0.05]}),
        ('rays', {'strategy_params_long.mult2': [-0.25, -0.125, 0.0, +0.125, +0.25]}),
        ('rays', {'strategy_params_short.mult2': [-0.25, -0.125, 0.0, +0.125, +0.25]}),
        ('rays', {'strategy_params_long.mult4': [-0.40, -0.20, 0.0, +0.20, +0.40]}),
        ('rays', {'strategy_params_short.mult4': [-0.40, -0.20, 0.0, +0.20, +0.40]}),
        ('grid', {
            'strategy_params_long.tpPercent':         'around:0.015',
            'strategy_params_short.tpPercent':        'around:0.015',
            'strategy_params_long.callbackPercent':   'around:0.02',
            'strategy_params_short.callbackPercent':  'around:0.02',
            'strategy_params_long.firstBuyUSDT':      'around:0.5',
            'strategy_params_short.firstSellUSDT':    'around:0.5',
        }),
        ('grid', {
            'strategy_params_long.linearDropPercent':  'around:0.01',
            'strategy_params_short.linearRisePercent': 'around:0.01',
            'strategy_params_long.subSellTPPercent':   'around:0.03',
            'strategy_params_short.subSellTPPercent':  'around:0.03',
        }),
    ]
