#!/usr/bin/env python3
# Optimize SHORT first from current DUAL_final_best.yaml
# Use with: --cfg DUAL_final_best.yaml
GRID_VALUES_ARE_DELTAS = True

def default_plan(limit_bars: int = None):
    return [
        # --- Short exits / sizing ---
        ('rays', {'strategy_params_short.tpPercent':        [-0.06, -0.03, 0.0, +0.03, +0.06]}),
        ('rays', {'strategy_params_short.subSellTPPercent': [0.00, +0.03, +0.06, +0.10, +0.15]}),
        ('rays', {'strategy_params_short.callbackPercent':  [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_short.linearRisePercent':[-0.03, 0.0, +0.03, +0.06, +0.10]}),
        ('rays', {'portfolio.position_notional_short':      [-6.0, -3.0, 0.0, +3.0, +6.0]}),
        ('rays', {'strategy_params_short.firstSellUSDT':    [-6.0, -3.0, 0.0, +3.0, +6.0]}),

        # --- Short ladder shape: rises ---
        ('rays', {'strategy_params_short.rise1': [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_short.rise2': [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ('rays', {'strategy_params_short.rise3': [-0.10, -0.05, 0.0, +0.05, +0.10]}),
        ('rays', {'strategy_params_short.rise4': [-0.10, -0.05, 0.0, +0.05, +0.10]}),
        ('rays', {'strategy_params_short.rise5': [-0.10, -0.05, 0.0, +0.05, +0.10]}),

        # --- Short ladder shape: multipliers ---
        ('rays', {'strategy_params_short.mult2': [-0.40, -0.20, 0.0, +0.20, +0.40]}),
        ('rays', {'strategy_params_short.mult3': [-0.40, -0.20, 0.0, +0.20, +0.40]}),
        ('rays', {'strategy_params_short.mult4': [-0.60, -0.30, 0.0, +0.30, +0.60]}),
        ('rays', {'strategy_params_short.mult5': [-1.00, -0.50, 0.0, +0.50, +1.00]}),

        # --- Focused short grids ---
        ('grid', {
            'strategy_params_short.tpPercent':         'around:0.02',
            'strategy_params_short.subSellTPPercent':  'around:0.04',
            'strategy_params_short.callbackPercent':   'around:0.03',
            'strategy_params_short.linearRisePercent': 'around:0.03',
        }),
        ('grid', {
            'strategy_params_short.rise1': 'around:0.03',
            'strategy_params_short.rise2': 'around:0.03',
            'strategy_params_short.rise3': 'around:0.04',
            'strategy_params_short.mult2': 'around:0.20',
            'strategy_params_short.mult4': 'around:0.30',
        }),
    ]
