#!/usr/bin/env python3
# tuner_plan_cryptomine_1m_ddweek_smoke.py
# Very narrow smoke plan (fast check ~minutes). Uses DELTAS around baseline.
# Goal: verify tuner+backtester pipeline works and produce sane variation.
#
# NOTE: drawdown-duration filtering is handled in the tuner (not here).
GRID_VALUES_ARE_DELTAS = True

def default_plan(limit_bars: int = None):
    return [
        # Exits (tiny deltas)
        ("rays", {"tp":    [-0.05, 0.0, +0.05]}),
        ("rays", {"subtp": [-0.10, 0.0, +0.10]}),
        ("rays", {"cb":    [-0.03, 0.0, +0.03]}),

        # DCA pacing / safety (tiny)
        ("rays", {"lin-drop": [-0.10, 0.0, +0.10]}),
        ("rays", {"fills":    [-1, 0, +1]}),
        ("rays", {"sigwin":   [-2, 0, +2]}),
        ("rays", {"winbars":  [-1, 0, +1]}),

        # Small focused grid around best
        ("grid", {
            "tp":      "around:0.03",
            "subtp":   "around:0.05",
            "cb":      "around:0.02",
            "lin-drop":"around:0.05",
        }),
    ]
