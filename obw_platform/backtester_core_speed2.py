
#!/usr/bin/env python3
import argparse, sqlite3, importlib, time, sys
from dataclasses import dataclass

import pathlib as _p
# Ensure strategies package is on path
sys.path.insert(0, str((_p.Path(__file__).parent / 'obw_c2_repro_pack_nodb' / 'backtest_SK').resolve()))

def load_yaml(path):
    import yaml
    with open(path,"r") as f:
        return yaml.safe_load(f)

def import_by_path(path: str):
    mod_name, cls_name = path.rsplit(".", 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, cls_name)

def connect_db(path: str):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=OFF;")
    con.execute("PRAGMA synchronous=OFF;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA mmap_size=268435456;")
    con.row_factory = sqlite3.Row
    return con

@dataclass
class Position:
    side: str
    entry: float
    sl: float
    tp: float

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--limit-bars", type=int, default=500)
    args = ap.parse_args()

    t0 = time.time()
    cfg = load_yaml(args.cfg)
    con = connect_db(cfg["cache_db"])

    # Load last N distinct times
    # Find the threshold time for the last N distinct bars
    th_row = con.execute(
        "SELECT t FROM (SELECT DISTINCT datetime_utc AS t FROM price_indicators ORDER BY datetime_utc DESC LIMIT ?) ORDER BY t ASC LIMIT 1",
        (int(args.limit_bars),)
    ).fetchone()
    if not th_row:
        print("No times."); return
    min_time = th_row[0]

    # Fetch all rows with datetime_utc >= min_time, sorted
    rows = con.execute(
        "SELECT symbol, datetime_utc, close, atr_ratio, dp6h, dp12h, quote_volume, qv_24h "
        "FROM price_indicators WHERE datetime_utc >= ? ORDER BY datetime_utc ASC, symbol ASC",
        (min_time,)
    ).fetchall()

    # Build per-time slices as arrays for fast iteration
    # Also compute mom_sum once per row
    slices = []  # list of (t, [rows_for_t])
    cur_t = None; bucket = []
    for r in rows:
        t = r["datetime_utc"]
        if cur_t is None:
            cur_t = t
        if t != cur_t:
            slices.append((cur_t, bucket))
            bucket = []
            cur_t = t
        bucket.append((
            r["symbol"],
            float(r["close"] or 0.0),
            float(r["atr_ratio"] or 0.0),
            float(r["dp6h"] or 0.0),
            float(r["dp12h"] or 0.0),
            float(r["quote_volume"] or 0.0),
            float(r["qv_24h"] or 0.0),
        ))
    if bucket:
        slices.append((cur_t, bucket))

    # Strategy init
    # thresholds for prefilter
    min_atr = float(cfg.get('min_atr_ratio', 0.0))
    min_mom = float(cfg.get('min_momentum_sum', 0.0))
    min_qv24 = float(cfg.get('min_qv_24h', 0.0))
    min_qv1h = float(cfg.get('min_qv_1h', 0.0))

    Strat = import_by_path(cfg["strategy_class"])
    strat = Strat(cfg)

    portfolio = cfg.get("portfolio", {})
    sp = cfg.get("strategy_params", {})
    initial_equity = float(portfolio.get("initial_equity", 100.0))
    pos_notional = float(portfolio.get("position_notional", 20.0))
    fee = float(portfolio.get("fee_rate", 0.001))
    slippage = float(portfolio.get("slippage_per_side", 0.0003))
    top_n = int(sp.get("top_n", 8))
    side_pref = str(sp.get("side","BOTH")).upper()
    tp_mult = float(sp.get("tp_atr_mult", 2.6))
    sl_mult = float(sp.get("sl_atr_mult", 1.0))
    max_notional_frac = float(portfolio.get("max_notional_frac", 0.5))

    equity = initial_equity
    positions = {}  # sym -> Position
    wins=losses=trades=0; pnl_pos=0.0; pnl_neg=0.0

    # Main loop
    for t, bucket in slices:
        # quick lookup for exits
        px_map = {sym: close for (sym, close, atr, dp6, dp12, qv1h, qv24) in bucket}

        # exits
        if positions:
            for sym, pos in list(positions.items()):
                px = px_map.get(sym)
                if px is None: continue
                if pos.side=="LONG":
                    hit = (px >= pos.tp) or (px <= pos.sl)
                    pnl = (px - pos.entry)/pos.entry - 2*slippage - 2*fee if hit else None
                else:
                    hit = (px <= pos.tp) or (px >= pos.sl)
                    pnl = (pos.entry - px)/pos.entry - 2*slippage - 2*fee if hit else None
                if pnl is not None:
                    trades+=1
                    if pnl>0: wins+=1; pnl_pos += pnl
                    else: losses+=1; pnl_neg += pnl
                    equity *= (1.0 + (pnl * pos_notional / equity))
                    del positions[sym]

        # select top_n by momentum (linear scan; n is very small)
        # mom_sum = dp6h + dp12h; for SHORT, invert sign
        invert = (side_pref=="SHORT")
        # simple selection by maintaining small array of best candidates
        best = []  # list of (score, idx)
        for idx, (sym, close, atr, dp6, dp12, qv1h, qv24) in enumerate(bucket):
            score = (dp6 + dp12)
            if invert: score = -score
            # insert if better than worst or if we have < top_n
            if len(best) < top_n:
                best.append((score, idx))
                if len(best)==top_n:
                    best.sort(key=lambda x: x[0], reverse=True)
            else:
                if score > best[-1][0]:
                    best[-1] = (score, idx)
                    # keep sorted descending
                    if best[-2][0] < best[-1][0]:
                        best.sort(key=lambda x: x[0], reverse=True)

        # open on candidates
        for _, idx in best:
            sym, close, atr, dp6, dp12, qv1h, qv24 = bucket[idx]
            if sym in positions: continue
            mom_sum = dp6 + dp12
            # HARD THRESH FILTER to avoid heavy entry_signal() when obviously failing
            if atr < min_atr: continue
            if qv24 < min_qv24 or qv1h < min_qv1h: continue
            if side_pref in ('BOTH','LONG'):
                if mom_sum < min_mom: continue
            else:
                if -mom_sum < min_mom: continue
            row = {"close": close, "atr_ratio": atr, "dp6h": dp6, "dp12h": dp12, "quote_volume": qv1h, "qv_24h": qv24}
            sig = strat.entry_signal(t, sym, row, ctx={})
            if sig is None: continue
            if len(positions)*pos_notional >= max_notional_frac * equity: break
            atr_abs = max(1e-12, atr*close)
            if sig.side=="LONG":
                sl = close - sl_mult*atr_abs; tp = close + tp_mult*atr_abs
            else:
                sl = close + sl_mult*atr_abs; tp = close - tp_mult*atr_abs
            positions[sym] = Position(sig.side, close, sl, tp)

    # mark-to-market
    if slices:
        last_px = {sym: close for (sym, close, atr, dp6, dp12, qv1h, qv24) in slices[-1][1]}
        for sym, pos in list(positions.items()):
            px = last_px.get(sym)
            if px is None: continue
            pnl = (px - pos.entry)/pos.entry - 2*slippage - 2*fee if pos.side=="LONG" else (pos.entry - px)/pos.entry - 2*slippage - 2*fee
            trades += 1
            if pnl>0: wins+=1; pnl_pos += pnl
            else: losses+=1; pnl_neg += pnl
            equity *= (1.0 + (pnl * pos_notional / equity))
            del positions[sym]

    elapsed = time.time() - t0
    # write summary.csv
    import pandas as pd
    pf = (pnl_pos / max(1e-12, -pnl_neg)) if (pnl_pos>0 and pnl_neg<0) else 0.0
    pd.DataFrame([{
        "equity_start": initial_equity, "equity_end": equity, "trades": trades,
        "profit_factor": pf, "win_rate_%": (wins*100.0/max(1,trades) if trades else 0.0),
        "elapsed_sec": elapsed
    }]).to_csv("summary.csv", index=False)
    print(f"equity_end={equity:.6f} trades={trades} pf={pf:.6f} elapsed_sec={elapsed:.6f}")

if __name__ == "__main__":
    main()
