
import sqlite3
import pandas as pd
import numpy as np

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
    med = float(np.median(s))
    return max(1.0, med/3600.0)

def load_cache(db_path: str):
    con = sqlite3.connect(db_path)
    table = _detect_main_table(con)
    if not table:
        raise RuntimeError("No tables in cache DB")
    df = pd.read_sql_query(f"SELECT * FROM {table}", con)
    con.close()
    cols = {c.lower(): c for c in df.columns}
    def col(name, default=None):
        return cols.get(name.lower(), default)
    tcol = col("datetime_utc") or col("timestamp") or col("time") or col("dt") or col("datetime")
    if tcol is None: raise RuntimeError("No datetime column")
    df[tcol] = pd.to_datetime(df[tcol], utc=True)
    sym_col = col("symbol","symbol")
    df = df.sort_values([sym_col, tcol]).reset_index(drop=True)
    qv1 = col("quote_volume")
    if qv1 is None and col("volume") and col("close"):
        df["quote_volume"] = df[col("volume")].astype(float)*df[col("close")].astype(float)
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
        if t not in gg.index: continue
        row = gg.loc[t]
        def safe(c):
            return float(row[c]) if c in gg.columns else None
        prior = gg.loc[t - pd.Timedelta(hours=24): t]
        high24 = float(np.nanmax(prior["high"])) if "high" in gg.columns and len(prior) else np.nan
        hcp = float(min(100.0, max(0.0, (float(row["close"])/high24)*100.0))) if (high24 and high24>0) else 0.0
        d = {
            "close": safe("close"),
            "high": safe("high"),
            "low": safe("low"),
            "open": safe("open"),
            "rsi": safe("rsi"),
            "stochastic": safe("stochastic"),
            "mfi": safe("mfi"),
            "atr_ratio": safe("atr_ratio"),
            "overbought_index": safe("overbought_index"),
            "quote_volume": safe("quote_volume"),
            "qv_24h": safe("qv_24h"),
            "highclose_pct": hcp,
        }
        out[sym] = d
    return out
