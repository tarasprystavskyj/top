#!/usr/bin/env python3
# tuner_plan_cryptomine_1m_night5h.py
# Night plan (~5 hours target depending on jobs/backtester speed).
# Designed for 200d / 288000 bars. Moderate breadth, then focused grid.
#
GRID_VALUES_ARE_DELTAS = True

def default_plan(limit_bars: int = None):
    return [
        # ---- Phase 1: stabilize exits ----
        ("rays", {"tp":    [ -0.20, -0.10, 0.0, +0.10, +0.20 ]}),   # deltas
        ("rays", {"subtp": [ -0.20, -0.10, 0.0, +0.10, +0.20 ]}),
        ("rays", {"cb":    [ -0.10, -0.05, 0.0, +0.05, +0.10 ]}),

        # ---- Phase 2: DCA pacing / safety ----
        ("rays", {"lin-drop": [ -0.30, -0.15, 0.0, +0.15, +0.30 ]}),
        ("rays", {"fills":    [ -2, -1, 0, +1, +2 ]}),
        ("rays", {"sigwin":   [ -6, -3, 0, +3, +6 ]}),
        ("rays", {"winbars":  [ -3, -1, 0, +1, +3 ]}),

        # ---- Phase 3: early ladder shape (small) ----
        ("rays", {"d1": [ -0.05, -0.02, 0.0, +0.02, +0.05 ]}),
        ("rays", {"d2": [ -0.05, -0.02, 0.0, +0.02, +0.05 ]}),
        ("rays", {"m2": [ -0.30, -0.15, 0.0, +0.15, +0.30 ]}),
        ("rays", {"m4": [ -0.50, -0.25, 0.0, +0.25, +0.50 ]}),

        # ---- Phase 4: focused grid around the best found so far ----
        ("grid", {
            "tp":      "around:0.08",
            "subtp":   "around:0.08",
            "cb":      "around:0.03",
            "lin-drop":"around:0.08",
            "fills":   "around:1",
        }),
    ]
