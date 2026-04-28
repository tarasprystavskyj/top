#!/usr/bin/env python3
# LIFO STRICT / ENA narrow overnight tuning plan
# Window target: 2025-12-15 .. 2026-03-15
#
# Goal:
#   Tune only:
#   - callbackPercent
#   - linearDropPercent / linearRisePercent
#   - subSellTPPercent
#   - tpPercent
#   - trendMaLen
#
# GRID_VALUES_ARE_DELTAS = True
# All values below are deltas from the base YAML.

GRID_VALUES_ARE_DELTAS = True


def default_plan(limit_bars: int = None):
    return [
        # Stage 1: one-dimensional rays.
        ('rays', {
            'strategy_params_long.tpPercent':  [-0.06, -0.04, -0.02, 0.0, +0.02, +0.04, +0.06],
        }),
        ('rays', {
            'strategy_params_short.tpPercent': [-0.06, -0.04, -0.02, 0.0, +0.02, +0.04, +0.06],
        }),

        ('rays', {
            'strategy_params_long.subSellTPPercent':  [-0.12, -0.08, -0.04, -0.02, 0.0, +0.02, +0.04, +0.08, +0.12],
        }),
        ('rays', {
            'strategy_params_short.subSellTPPercent': [-0.12, -0.08, -0.04, -0.02, 0.0, +0.02, +0.04, +0.08, +0.12],
        }),

        ('rays', {
            'strategy_params_long.callbackPercent':  [-0.035, -0.025, -0.015, -0.0075, 0.0, +0.0075, +0.015, +0.025, +0.035],
        }),
        ('rays', {
            'strategy_params_short.callbackPercent': [-0.035, -0.025, -0.015, -0.0075, 0.0, +0.0075, +0.015, +0.025, +0.035],
        }),

        ('rays', {
            'strategy_params_long.linearDropPercent':  [-0.025, -0.015, -0.01, -0.005, 0.0, +0.005, +0.01, +0.015, +0.025],
        }),
        ('rays', {
            'strategy_params_short.linearRisePercent': [-0.025, -0.015, -0.01, -0.005, 0.0, +0.005, +0.01, +0.015, +0.025],
        }),

        ('rays', {
            'strategy_params_long.trendMaLen':  [-8, -6, -4, -2, 0, +2, +4, +6, +8, +12],
        }),
        ('rays', {
            'strategy_params_short.trendMaLen': [-8, -6, -4, -2, 0, +2, +4, +6, +8, +12],
        }),

        # Stage 2: leg-local grids. Avoid 3^10 explosion.
        ('grid', {
            'strategy_params_long.tpPercent':          'around:0.02',
            'strategy_params_long.subSellTPPercent':  'around:0.04',
            'strategy_params_long.callbackPercent':   'around:0.0125',
            'strategy_params_long.linearDropPercent': 'around:0.0075',
            'strategy_params_long.trendMaLen':        [-4, 0, +4],
        }),

        ('grid', {
            'strategy_params_short.tpPercent':          'around:0.02',
            'strategy_params_short.subSellTPPercent':   'around:0.04',
            'strategy_params_short.callbackPercent':    'around:0.0125',
            'strategy_params_short.linearRisePercent':  'around:0.0075',
            'strategy_params_short.trendMaLen':         [-4, 0, +4],
        }),

        # Stage 3: paired TP/callback/sub-sell interaction.
        ('grid', {
            'strategy_params_long.tpPercent':          'around:0.015',
            'strategy_params_short.tpPercent':         'around:0.015',
            'strategy_params_long.callbackPercent':    'around:0.01',
            'strategy_params_short.callbackPercent':   'around:0.01',
            'strategy_params_long.subSellTPPercent':   'around:0.03',
            'strategy_params_short.subSellTPPercent':  'around:0.03',
        }),

        # Stage 4: paired DCA spacing + trend memory.
        ('grid', {
            'strategy_params_long.linearDropPercent':   'around:0.0075',
            'strategy_params_short.linearRisePercent':  'around:0.0075',
            'strategy_params_long.trendMaLen':          [-4, 0, +4],
            'strategy_params_short.trendMaLen':         [-4, 0, +4],
        }),
    ]
