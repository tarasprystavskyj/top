#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml

FEE_RATE_DEFAULT = 0.0005
SLIP_RATE_DEFAULT = 0.0004


def load_agg_15m(npz_path: str) -> pd.DataFrame:
    z = np.load(npz_path, allow_pickle=True)
    need = {"timestamp_s", "open", "high", "low", "close", "volume"}
    miss = sorted(need - set(z.files))
    if miss:
        raise ValueError(f"NPZ missing fields: {miss}")
    df30 = pd.DataFrame({
        "timestamp_s": z["timestamp_s"],
        "open": z["open"],
        "high": z["high"],
        "low": z["low"],
        "close": z["close"],
        "volume": z["volume"],
        "quote_volume": z["quote_volume"] if "quote_volume" in z.files else z["close"] * z["volume"],
    })
    df30["dt"] = pd.to_datetime(df30["timestamp_s"], unit="s", utc=True)
    df15 = (
        df30.set_index("dt")
        .resample("15min")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "quote_volume": "sum"})
        .dropna()
    )
    return df15


def prepare_cache(df: pd.DataFrame):
    c = df["close"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    q = df["quote_volume"].to_numpy(dtype=float)

    prev = np.r_[c[0], c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - prev), np.abs(l - prev)])
    bar_rng = (h - l) / np.maximum(c, 1e-12)
    absret = np.r_[0.0, np.abs(np.diff(c) / np.maximum(c[:-1], 1e-12))]

    lengths = {4, 8, 12, 16, 24, 32, 48, 64, 96, 128, 160, 192}
    roll = {}
    for L in lengths:
        roll[("tr", L)] = pd.Series(tr).rolling(L, min_periods=L).mean().to_numpy()
        roll[("ret", L)] = pd.Series(absret).rolling(L, min_periods=L).mean().to_numpy()
        roll[("rng", L)] = pd.Series(bar_rng).rolling(L, min_periods=L).mean().to_numpy()
        roll[("qv", L)] = pd.Series(q).rolling(L, min_periods=L).mean().shift(1).to_numpy()

    ema = {L: pd.Series(c).ewm(span=L, adjust=False).mean().to_numpy() for L in [8, 12, 16, 20, 24, 32, 48, 64, 96]}
    hh = {L: pd.Series(h).rolling(L, min_periods=L).max().shift(1).to_numpy() for L in [4, 8, 12, 16, 24, 32, 48]}
    ll = {L: pd.Series(l).rolling(L, min_periods=L).min().shift(1).to_numpy() for L in [4, 8, 12, 16, 24, 32, 48]}

    return c, h, l, q, roll, ema, hh, ll


def backtest(df: pd.DataFrame, params: dict, cache, *, fee_rate: float, slip_rate: float, start=None, end=None, export_curve: bool = True):
    c, h, l, q, roll, ema, hh, ll = cache

    times = df.index
    if start is None:
        i0, i1 = 0, len(c)
    else:
        mask = (times >= pd.Timestamp(start, tz="UTC")) & (times < pd.Timestamp(end, tz="UTC"))
        idx = np.flatnonzero(mask)
        if len(idx) < 50:
            raise ValueError("window too small")
        i0, i1 = idx[0], idx[-1] + 1

    comp = roll[("ret", params["comp_len"])][i0:i1] / np.maximum(roll[("ret", params["base_len"])][i0:i1], 1e-12)
    comp2 = roll[("rng", params["comp_len"])][i0:i1] / np.maximum(roll[("rng", params["base_len"])][i0:i1], 1e-12)
    is_comp = (comp < params["comp_ratio"]) & (comp2 < params["range_ratio"])
    vol_ok = q[i0:i1] > roll[("qv", params["base_len"])][i0:i1] * params["vol_mult"]
    ef = ema[params["ema_fast"]][i0:i1]
    es = ema[params["ema_slow"]][i0:i1]
    L = ll[params["brk_len"]][i0:i1]

    cc, hh_, ll_ = c[i0:i1], h[i0:i1], l[i0:i1]
    atr = roll[("tr", params["comp_len"])][i0:i1]

    pos = 0
    entry = stop = tp = 0.0
    hold = 0
    eq = 1.0
    trades = []
    trade_rows = []

    eq_curve = np.ones(len(cc), dtype=float)
    pos_curve = np.zeros(len(cc), dtype=int)
    entry_curve = np.full(len(cc), np.nan)

    start_i = max(params["base_len"], params["ema_slow"], params["brk_len"]) + 2

    for i in range(start_i, len(cc)):
        if pos != 0:
            hold += 1
            exit_px = None
            exit_reason = None

            if pos == -1:
                if hh_[i] >= stop:
                    exit_px = stop * (1 + slip_rate)
                    exit_reason = "stop"
                elif ll_[i] <= tp:
                    exit_px = tp * (1 + slip_rate)
                    exit_reason = "tp"

            if exit_px is None and hold >= params["max_hold"]:
                exit_px = cc[i] * (1 + slip_rate)
                exit_reason = "timeout"

            if exit_px is not None:
                net = ((exit_px - entry) / entry * pos) - 2 * fee_rate
                eq *= 1 + net
                trades.append(net)
                trade_rows.append({
                    "entry_time": str(times[i0:i1][max(i - hold, 0)]),
                    "exit_time": str(times[i0:i1][i]),
                    "side": "short",
                    "entry": entry,
                    "exit": exit_px,
                    "ret": float(net),
                    "hold_bars": int(hold),
                    "reason": exit_reason,
                })
                pos = 0
                hold = 0

        if pos == 0 and is_comp[i - 1] and vol_ok[i] and cc[i] < L[i] and ef[i] < es[i]:
            pos = -1
            entry = cc[i] * (1 - slip_rate)
            stop = entry + params["stop_atr"] * atr[i]
            tp = entry - params["tp_atr"] * atr[i]
            hold = 0

        eq_curve[i] = eq
        pos_curve[i] = pos
        entry_curve[i] = entry if pos != 0 else np.nan

    if pos != 0:
        exit_px = cc[-1] * (1 + slip_rate)
        net = ((exit_px - entry) / entry * pos) - 2 * fee_rate
        eq *= 1 + net
        trades.append(net)
        trade_rows.append({
            "entry_time": str(times[i0:i1][max(len(cc) - 1 - hold, 0)]),
            "exit_time": str(times[i0:i1][-1]),
            "side": "short",
            "entry": entry,
            "exit": exit_px,
            "ret": float(net),
            "hold_bars": int(hold),
            "reason": "final_close",
        })

    trades = np.array(trades, dtype=float)
    ec = pd.Series(eq_curve).ffill().to_numpy(dtype=float)
    peak = np.maximum.accumulate(ec)
    dd_curve = ec / np.maximum(peak, 1e-12) - 1.0
    mdd = float(dd_curve.min())

    gp = trades[trades > 0].sum() if len(trades) else 0.0
    gl = -trades[trades < 0].sum() if len(trades) else 0.0
    pf = float(gp / max(gl, 1e-12)) if len(trades) else 0.0

    ret = float(eq - 1.0)
    days = max((times[i1 - 1] - times[i0]).total_seconds() / 86400.0, 1e-9)
    ann = float(eq ** (365.0 / days) - 1.0) if eq > 0 else -1.0

    curve_df = None
    if export_curve:
        curve_df = pd.DataFrame({
            "dt": times[i0:i1],
            "close": cc,
            "equity": ec,
            "drawdown": dd_curve,
            "position": pos_curve,
            "entry_price": entry_curve,
        })

    return {
        "ret": ret,
        "annualized": ann,
        "profit_factor": pf,
        "trades": int(len(trades)),
        "mdd": mdd,
        "win_rate": float((trades > 0).mean()) if len(trades) else 0.0,
        "days": float(days),
        "curve": curve_df,
        "trades_rows": trade_rows,
    }


