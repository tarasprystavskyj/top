
import pandas as pd
from .engine.data import load_cache, build_md_slice
from .engine.portfolio import Portfolio

def is_open_hour(t, open_hour_kyiv: int, kyiv_offset_hours: int) -> bool:
    utc_hour = (open_hour_kyiv - kyiv_offset_hours) % 24
    tt = t if t.tzinfo else pd.Timestamp(t, tz="UTC")
    return (tt.hour == utc_hour) and (tt.minute == 0)

def run_backtest(cache_db: str, strategy_cls, strategy_cfg: dict, portfolio_cfg: dict, session_cfg: dict, limit_bars: int=0):
    dfs, all_times = load_cache(cache_db)
    if limit_bars and limit_bars>0:
        all_times = all_times[-int(limit_bars):]
    strat = strategy_cls(strategy_cfg)
    pf = Portfolio(portfolio_cfg)
    open_hour_kyiv = int(session_cfg.get("open_hour_kyiv", 2))
    kyiv_offset_hours = int(session_cfg.get("kyiv_offset_hours", 3))
    cooldown_days = int(strategy_cfg.get("cooldown_days", 3))
    last_open_time = {}
    for t in all_times:
        t = pd.Timestamp(t).tz_convert("UTC") if pd.Timestamp(t).tzinfo else pd.Timestamp(t, tz="UTC")
        if not is_open_hour(t, open_hour_kyiv, kyiv_offset_hours): continue
        md = build_md_slice(dfs, t)
        univ = strat.universe(t, md)
        ranked = strat.rank(t, md, univ)
        for sym in ranked:
            lo = last_open_time.get(sym)
            if lo is not None and (t - lo) < pd.Timedelta(days=cooldown_days): continue
            sig = strat.entry_signal(t, sym, md[sym], ctx={"portfolio": pf})
            if not sig: continue
            if not pf.can_open(portfolio_cfg): break
            pos = pf.open(sym, sig, t, md[sym]["close"])
            pos.meta["max_hold_hours"] = sig.get("max_hold_hours", strategy_cfg.get("hold_hours",60))
            last_open_time[sym] = t
        for pos in pf.open_positions():
            row = md.get(pos.symbol)
            if row is None: continue
            adj = strat.manage_position(t, pos.symbol, pos, row, ctx={"portfolio": pf})
            if adj["action"] == "EXIT":
                pf.close(pos, t, row["close"], reason=adj.get("reason","exit"))
            elif adj["action"] == "MOVE_SL" and adj.get("new_stop") is not None:
                pos.stop_price = adj["new_stop"]
            elif adj["action"] == "MOVE_TP" and adj.get("new_tp") is not None:
                pos.take_profit = adj["new_tp"]
    return pf
