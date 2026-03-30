#!/usr/bin/env python3
# Final joint pass for BOTH legs after separate short and long optimization.
# Use the best cfg from long pass as input cfg.
GRID_VALUES_ARE_DELTAS = True

def default_plan(limit_bars: int = None):
    return [
        # --- Interaction / hedge balance first ---
        ('rays', {'portfolio.position_notional_long':  [-0.5, -0.25, 0.0, +0.25, +0.5]}),
        ('rays', {'portfolio.position_notional_short': [-3.0, -1.5, 0.0, +1.5, +3.0]}),
        ('rays', {'strategy_params_long.firstBuyUSDT':  [-0.5, -0.25, 0.0, +0.25, +0.5]}),
        ('rays', {'strategy_params_short.firstSellUSDT':[-3.0, -1.5, 0.0, +1.5, +3.0]}),

        # --- Both exits ---
        ('rays', {'strategy_params_long.tpPercent':         [-0.03, -0.015, 0.0, +0.015, +0.03]}),
        ('rays', {'strategy_params_short.tpPercent':        [-0.03, -0.015, 0.0, +0.015, +0.03]}),
        ('rays', {'strategy_params_long.subSellTPPercent':  [-0.05, -0.02, 0.0, +0.02, +0.05]}),
        ('rays', {'strategy_params_short.subSellTPPercent': [-0.05, -0.02, 0.0, +0.02, +0.05]}),
        ('rays', {'strategy_params_long.callbackPercent':   [-0.03, -0.015, 0.0, +0.015, +0.03]}),
        ('rays', {'strategy_params_short.callbackPercent':  [-0.03, -0.015, 0.0, +0.015, +0.03]}),

        # --- Both pacing ---
        ('rays', {'strategy_params_long.linearDropPercent': [-0.02, -0.01, 0.0, +0.01, +0.02]}),
        ('rays', {'strategy_params_short.linearRisePercent':[-0.02, -0.01, 0.0, +0.01, +0.02]}),

        # --- Final joint grids ---
        ('grid', {
            'portfolio.position_notional_long':      'around:0.25',
            'portfolio.position_notional_short':     'around:1.5',
            'strategy_params_long.tpPercent':        'around:0.01',
            'strategy_params_short.tpPercent':       'around:0.01',
            'strategy_params_long.callbackPercent':  'around:0.015',
            'strategy_params_short.callbackPercent': 'around:0.015',
        }),
        ('grid', {
            'strategy_params_long.linearDropPercent':  'around:0.01',
            'strategy_params_short.linearRisePercent': 'around:0.01',
            'strategy_params_long.subSellTPPercent':   'around:0.02',
            'strategy_params_short.subSellTPPercent':  'around:0.02',
        }),
    ]
