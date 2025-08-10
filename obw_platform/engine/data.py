
import os, sqlite3, pandas as pd, numpy as np

def _detect_main_table(con):
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = [r[0] for r in cur.fetchall()]
    for cand in ("price_indicators","klines","candles","ohlcv"):
        if cand in names: return cand
    return names[0] if names else None

def _infer_bar_hours(gg: pd.DataFrame) -> float:
    if len(gg.index) < 2: return 1.0
    s = pd.Series(gg.index).diff().dropna().dt.total_seconds().values
    if len(s)==0: return 1.0
    med = float(np.median(s)); return max(1.0, med/3600.0)

def load_cache(db_path: str):
    if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
        raise RuntimeError(f"Cache DB not found or empty: {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    table = _detect_main_table(con)
    if not table:
        con.close()
        raise RuntimeError("No tables in cache DB")
    df = pd.read_sql_query(f"SELECT * FROM {table}", con)
    con.close()
    cols = {c.lower(): c for c in df.columns}
    def col(name, default=None): return cols.get(name.lower(), default)
    tcol = col("datetime_utc") or col("timestamp") or col("time") or col("dt") or col("datetime")
    if tcol is None:
        raise RuntimeError("No datetime column in cache DB")
    df[tcol] = pd.to_datetime(df[tcol], utc=True)
    sym_col = col("symbol","symbol")
    df = df.sort_values([sym_col, tcol]).reset_index(drop=True)
    if "quote_volume" not in df.columns and col("volume") and col("close"):
        df["quote_volume"] = df[col("volume")].astype(float) * df[col("close")].astype(float)
    dfs = {}
    for sym, g in df.groupby(sym_col):
        gg = g.copy().set_index(tcol).sort_index()
        if "qv_24h" not in gg.columns and "quote_volume" in gg.columns:
            win = max(1, int(round(24.0 / _infer_bar_hours(gg))))
            gg["qv_24h"] = gg["quote_volume"].rolling(win, min_periods=win).sum()
        dfs[sym] = gg
    all_times = sorted(df[tcol].unique())
    return dfs, pd.to_datetime(all_times)

def build_md_slice(dfs: dict, t):
    out = {}
    for sym, gg in dfs.items():
        if t not in gg.index: continue
        row = gg.loc[t]
        def v(c): return float(row[c]) if c in gg.columns else None
        prior = gg.loc[t - pd.Timedelta(hours=24): t]
        if "high" in gg.columns and len(prior):
            import numpy as np
            high24 = float(np.nanmax(prior["high"]))
            hcp = float(min(100.0, max(0.0, (float(row["close"])/max(high24,1e-12))*100.0)))
        else:
            hcp = 0.0
        out[sym] = {
            "close": v("close"), "high": v("high"), "low": v("low"), "open": v("open"),
            "rsi": v("rsi"), "stochastic": v("stochastic"), "mfi": v("mfi"),
            "atr_ratio": v("atr_ratio"), "overbought_index": v("overbought_index"),
            "quote_volume": v("quote_volume"), "qv_24h": v("qv_24h"), "highclose_pct": hcp
        }
    return out
