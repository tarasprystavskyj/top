import sqlite3
import pandas as pd
import numpy as np

def _detect_main_table(con: sqlite3.Connection) -> str:
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = [r[0] for r in cur.fetchall()]
    for cand in ("price_indicators","klines","candles","ohlcv"):
        if cand in names: return cand
    if names: return names[0]
    raise RuntimeError("No tables in cache DB")

def _infer_bar_hours(gg: pd.DataFrame) -> float:
    if len(gg.index) < 2: return 1.0
    s = pd.Series(gg.index).diff().dropna().dt.total_seconds().values
    if len(s)==0: return 1.0
    med = float(np.median(s))
    return max(1.0, med/3600.0)

def load_cache(db_path: str):
    con = sqlite3.connect(db_path)
    table = _detect_main_table(con)
    df = pd.read_sql_query(f"SELECT * FROM {table}", con)
    con.close()
    cols = {c.lower(): c for c in df.columns}
    def col(name, default=None):
        return cols.get(name.lower(), default)
    tcol = col("datetime_utc") or col("timestamp") or col("time") or col("dt") or col("datetime")
    if tcol is None: raise RuntimeError("No datetime column in DB")
    df[tcol] = pd.to_datetime(df[tcol], utc=True)
    sym_col = col("symbol","symbol")
    df = df.sort_values([sym_col, tcol]).reset_index(drop=True)
    qv1 = col("quote_volume")
    if qv1 is None and col("volume") and col("close"):
        df["quote_volume"] = df[col("volume")].astype(float) * df[col("close")].astype(float)
        qv1 = "quote_volume"
    need_qv24 = ("qv_24h" not in [c.lower() for c in df.columns])
    dfs = {}
    for sym, g in df.groupby(sym_col):
        gg = g.copy().set_index(tcol).sort_index()
        if need_qv24 and "quote_volume" in gg.columns:
            bar_h = _infer_bar_hours(gg)
            win = max(1, int(round(24.0 / bar_h)))
            gg["qv_24h"] = gg["quote_volume"].rolling(window=win, min_periods=win).sum()
        dfs[sym] = gg
    all_times = sorted(df[tcol].unique())
    return dfs, pd.to_datetime(all_times)

def build_md_slice(dfs: dict, t):
    out = {}
    for sym, gg in dfs.items():
        if t not in gg.index:
            continue
        row = gg.loc[t]
        def safe(col, default=None):
            return float(row[col]) if col in gg.columns else default
        dp6 = np.nan
        dp12= np.nan
        t6 = t - pd.Timedelta(hours=6)
        t12= t - pd.Timedelta(hours=12)
        try:
            if t6 in gg.index and gg.loc[t6,"close"]>0:
                dp6 = float(gg.loc[t,"close"] / gg.loc[t6,"close"] - 1.0)
        except Exception: pass
        try:
            if t12 in gg.index and gg.loc[t12,"close"]>0:
                dp12 = float(gg.loc[t,"close"] / gg.loc[t12,"close"] - 1.0)
        except Exception: pass
        d = {
            "close": safe("close", None),
            "high": safe("high", None),
            "low": safe("low", None),
            "open": safe("open", None),
            "rsi": safe("rsi", None),
            "stochastic": safe("stochastic", None),
            "mfi": safe("mfi", None),
            "atr_ratio": safe("atr_ratio", None),
            "overbought_index": safe("overbought_index", None),
            "quote_volume": safe("quote_volume", None),
            "qv_24h": safe("qv_24h", None),
            "dp6h": dp6 if pd.notna(dp6) else 0.0,
            "dp12h": dp12 if pd.notna(dp12) else 0.0,
        }
        out[sym] = d
    return out