#!/usr/bin/env python3
GRID_VALUES_ARE_DELTAS = True

def default_plan(limit_bars: int = None):
    return [
        ('rays', {'strategy_params_short.hardBreakevenDeleveragePct': [-5.0, 0.0]}),
        ('rays', {'strategy_params_short.linearRisePercent': [0.0, +0.02]}),
    ]
