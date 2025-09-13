# tuner_plan_avaai_slip16.py
# План під slippage_per_side = 0.0016 (0.16%/side).
# Орієнтири: більший TP, трохи ширший SL, сильніші фільтри ATR/ліквідності,
# менший top-n. Працює з auto_tuner_rays2grid_v3_fix.py (RAYS→GRID).

def _seq(start, stop, step):
    vals, x = [], float(start)
    st = float(step) if step else (abs(stop-start)/10.0 or 1.0)
    if start <= stop:
        while x <= float(stop) + 1e-12:
            vals.append(round(x, 10)); x += st 
    else:
        while x >= float(stop) - 1e-12:
            vals.append(round(x, 10)); x -= st
    return vals

def default_plan(limit_bars: int = None):
    # Швидка перевірка коли вікно зовсім мале
    if limit_bars and limit_bars < 400:
        return [
            ("rays", {"strategy_params.tp_atr_mult": [3.9,4.2,4.4]}),
            ("rays", {"strategy_params.sl_atr_mult": [1.00,1.04,1.08,1.12]}),
            ("rays", {"min-mom": [0.022,0.024,0.026]}),
            ("rays", {"min-atr": [0.0006,0.0008,0.0010]}),
            ("rays", {"strategy_params.min_qv_24h": [300000,500000,800000]}),
            ("rays", {"strategy_params.min_qv_1h":  [12000,15000,20000]}),
            ("rays", {"top-n": [6,7,8]}),
            ("rays", {"side": ["LONG","BOTH"]}),
            ("rays", {"open_on_heat": [False]}),
        ]

    # Основний горизонт (≈5000 барів)
    rays = [
        # 1) Геометрія виходів (TP/SL) — змістили вгору через friction ~0.52%/угода
        ("rays", {"strategy_params.tp_atr_mult": _seq(3.8, 4.6, 0.1)}),
        ("rays", {"strategy_params.sl_atr_mult": _seq(0.98, 1.14, 0.02)}),

        # 2) Волатильність/моментум — сильніші фільтри
        ("rays", {"min-atr": _seq(0.0006, 0.0014, 0.0002)}),
        ("rays", {"min-mom": _seq(0.022, 0.030, 0.001)}),

        # 3) Ліквідність — підвищили, щоб зменшувати фактичне сліпедж/ре-квоти
        ("rays", {"strategy_params.min_qv_24h": [300000, 500000, 800000, 1200000]}),
        ("rays", {"strategy_params.min_qv_1h":  [12000, 15000, 20000, 30000]}),

        # 4) Ширина юніверсу та сторона
        ("rays", {"top-n": [6,7,8,9]}),
        ("rays", {"side": ["LONG","BOTH"]}),

        # 5) Сайзинг (може впливати на фактичний сліпедж; диверсифікація)
        ("rays", {"position_notional": [40, 50, 60, 80]}),
        ("rays", {"portfolio.max_notional_frac": [0.6, 0.7, 0.8]}),

        # 6) Довжина базового вікна (якщо використовується в страті)
        ("rays", {"strategy_params.length": [10,12,14,16]}),

        # Фіксація класичного входу
        ("rays", {"open_on_heat": [False]}),
    ]

    # GRID-1: середнє уточнення навколо кращої комбінації
    grid1 = ("grid", {
        "strategy_params.tp_atr_mult": "around:0.10",
        "strategy_params.sl_atr_mult": "around:0.02",
        "min-atr": "around:0.0001",
        "min-mom": "around:0.001",
        "strategy_params.min_qv_24h": "around:100000",
        "strategy_params.min_qv_1h":  "around:5000",
        "top-n": "around:1",
        "position_notional": "around:10",
        "portfolio.max_notional_frac": "around:0.05",
        "strategy_params.length": "around:2",
        "open_on_heat": [False],
    })

    # GRID-2: тонке уточнення
    grid2 = ("grid", {
        "strategy_params.tp_atr_mult": "around:0.05",
        "strategy_params.sl_atr_mult": "around:0.01",
        "min-atr": "around:0.00005",
        "min-mom": "around:0.0005",
        "strategy_params.min_qv_24h": "around:50000",
        "strategy_params.min_qv_1h":  "around:2500",
        "top-n": "around:1",
        "position_notional": "around:5",
        "portfolio.max_notional_frac": "around:0.02",
        "strategy_params.length": "around:1",
        "open_on_heat": [False],
    })

    return rays + [grid1, grid2]
