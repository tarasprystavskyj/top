#!/usr/bin/env python3
# Optimize LONG second, using the best cfg from short pass as input cfg.
GRID_VALUES_ARE_DELTAS = True

def default_plan(limit_bars: int = None):
    return [
        # --- Long exits / sizing ---
        ('rays', {'strategy_params_long.tpPercent':         [-0.06, -0.03, 0.0, +0.03, +0.06]}),
        ('rays', {'strategy_params_long.subSellTPPercent':  [-0.12, -0.06, 0.0, +0.06, +0.12]}),
        ('rays', {'strategy_params_long.callbackPercent':   [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_long.linearDropPercent': [-0.05, -0.02, 0.0, +0.02, +0.05]}),
        ('rays', {'portfolio.position_notional_long':       [-1.0, -0.5, 0.0, +0.5, +1.0]}),
        ('rays', {'strategy_params_long.firstBuyUSDT':      [-1.0, -0.5, 0.0, +0.5, +1.0]}),

        # --- Long ladder shape: drops ---
        ('rays', {'strategy_params_long.drop1': [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_long.drop2': [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_long.drop3': [-0.10, -0.05, 0.0, +0.05, +0.10]}),
        ('rays', {'strategy_params_long.drop4': [-0.10, -0.05, 0.0, +0.05, +0.10]}),
        ('rays', {'strategy_params_long.drop5': [-0.10, -0.05, 0.0, +0.05, +0.10]}),

        # --- Long ladder shape: multipliers ---
        ('rays', {'strategy_params_long.mult2': [-0.40, -0.20, 0.0, +0.20, +0.40]}),
        ('rays', {'strategy_params_long.mult3': [-0.40, -0.20, 0.0, +0.20, +0.40]}),
        ('rays', {'strategy_params_long.mult4': [-0.60, -0.30, 0.0, +0.30, +0.60]}),
        ('rays', {'strategy_params_long.mult5': [-1.00, -0.50, 0.0, +0.50, +1.00]}),

        # --- Focused long grids ---
        ('grid', {
            'strategy_params_long.tpPercent':         'around:0.02',
            'strategy_params_long.subSellTPPercent':  'around:0.04',
            'strategy_params_long.callbackPercent':   'around:0.03',
            'strategy_params_long.linearDropPercent': 'around:0.02',
        }),
        ('grid', {
            'strategy_params_long.drop1': 'around:0.03',
            'strategy_params_long.drop2': 'around:0.03',
            'strategy_params_long.drop3': 'around:0.04',
            'strategy_params_long.mult2': 'around:0.20',
            'strategy_params_long.mult4': 'around:0.30',
        }),
    ]