def save_plots(curve_df: pd.DataFrame, out_dir: Path, prefix: str):
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(curve_df["dt"], curve_df["equity"], label="Equity")
    ax.set_title(f"{prefix} Equity Curve")
    ax.grid(True, alpha=0.3)
    ax.legend()
    eq_path = out_dir / f"{prefix}_equity.png"
    fig.tight_layout()
    fig.savefig(eq_path, dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(curve_df["dt"], curve_df["drawdown"] * 100.0, label="Drawdown %")
    ax.set_title(f"{prefix} Drawdown")
    ax.grid(True, alpha=0.3)
    ax.legend()
    dd_path = out_dir / f"{prefix}_drawdown.png"
    fig.tight_layout()
    fig.savefig(dd_path, dpi=160)
    plt.close(fig)

    return {"equity_plot": str(eq_path), "drawdown_plot": str(dd_path)}


def main():
    ap = argparse.ArgumentParser(description="Stage3 challenger short volatility expansion backtest")
    ap.add_argument("--npz", required=True)
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--plots-dir", default="")
    ap.add_argument("--time-from", default="")
    ap.add_argument("--time-to", default="")
    ap.add_argument("--fee-rate", type=float, default=FEE_RATE_DEFAULT)
    ap.add_argument("--slip-rate", type=float, default=SLIP_RATE_DEFAULT)
    args = ap.parse_args()

    params = yaml.safe_load(open(args.cfg, "r", encoding="utf-8"))
    df = load_agg_15m(args.npz)
    cache = prepare_cache(df)

    res = backtest(
        df,
        params,
        cache,
        fee_rate=float(args.fee_rate),
        slip_rate=float(args.slip_rate),
        start=args.time_from or None,
        end=args.time_to or None,
        export_curve=True,
    )

    curve = res.pop("curve")
    trades_rows = res.pop("trades_rows")

    out = {
        "strategy_family": "short_volatility_expansion_stage3_challenger",
        "execution_basis": "ENA 30s OHLCV aggregated to 15m",
        "costs": {"fee_per_side": float(args.fee_rate), "slippage_per_side": float(args.slip_rate)},
        "params": params,
        "window": {"time_from": args.time_from or None, "time_to": args.time_to or None},
        "metrics": res,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    trades_csv = out_path.with_suffix(".trades.csv")
    pd.DataFrame(trades_rows).to_csv(trades_csv, index=False)

    curve_csv = out_path.with_suffix(".curve.csv")
    curve.to_csv(curve_csv, index=False)

    if args.plots_dir:
        plots = save_plots(curve, Path(args.plots_dir), out_path.stem)
        out["artifacts"] = {**plots, "trades_csv": str(trades_csv), "curve_csv": str(curve_csv)}
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
