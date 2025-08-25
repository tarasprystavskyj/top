# tuner_plan.py
def default_plan(limit_bars: int = None):
    """
    Returns a list of steps for the auto tuner.
    Each step is ("rays"|"grid", {param: values or "around:step"}).
    """

    # Короткі вікна (<=60) – мʼякші фільтри
    if limit_bars is not None and limit_bars <= 60:
        return [
            ("rays", {"min-atr": [0]}),
            ("rays", {"min-mom": [-0.01, -0.005, 0.0, 0.01]}),
            ("rays", {"side": ["LONG"]}),
            ("rays", {"top-n": [6, 8, 10]}),
            ("rays", {"strategy_params.length": [6, 7, 8]}),
            ("rays", {"tp": [2.8, 3.0, 3.2, 3.4]}),
            ("rays", {"sl": [1.0, 1.06, 1.10]}),
            ("rays", {"strategy_params.macd_filter": [0]}),
            ("rays", {"strategy_params.volume_length": [0]}),
            ("rays", {"strategy_params.adx_length": [0]}),
            ("rays", {"strategy_params.adx_threshold": [0]}),
            ("rays", {"strategy_params.atr_threshold_ratio": [0.0]}),
            ("grid", {
                "tp": "around:0.10",
                "sl": "around:0.02",
                "min-mom": "around:0.005",
                "position_notional": "around:10",
                "top-n": "around:2",
                "strategy_params.length": "around:1"
            }),
        ]

    # Довга історія (>=4500), у т.ч. 5000 барів
    if limit_bars is not None and limit_bars >= 4500:
        return [
            # === COARSE RAYS ===
            ("rays", {"tp": _fr(3.0, 4.2, 0.10)}),
            ("rays", {"sl": _fr(1.02, 1.20, 0.02)}),

            ("rays", {"strategy_params.length": [8, 10, 12, 14, 16, 20, 24]}),

            ("rays", {"min-mom": _fr(0.016, 0.030, 0.001)}),
            ("rays", {"min-atr": [0, 0.0005, 0.0008, 0.0010, 0.0015, 0.0020]}),

            ("rays", {"strategy_params.atr_threshold_ratio": [0.0, 0.0010, 0.0015, 0.0020, 0.0030, 0.0040]}),
            ("rays", {"strategy_params.macd_filter": [0, 1]}),
            ("rays", {"strategy_params.volume_length": [0, 20, 50]}),
            ("rays", {"strategy_params.adx_length": [0, 14, 20]}),
            ("rays", {"strategy_params.adx_threshold": [0, 12, 15, 20]}),

            ("rays", {"side": ["LONG", "BOTH"]}),
            ("rays", {"top-n": [8, 10, 12]}),
            ("rays", {"position_notional": _fr(60, 120, 10)}),

            # === GRID #1 (звуження) ===
            ("grid", {
                "tp": "around:0.06",
                "sl": "around:0.02",
                "min-mom": "around:0.001",
                "min-atr": "around:0.0002",
                "position_notional": "around:10",
                "top-n": "around:2",
                "strategy_params.length": "around:2",
                "strategy_params.atr_threshold_ratio": "around:0.0003",
                "strategy_params.adx_threshold": "around:3",
            }),

            # === GRID #2 (файн-тюн) ===
            ("grid", {
                "tp": "around:0.02",
                "sl": "around:0.01",
                "min-mom": "around:0.0005",
                "min-atr": "around:0.0001",
                "position_notional": "around:5",
                "top-n": "around:1",
                "strategy_params.length": "around:1",
                "strategy_params.atr_threshold_ratio": "around:0.0001",
                "strategy_params.adx_threshold": "around:2",
            }),
        ]

    # Базовий (≈1440) якщо не вказано інше
    return [
        ("rays", {"tp": _fr(3.25, 3.75, 0.05)}),
        ("rays", {"sl": _fr(1.02, 1.12, 0.02)}),
        ("rays", {"min-mom": _fr(0.019, 0.024, 0.001)}),
        ("rays", {"min-atr": [0, 0.0005, 0.0008, 0.0010, 0.0012]}),
        ("rays", {"side": ["LONG", "BOTH"]}),
        ("rays", {"top-n": [10, 12, 14, 16]}),
        ("rays", {"position_notional": _fr(70, 130, 10)}),
        ("grid", {
            "tp": "around:0.02",
            "sl": "around:0.02",
            "min-mom": "around:0.001",
            "min-atr": "around:0.0002",
            "position_notional": "around:10",
            "top-n": "around:2"
        }),
    ]

# helper
def _fr(start, stop, step):
    vals, x = [], float(start)
    while x <= float(stop) + 1e-12:
        vals.append(round(x, 10))
        x += float(step)
    return vals
