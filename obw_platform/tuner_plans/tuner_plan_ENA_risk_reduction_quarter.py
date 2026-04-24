#!/usr/bin/env python3
# ENA risk-reduction tuner plan for auto_tuner_dual_fast_pack.py
# Designed for multi-day server runs. Use deltas around current YAML values.
GRID_VALUES_ARE_DELTAS = True

def default_plan(limit_bars: int = None):
    return [
        # 1) Trigger deleverage earlier; most direct MTM DD lever.
        ('rays', {'strategy_params_long.hardBreakevenDeleveragePct':  [-20.0, -12.5, -7.5, 0.0, +7.5]}),
        ('rays', {'strategy_params_short.hardBreakevenDeleveragePct': [-20.0, -12.5, -7.5, 0.0, +7.5]}),

        # 2) Widen DCA grid. Usually reduces inventory accumulation and MTM DD.
        ('rays', {'strategy_params_long.linearDropPercent':  [-0.02, 0.0, +0.02, +0.04, +0.06]}),
        ('rays', {'strategy_params_short.linearRisePercent': [-0.02, 0.0, +0.02, +0.04, +0.06]}),
        ('grid', {
            'strategy_params_long.drop1':  [0.0, +0.03, +0.06],
            'strategy_params_long.drop2':  [0.0, +0.03, +0.06],
            'strategy_params_short.rise1': [0.0, +0.03, +0.06],
            'strategy_params_short.rise2': [0.0, +0.03, +0.06],
        }),
        ('grid', {
            'strategy_params_long.drop3':  [0.0, +0.05, +0.10],
            'strategy_params_long.drop4':  [0.0, +0.05, +0.10],
            'strategy_params_short.rise3': [0.0, +0.05, +0.10],
            'strategy_params_short.rise4': [0.0, +0.05, +0.10],
        }),

        # 3) Reduce martingale convexity.
        ('rays', {'strategy_params_long.mult2':  [-0.30, -0.20, -0.10, 0.0]}),
        ('rays', {'strategy_params_short.mult2': [-0.30, -0.20, -0.10, 0.0]}),
        ('rays', {'strategy_params_long.mult4':  [-0.60, -0.40, -0.20, 0.0]}),
        ('rays', {'strategy_params_short.mult4': [-0.60, -0.40, -0.20, 0.0]}),
        ('rays', {'strategy_params_long.mult5':  [-1.00, -0.70, -0.40, 0.0]}),
        ('rays', {'strategy_params_short.mult5': [-1.00, -0.70, -0.40, 0.0]}),

        # 4) Exit sooner at the warehouse level; can reduce stuck-leg age but may cut PnL/trade.
        ('rays', {'strategy_params_long.tpPercent':  [-0.06, -0.04, -0.02, 0.0, +0.02]}),
        ('rays', {'strategy_params_short.tpPercent': [-0.06, -0.04, -0.02, 0.0, +0.02]}),
        ('rays', {'strategy_params_long.callbackPercent':  [-0.04, -0.02, 0.0, +0.02]}),
        ('rays', {'strategy_params_short.callbackPercent': [-0.04, -0.02, 0.0, +0.02]}),

        # 5) Exact last-lot partial exit aggressiveness.
        ('rays', {'strategy_params_long.subSellTPPercent':  [-0.08, -0.04, -0.02, 0.0, +0.02]}),
        ('rays', {'strategy_params_short.subSellTPPercent': [-0.08, -0.04, -0.02, 0.0, +0.02]}),

        # 6) Cap burstiness. Keep as integer values through absolute mode by using deltas around current.
        ('rays', {'strategy_params_long.maxFillsPerBar':  [-4, -3, -2, 0]}),
        ('rays', {'strategy_params_short.maxFillsPerBar': [-4, -3, -2, 0]}),
        ('rays', {'strategy_params_long.maxOrdersPer3Min':  [-8, -6, -4, 0]}),
        ('rays', {'strategy_params_short.maxOrdersPer3Min': [-8, -6, -4, 0]}),

        # 7) Reduce trend-adaptive maximum exposure.
        ('rays', {'strategy_params_long.maxLongInvestPct':   [-0.60, -0.40, -0.20, 0.0]}),
        ('rays', {'strategy_params_short.maxShortInvestPct': [-0.60, -0.40, -0.20, 0.0]}),

        # 8) Local polish around best-so-far.
        ('grid', {
            'strategy_params_long.hardBreakevenDeleveragePct':  'around:5',
            'strategy_params_short.hardBreakevenDeleveragePct': 'around:5',
            'strategy_params_long.linearDropPercent':           'around:0.01',
            'strategy_params_short.linearRisePercent':          'around:0.01',
        }),
        ('grid', {
            'strategy_params_long.tpPercent':          'around:0.01',
            'strategy_params_short.tpPercent':         'around:0.01',
            'strategy_params_long.subSellTPPercent':   'around:0.02',
            'strategy_params_short.subSellTPPercent':  'around:0.02',
        }),
    ]
