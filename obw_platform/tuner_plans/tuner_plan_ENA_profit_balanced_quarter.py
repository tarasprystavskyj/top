#!/usr/bin/env python3
GRID_VALUES_ARE_DELTAS = True

def default_plan(limit_bars: int = None):
    return [
        ('rays', {'strategy_params_long.tpPercent':         [-0.03, -0.015, 0.0, +0.015, +0.03]}),
        ('rays', {'strategy_params_short.tpPercent':        [-0.03, -0.015, 0.0, +0.015, +0.03]}),
        ('rays', {'strategy_params_long.subSellTPPercent':  [-0.06, -0.03, 0.0, +0.03, +0.06]}),
        ('rays', {'strategy_params_short.subSellTPPercent': [-0.06, -0.03, 0.0, +0.03, +0.06]}),
        ('rays', {'strategy_params_long.callbackPercent':   [-0.04, -0.02, 0.0, +0.02, +0.04]}),
        ('rays', {'strategy_params_short.callbackPercent':  [-0.04, -0.02, 0.0, +0.02, +0.04]}),
        ('rays', {'strategy_params_long.linearDropPercent': [-0.02, -0.01, 0.0, +0.01, +0.02]}),
        ('rays', {'strategy_params_short.linearRisePercent':[-0.02, -0.01, 0.0, +0.01, +0.02]}),
        ('rays', {'strategy_params_long.mult2': [-0.20, -0.10, 0.0, +0.10]}),
        ('rays', {'strategy_params_short.mult2': [-0.20, -0.10, 0.0, +0.10]}),
        ('rays', {'strategy_params_long.mult4': [-0.30, -0.15, 0.0, +0.15]}),
        ('rays', {'strategy_params_short.mult4': [-0.30, -0.15, 0.0, +0.15]}),
        ('grid', {
            'strategy_params_long.tpPercent':          'around:0.01',
            'strategy_params_short.tpPercent':         'around:0.01',
            'strategy_params_long.callbackPercent':    'around:0.015',
            'strategy_params_short.callbackPercent':   'around:0.015',
            'strategy_params_long.linearDropPercent':  'around:0.01',
            'strategy_params_short.linearRisePercent': 'around:0.01',
        }),
    ]
