import pandas as pd

from obw_platform.strategies.breakout_avaai_full_with_universe_5 import (
    BreakoutAVAAIFull,
    pick_side,
)


def make_df(close_values):
    close = pd.Series(close_values)
    high = close + 1
    low = close - 1
    volume = pd.Series([1] * len(close))
    return pd.DataFrame({"close": close, "high": high, "low": low, "volume": volume})


def minimal_cfg():
    return {
        "side_filter": {
            "ema_fast": 2,
            "ema_slow": 3,
            "vwap_len": 2,
            "atr_len": 2,
            "adx_len": 2,
            "z_len": 3,
            "z_long_min": 0.1,
            "z_short_min": 0.1,
            "min_adx": 0,
            "htf_trend_filter": False,
            "htf_ema_fast": 2,
            "htf_ema_slow": 3,
            "votes_needed": 1,
        },
        "strategy_params": {
            "side": "BOTH",
            "tp_atr_mult": 1.0,
            "sl_atr_mult": 1.0,
            "min_qv_24h": 0.0,
            "min_qv_1h": 0.0,
            "min_atr_ratio": 0.0,
            "min_momentum_sum": 0.0,
        },
    }


def test_pick_side_long_short():
    cfg = minimal_cfg()
    df_long = make_df([100, 101, 102, 103])
    assert pick_side(df_long, cfg) == "LONG"

    df_short = make_df([103, 102, 101, 100])
    assert pick_side(df_short, cfg) == "SHORT"


def test_entry_signal_respects_pick_side():
    cfg = minimal_cfg()
    df = make_df([100, 101, 102, 103])
    row = {
        "close": df["close"].iloc[-1],
        "atr_ratio": 0.01,
        "dp6h": 0.01,
        "dp12h": 0.01,
    }
    strat = BreakoutAVAAIFull(cfg)
    sig = strat.entry_signal(0, "AAA", row, ctx={"df": df})
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_signal_respects_pick_side_short():
    cfg = minimal_cfg()
    df = make_df([103, 102, 101, 100])
    row = {
        "close": df["close"].iloc[-1],
        "atr_ratio": 0.01,
        "dp6h": -0.01,
        "dp12h": -0.01,
    }
    strat = BreakoutAVAAIFull(cfg)
    sig = strat.entry_signal(0, "AAA", row, ctx={"df": df})
    assert sig is not None
    assert sig.side == "SHORT"


def test_pick_side_none_when_votes_insufficient():
    cfg = minimal_cfg()
    cfg["side_filter"]["votes_needed"] = 5
    df = make_df([100, 101, 102, 103])
    assert pick_side(df, cfg) is None


def test_entry_signal_none_when_votes_insufficient():
    cfg = minimal_cfg()
    cfg["side_filter"]["votes_needed"] = 5
    df = make_df([100, 101, 102, 103])
    row = {
        "close": df["close"].iloc[-1],
        "atr_ratio": 0.01,
        "dp6h": 0.01,
        "dp12h": 0.01,
    }
    strat = BreakoutAVAAIFull(cfg)
    assert strat.entry_signal(0, "AAA", row, ctx={"df": df}) is None
