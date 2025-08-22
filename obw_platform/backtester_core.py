import argparse, importlib, yaml
import pandas as pd
from engine.data import load_cache, build_md_slice
from engine.portfolio import Portfolio

def is_open_hour(t, open_hour_kyiv: int, kyiv_offset_hours: int) -> bool:
    utc_hour = (open_hour_kyiv - kyiv_offset_hours) % 24
    tt = t if t.tzinfo else pd.Timestamp(t, tz="UTC")
    return (tt.hour == utc_hour) and (tt.minute == 0)

def load_strategy(path_cls: str, cfg: dict):
    mod_path, cls_name = path_cls.rsplit(".", 1)
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    return cls(cfg)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", type=str, default="configs/alpha_v0.yaml")
    ap.add_argument("--limit-bars", type=int, default=0)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.cfg, "r"))
    dfs, all_times = load_cache(cfg["cache_db"])
    if args.limit_bars and args.limit_bars>0:
        all_times = all_times[-int(args.limit_bars):]

    strat = load_strategy(cfg["strategy_class"], cfg.get("strategy_params", {}))
    pf = Portfolio(cfg["portfolio"])

    open_hour_kyiv = int(cfg.get("session",{}).get("open_hour_kyiv", 1))
    kyiv_offset_hours = int(cfg.get("session",{}).get("kyiv_offset_hours", 3))

    last_open_time = {}
    cooldown_days = int(cfg.get("strategy_params",{}).get("cooldown_days", 3))

    for t in all_times:
        t = pd.Timestamp(t).tz_convert("UTC") if pd.Timestamp(t).tzinfo else pd.Timestamp(t, tz="UTC")
        if not is_open_hour(t, open_hour_kyiv, kyiv_offset_hours):
            continue

        md = build_md_slice(dfs, t)

        univ = strat.universe(t, md)
        ranked = strat.rank(t, md, univ)

        for sym in ranked:
            lo = last_open_time.get(sym)
            if lo is not None and (t - lo) < pd.Timedelta(days=cooldown_days):
                continue
            sig = strat.entry_signal(t, sym, md[sym], ctx={"portfolio": pf})
            if not sig:
                continue
            if not pf.can_open(cfg["portfolio"]):
                break
            pos = pf.open(sym, sig, t, md[sym]["close"])
            pos.meta["max_hold_hours"] = sig.max_hold_hours if sig.max_hold_hours is not None else cfg["strategy_params"].get("hold_hours", 48)
            last_open_time[sym] = t

        for pos in pf.open_positions():
            row = md.get(pos.symbol)
            if row is None:
                continue
            adj = strat.manage_position(t, pos.symbol, pos, row, ctx={"portfolio": pf})
            if adj.action == "EXIT":
                pf.close(pos, t, row["close"], reason=adj.reason)
            elif adj.action == "MOVE_SL" and adj.new_stop is not None:
                pos.stop_price = adj.new_stop
            elif adj.action == "MOVE_TP" and adj.new_tp is not None:
                pos.take_profit = adj.new_tp

    pf.save_trades("trades.csv")
    pf.save_summary("summary.csv")
    print("Saved trades.csv and summary.csv")

    # --- Visualization (auto) ---
    try:
        from engine.visualize_results import plot_equity_curves
        ret = plot_equity_curves(
            trades_csv="trades.csv",
            summary_csv="summary.csv",
            show=False,
            save_dir="plots",
            file_prefix="run"
        )
        print("[visualize] saved", ret)
    except Exception as _e:
        print("[visualize] skipped:", _e)


if __name__ == "__main__":
    main()