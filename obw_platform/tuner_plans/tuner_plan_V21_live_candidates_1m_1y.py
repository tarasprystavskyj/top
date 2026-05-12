#!/usr/bin/env python3
"""V21 live-candidate tuning plan for 1m one-year symbol caches.

This is intentionally conservative: start from the confirmed V21 static9p38
config and tune exits, partial exits, spacing, and exposure caps in narrow
ranges. It is shared by FREEDOMMONEY, MAXXING, and CHECK runner scripts.
"""

GRID_VALUES_ARE_DELTAS = True


def default_plan(limit_bars: int = None):
    return [
        ("rays", {
            "strategy_params_long.tpPercent": [-0.10, -0.07, -0.05, -0.03, -0.015, 0.0, 0.015, 0.03, 0.05, 0.08],
        }),
        ("rays", {
            "strategy_params_short.tpPercent": [-0.08, -0.06, -0.04, -0.025, -0.01, 0.0, 0.01, 0.025, 0.04, 0.06],
        }),
        ("rays", {
            "strategy_params_long.subSellTPPercent": [-0.14, -0.12, -0.10, -0.08, -0.05, -0.025, 0.0, 0.025, 0.05],
        }),
        ("rays", {
            "strategy_params_short.subSellTPPercent": [-0.12, -0.10, -0.07, -0.04, -0.02, 0.0, 0.02, 0.04, 0.07],
        }),
        ("rays", {
            "strategy_params_long.callbackPercent": [-0.03, -0.02, -0.01, -0.005, 0.0, 0.005, 0.01, 0.02],
        }),
        ("rays", {
            "strategy_params_short.callbackPercent": [-0.04, -0.025, -0.015, -0.0075, 0.0, 0.0075, 0.015, 0.025],
        }),
        ("rays", {
            "strategy_params_long.linearDropPercent": [-0.025, -0.018, -0.012, -0.006, 0.0, 0.006, 0.012, 0.02, 0.03],
        }),
        ("rays", {
            "strategy_params_short.linearRisePercent": [-0.07, -0.05, -0.035, -0.02, -0.01, 0.0, 0.01, 0.02, 0.035, 0.05],
        }),
        ("rays", {
            "strategy_params_long.maxLongInvestPct": [-0.55, -0.40, -0.25, -0.10, 0.0, 0.10, 0.20, 0.35],
        }),
        ("rays", {
            "strategy_params_short.maxShortInvestPct": [-0.55, -0.40, -0.25, -0.10, 0.0, 0.10, 0.20, 0.35],
        }),
        ("grid", {
            "strategy_params_long.tpPercent": "around:0.02",
            "strategy_params_long.subSellTPPercent": "around:0.03",
            "strategy_params_long.callbackPercent": "around:0.0075",
            "strategy_params_long.linearDropPercent": "around:0.008",
        }),
        ("grid", {
            "strategy_params_short.tpPercent": "around:0.0175",
            "strategy_params_short.subSellTPPercent": "around:0.03",
            "strategy_params_short.callbackPercent": "around:0.0075",
            "strategy_params_short.linearRisePercent": "around:0.012",
        }),
        ("grid", {
            "strategy_params_long.maxLongInvestPct": "around:0.15",
            "strategy_params_short.maxShortInvestPct": "around:0.15",
            "strategy_params_long.tpPercent": "around:0.0125",
            "strategy_params_short.tpPercent": "around:0.0125",
        }),
    ]
