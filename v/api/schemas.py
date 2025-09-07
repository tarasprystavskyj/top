"""JSON schemas used by the web API."""

BREAKOUT_AVAAI_FULL_5M_CONFIG_SCHEMA = {
    "title": "Breakout AVAAI Full (5m) Config",
    "type": "object",
    "properties": {
        "side": {"type": "string", "enum": ["LONG", "SHORT", "BOTH"], "default": "BOTH"},
        "timeframe": {"type": "string", "enum": ["1m", "5m", "15m", "1h"], "default": "5m"},
        "open_on_heat": {"type": "boolean", "default": False},
        "open_heat_min": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.35},
        "entry": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["market", "limit_aggressive"], "default": "market"},
                "offset_ticks": {"type": "integer", "minimum": 0, "default": 1},
                "chase_steps": {"type": "integer", "minimum": 0, "maximum": 5, "default": 2},
                "chase_delay_ms": {"type": "integer", "minimum": 50, "maximum": 2000, "default": 120},
                "chase_extra_ticks": {"type": "integer", "minimum": 0, "maximum": 5, "default": 1},
            },
        },
        "strategy_params": {
            "type": "object",
            "properties": {
                "top_n": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
                "min_atr_ratio": {"type": "number", "minimum": 0, "maximum": 0.2, "default": 0.008},
                "min_momentum_sum": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.003},
                "adx_threshold": {"type": "number", "minimum": 0, "maximum": 50, "default": 22},
                "macd_filter": {"type": "integer", "minimum": 0, "maximum": 1, "default": 1},
                "tp_atr_mult": {"type": "number", "minimum": 0, "maximum": 10, "default": 4.1},
                "sl_atr_mult": {"type": "number", "minimum": 0, "maximum": 10, "default": 1.04},
                "min_qv_24h": {"type": "number", "minimum": 0, "default": 200000},
                "min_qv_1h": {"type": "number", "minimum": 0, "default": 10000},
            },
        },
        "portfolio": {
            "type": "object",
            "properties": {
                "initial_equity": {"type": "number", "default": 100},
                "position_notional": {"type": "number", "default": 2.2},
                "notional_max": {"type": "number", "default": 5.0},
                "max_notional_frac": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.9},
                "fee_rate": {"type": "number", "default": 0.001},
                "slippage_per_side": {"type": "number", "default": 0.0008},
            },
            "required": ["position_notional"],
        },
        "universe_file": {"type": "string", "default": "universe_v5_avaai_5m_5000.txt"},
    },
    "required": ["strategy_params", "portfolio", "universe_file"],
}
