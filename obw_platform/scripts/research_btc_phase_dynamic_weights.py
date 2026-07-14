#!/usr/bin/env python3
"""BTC movement phase diagnostics and dynamic portfolio weights.

Research/paper only. Reads local OHLCV NPZ files and writes reports. It does
not call exchanges, read secrets, or place orders.

Functional-analysis view used here:
- price is a sampled function x(t);
- log-return / smoothed momentum is a finite-difference derivative D x;
- momentum change is D^2 x;
- BTC regimes are regions in the (D x, D^2 x) phase plane.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from run_liquid_money_live_like_monotonic_tune import replay_live_like, safe_symbol

DEFAULT_NPZ = REPO / "DB" / "fast_cache_bingx_usdtm_1m_btc_pyth_hype_sol_ena_uni_mature_merged_20260713.npz"
DEFAULT_OUT = ROOT / "_reports" / "btc_phase_dynamic_weights_20260713"
DEFAULT_RUN = ROOT / "_reports" / "liquid_money_mature_1x_full_overnight_20260712" / "run_20260712_231936"

SYMBOLS = [
    "PYTH/USDT:USDT",
    "HYPE/USDT:USDT",
    "SOL/USDT:USDT",
    "ENA/USDT:USDT",
    "UNI/USDT:USDT",
]
BTC = "BTC/USDT:USDT"


def load_npz(path: Path) -> Dict[str, Dict[str, np.ndarray]]:
    z = np.load(path, allow_pickle=True)
    syms = [str(s) for s in z["symbols"]]
    offsets = z["offsets"].astype(np.int64)
    n = len(z["timestamp_s"])
    out: Dict[str, Dict[str, np.ndarray]] = {}
    for i, sym in enumerate(syms):
        start = int(offsets[i])
        end = int(offsets[i + 1]) if i + 1 < len(offsets) else n
        out[sym] = {
            "timestamp_s": z["timestamp_s"][start:end].astype(np.int64),
            "open": z["open"][start:end].astype(np.float64),
            "high": z["high"][start:end].astype(np.float64),
            "low": z["low"][start:end].astype(np.float64),
            "close": z["close"][start:end].astype(np.float64),
            "volume": z["volume"][start:end].astype(np.float64),
        }
    return out


def load_json(path: Path) -> Dict[str, float]:
    return json.loads(path.read_text(encoding="utf-8"))


def config_map(run_dir: Path) -> Dict[str, Dict[str, float]]:
    pyth = load_json(run_dir / "PYTH_USDT_USDT" / "best_so_far.json")
    hype = load_json(run_dir / "HYPE_USDT_USDT" / "best_so_far.json")
    sol = load_json(run_dir / "SOL_USDT_USDT" / "best_so_far.json")

    # ENA and UNI are added before their own full tune exists. Use the best
    # hedge-screen templates found on the same strategy family:
    # ENA looked best under the HYPE template; UNI looked best under SOL.
    return {
        "PYTH/USDT:USDT": pyth,
        "HYPE/USDT:USDT": hype,
        "SOL/USDT:USDT": sol,
        "ENA/USDT:USDT": hype,
        "UNI/USDT:USDT": sol,
    }


def curve_stats_from_returns(returns: np.ndarray) -> Dict[str, float]:
    eq = np.cumprod(1.0 + np.asarray(returns, dtype=np.float64))
    if len(eq) == 0:
        return {"total_pct": 0.0, "mdd_pct": 0.0, "stagnation_days": 0.0, "monthly_pct": 0.0, "annualized_pct": 0.0}
    peak = np.maximum.accumulate(eq)
    dd = eq / np.maximum(peak, 1e-12) - 1.0
    best = eq[0]
    peak_i = 0
    max_stag = 0
    for i, val in enumerate(eq):
        if val > best:
            best = val
            peak_i = i
        else:
            max_stag = max(max_stag, i - peak_i)
    days = max(float(len(eq)), 1.0)
    total_mult = float(eq[-1])
    monthly = float("nan")
    annualized = float("nan")
    if total_mult > 0.0:
        monthly = (total_mult ** (30.0 / days) - 1.0) * 100.0
        annualized = (total_mult ** (365.24 / days) - 1.0) * 100.0
    return {
        "total_pct": (total_mult - 1.0) * 100.0,
        "mdd_pct": float(dd.min() * 100.0),
        "stagnation_days": float(max_stag),
        "monthly_pct": monthly,
        "annualized_pct": annualized,
    }


def simplex_weights(n: int, step: float) -> Iterable[Tuple[float, ...]]:
    units = int(round(1.0 / step))
    if n == 1:
        yield (1.0,)
        return
    def rec(left: int, k: int, prefix: List[int]):
        if k == 1:
            yield tuple(prefix + [left])
            return
        for v in range(left + 1):
            yield from rec(left - v, k - 1, prefix + [v])
    for counts in rec(units, n, []):
        yield tuple(c * step for c in counts)


def capped_normalize(raw: np.ndarray, max_w: float, min_w: float = 0.0) -> np.ndarray:
    scores = np.asarray(raw, dtype=np.float64).copy()
    n = len(scores)
    if n <= 0:
        return scores
    scores[~np.isfinite(scores)] = 0.0
    scores = np.maximum(scores, 0.0)
    if scores.sum() <= 1e-12:
        scores[:] = 1.0

    min_w = max(0.0, float(min_w))
    max_w = max(min_w, float(max_w))
    if min_w * n > 1.0:
        min_w = 1.0 / n
    if max_w * n < 1.0:
        max_w = 1.0 / n

    out = np.full(n, min_w, dtype=np.float64)
    remaining = 1.0 - min_w * n
    cap_extra = np.full(n, max_w - min_w, dtype=np.float64)
    active = np.ones(n, dtype=bool)
    scores = scores / max(float(scores.sum()), 1e-12)

    for _ in range(n + 1):
        if remaining <= 1e-12 or not bool(active.any()):
            break
        active_scores = scores[active]
        if active_scores.sum() <= 1e-12:
            active_scores = np.ones_like(active_scores)
        alloc = remaining * active_scores / max(float(active_scores.sum()), 1e-12)
        active_idx = np.where(active)[0]
        over = alloc > cap_extra[active_idx] + 1e-12
        if not bool(over.any()):
            out[active_idx] += alloc
            remaining = 0.0
            break
        capped_idx = active_idx[over]
        out[capped_idx] += cap_extra[capped_idx]
        remaining -= float(cap_extra[capped_idx].sum())
        active[capped_idx] = False
    if remaining > 1e-9:
        room = np.maximum(0.0, max_w - out)
        if room.sum() > 1e-12:
            out += remaining * room / room.sum()
    return out / max(float(out.sum()), 1e-12)


def btc_features(daily: pd.DataFrame) -> pd.DataFrame:
    out = daily.copy()
    logp = np.log(out["BTC_close"].astype(float))
    out["btc_ret_1d"] = logp.diff()
    out["btc_velocity_3d"] = logp.diff(3)
    out["btc_velocity_7d"] = logp.diff(7)
    out["btc_accel_3d"] = out["btc_velocity_3d"].diff()
    out["btc_accel_7d"] = out["btc_velocity_7d"].diff()
    out["btc_vol_7d"] = out["btc_ret_1d"].rolling(7).std()
    v = out["btc_velocity_3d"]
    a = out["btc_accel_3d"]
    phase = np.full(len(out), "neutral", dtype=object)
    phase[(v > 0) & (a > 0)] = "rise_accelerating"
    phase[(v > 0) & (a <= 0)] = "rise_decelerating"
    phase[(v <= 0) & (a > 0)] = "fall_decelerating"
    phase[(v <= 0) & (a <= 0)] = "fall_accelerating"
    out["btc_phase_v3_a3"] = phase
    out["btc_phase_signal_prevday"] = out["btc_phase_v3_a3"].shift(1).fillna("neutral")
    return out


def dynamic_phase_weights(
    returns: pd.DataFrame,
    features: pd.DataFrame,
    symbols: Sequence[str],
    lookback_days: int,
    beta: float,
    max_w: float,
    smooth: float,
    base_mode: str,
) -> Tuple[pd.DataFrame, np.ndarray]:
    r = returns[[f"ret_{safe_symbol(s)}" for s in symbols]].to_numpy(np.float64)
    # Predictive alignment: today's allocation can only use the BTC phase known
    # at yesterday's close. The return at index i is the move into day i.
    phases = features["btc_phase_signal_prevday"].astype(str).to_numpy()
    vols = np.nan_to_num(pd.DataFrame(r).rolling(21).std().to_numpy(), nan=0.0)
    weights = np.zeros_like(r)
    prev = np.ones(len(symbols), dtype=np.float64) / len(symbols)
    for i in range(len(r)):
        start = max(0, i - lookback_days)
        hist = r[start:i]
        hist_phases = phases[start:i]
        if len(hist) < 14:
            raw = np.ones(len(symbols), dtype=np.float64)
        else:
            same = hist_phases == phases[i]
            if same.sum() < 7:
                same = np.ones(len(hist), dtype=bool)
            mu = np.nanmean(hist[same], axis=0)
            vol = np.nanstd(hist[same], axis=0)
            if base_mode == "risk_parity":
                base = 1.0 / np.maximum(vol, 1e-4)
            else:
                base = np.ones(len(symbols), dtype=np.float64)
            score = np.nan_to_num(mu / np.maximum(vol, 1e-4), nan=0.0)
            score = np.clip(score, -2.0, 2.0)
            exp_score = np.exp(beta * score)
            raw = base * exp_score
        w = capped_normalize(raw, max_w=max_w)
        if i > 0:
            w = capped_normalize((1.0 - smooth) * w + smooth * prev, max_w=max_w)
        weights[i] = w
        prev = w
    port_ret = (weights * r).sum(axis=1)
    wdf = pd.DataFrame(weights, columns=[f"w_{safe_symbol(s)}" for s in symbols])
    return wdf, port_ret


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", type=Path, default=DEFAULT_NPZ)
    ap.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--initial-equity", type=float, default=300.0)
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--slippage", type=float, default=0.000938)
    ap.add_argument("--min-step-notional", type=float, default=2.5)
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    data = load_npz(args.npz)
    missing = [s for s in [BTC] + SYMBOLS if s not in data]
    if missing:
        raise SystemExit(f"missing symbols in npz: {missing}")
    cfgs = config_map(args.run_dir)

    # Build strategy curves.
    daily_parts: List[pd.DataFrame] = []
    price_parts: List[pd.DataFrame] = []
    for sym in [BTC] + SYMBOLS:
        d = data[sym]
        pdf = pd.DataFrame({
            "timestamp_s": d["timestamp_s"],
            "close": d["close"],
        })
        pdf["day"] = pd.to_datetime(pdf["timestamp_s"], unit="s", utc=True).dt.floor("D")
        pdaily = pdf.groupby("day").tail(1).copy()
        pdaily = pdaily[["day", "close"]].rename(columns={"close": f"price_{safe_symbol(sym)}"})
        price_parts.append(pdaily)

    for sym in SYMBOLS:
        df = replay_live_like(
            data[sym],
            cfgs[sym],
            initial_equity=args.initial_equity,
            fee=args.fee,
            slip=args.slippage,
            min_step_notional=args.min_step_notional,
            long_leverage=1.0,
            short_leverage=1.0,
        )
        df["day"] = pd.to_datetime(df["timestamp_s"], unit="s", utc=True).dt.floor("D")
        dd = df.groupby("day").tail(1).copy()
        ss = safe_symbol(sym)
        dd = dd[["day", "realized_pct", "unrealized_pct", "total_pct", "exposure_pct", "side"]].rename(
            columns={
                "realized_pct": f"realized_{ss}",
                "unrealized_pct": f"unrealized_{ss}",
                "total_pct": f"total_{ss}",
                "exposure_pct": f"exposure_{ss}",
                "side": f"side_{ss}",
            }
        )
        daily_parts.append(dd)

    daily = price_parts[0]
    for part in price_parts[1:] + daily_parts:
        daily = daily.merge(part, on="day", how="inner")
    daily = daily.sort_values("day").reset_index(drop=True)
    daily = daily.rename(columns={f"price_{safe_symbol(BTC)}": "BTC_close"})

    for sym in SYMBOLS:
        ss = safe_symbol(sym)
        daily[f"eq_{ss}"] = 1.0 + daily[f"total_{ss}"].astype(float) / 100.0
        daily[f"ret_{ss}"] = daily[f"eq_{ss}"].pct_change().fillna(0.0)
        daily[f"price_ret_{ss}"] = daily[f"price_{ss}"].astype(float).pct_change().fillna(0.0)

    daily = btc_features(daily)
    daily.to_csv(args.out_dir / "daily_strategy_price_btc_features.csv", index=False)

    # Correlation and conditional return diagnostics.
    corr_rows: List[Dict[str, object]] = []
    cond_rows: List[Dict[str, object]] = []
    features = ["btc_ret_1d", "btc_velocity_3d", "btc_velocity_7d", "btc_accel_3d", "btc_accel_7d", "btc_vol_7d"]
    for sym in SYMBOLS:
        ss = safe_symbol(sym)
        for kind, col in [("strategy", f"ret_{ss}"), ("price", f"price_ret_{ss}")]:
            for feat in features:
                corr_rows.append({
                    "symbol": sym,
                    "return_kind": kind,
                    "feature": feat,
                    "corr": float(daily[col].corr(daily[feat])),
                })
            for phase_col, alignment in [
                ("btc_phase_v3_a3", "same_day_descriptive"),
                ("btc_phase_signal_prevday", "prevday_predictive"),
            ]:
                for phase, g in daily.groupby(phase_col):
                    cond_rows.append({
                        "symbol": sym,
                        "return_kind": kind,
                        "alignment": alignment,
                        "btc_phase": phase,
                        "days": int(len(g)),
                        "mean_daily_pct": float(g[col].mean() * 100.0),
                        "median_daily_pct": float(g[col].median() * 100.0),
                        "hit_rate_pct": float((g[col] > 0).mean() * 100.0),
                        "compounded_phase_pct": float((np.prod(1.0 + g[col].to_numpy()) - 1.0) * 100.0),
                    })
    pd.DataFrame(corr_rows).to_csv(args.out_dir / "btc_feature_correlations.csv", index=False)
    pd.DataFrame(cond_rows).to_csv(args.out_dir / "btc_phase_conditional_returns.csv", index=False)

    # Static grid baseline over five sleeves.
    ret_cols = [f"ret_{safe_symbol(s)}" for s in SYMBOLS]
    rmat = daily[ret_cols].to_numpy(np.float64)
    static_rows: List[Dict[str, object]] = []
    for w in simplex_weights(len(SYMBOLS), 0.05):
        warr = np.asarray(w, dtype=np.float64)
        port_ret = rmat @ warr
        st = curve_stats_from_returns(port_ret)
        row: Dict[str, object] = {
            "name": "static_grid",
            "weights": json.dumps({s: float(x) for s, x in zip(SYMBOLS, w)}, sort_keys=True),
            **st,
        }
        for sym, x in zip(SYMBOLS, w):
            row[f"w_{safe_symbol(sym)}"] = float(x)
        # objective is MTM, but penalize non-realistic drawdown/stagnation.
        row["score"] = st["total_pct"] - max(0.0, abs(st["mdd_pct"]) - 10.0) * 12.0 - max(0.0, st["stagnation_days"] - 21.0) * 1.0
        static_rows.append(row)
    static_df = pd.DataFrame(static_rows).sort_values("score", ascending=False)
    static_df.to_csv(args.out_dir / "static_weight_grid.csv", index=False)

    # Dynamic BTC-phase allocator variants.
    dyn_rows: List[Dict[str, object]] = []
    dyn_curves: Dict[str, Tuple[pd.DataFrame, np.ndarray]] = {}
    for lookback, beta, max_w, smooth, base_mode in itertools.product(
        [30, 45, 60, 90, 120],
        [0.5, 1.0, 2.0, 4.0],
        [0.45, 0.60, 0.75, 0.85],
        [0.0, 0.35, 0.65],
        ["equal", "risk_parity"],
    ):
        wdf, port_ret = dynamic_phase_weights(daily, daily, SYMBOLS, lookback, beta, max_w, smooth, base_mode)
        st = curve_stats_from_returns(port_ret)
        name = f"btc_phase_lb{lookback}_b{beta:g}_cap{max_w:g}_sm{smooth:g}_{base_mode}"
        row = {
            "name": name,
            "lookback_days": lookback,
            "beta": beta,
            "max_symbol_weight": max_w,
            "smooth": smooth,
            "base_mode": base_mode,
            **st,
        }
        for sym in SYMBOLS:
            row[f"avg_w_{safe_symbol(sym)}"] = float(wdf[f"w_{safe_symbol(sym)}"].mean())
            row[f"last_w_{safe_symbol(sym)}"] = float(wdf[f"w_{safe_symbol(sym)}"].iloc[-1])
        row["score"] = st["total_pct"] - max(0.0, abs(st["mdd_pct"]) - 10.0) * 12.0 - max(0.0, st["stagnation_days"] - 21.0) * 1.0
        dyn_rows.append(row)
        dyn_curves[name] = (wdf, port_ret)
    dyn_df = pd.DataFrame(dyn_rows).sort_values("score", ascending=False)
    dyn_df.to_csv(args.out_dir / "btc_phase_dynamic_weight_grid.csv", index=False)

    # Named comparison curves.
    curves: Dict[str, np.ndarray] = {}
    equal_ret = rmat @ (np.ones(len(SYMBOLS)) / len(SYMBOLS))
    curves["equal_static_5"] = equal_ret
    best_static = static_df.iloc[0]
    best_static_w = np.asarray([float(best_static[f"w_{safe_symbol(s)}"]) for s in SYMBOLS])
    curves["best_static_grid_5"] = rmat @ best_static_w
    best_dyn = dyn_df.iloc[0]
    best_dyn_name = str(best_dyn["name"])
    curves[best_dyn_name] = dyn_curves[best_dyn_name][1]

    metric_rows = []
    for name, ret in curves.items():
        st = curve_stats_from_returns(ret)
        metric_rows.append({"name": name, **st})
        eq = np.cumprod(1.0 + ret)
        pd.DataFrame({"day": daily["day"], "equity": eq, "return": ret}).to_csv(args.out_dir / f"{name}_curve.csv", index=False)
    pd.DataFrame(metric_rows).to_csv(args.out_dir / "comparison_metrics.csv", index=False)

    # Plots.
    fig, ax = plt.subplots(figsize=(15, 7))
    for name, ret in curves.items():
        eq = np.cumprod(1.0 + ret)
        ax.plot(pd.to_datetime(daily["day"]), (eq / eq[0] - 1.0) * 100.0, label=name, linewidth=1.25)
    btc_norm = daily["BTC_close"].astype(float) / float(daily["BTC_close"].iloc[0])
    ax.plot(pd.to_datetime(daily["day"]), (btc_norm - 1.0) * 100.0, color="black", alpha=0.25, linewidth=1.0, label="BTC price %")
    ax.grid(True, alpha=0.25)
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.legend(loc="best", fontsize=8)
    ax.set_title("BTC-phase dynamic weights vs static baselines")
    ax.set_ylabel("% from start")
    fig.tight_layout()
    fig.savefig(args.out_dir / "btc_phase_portfolio_comparison.png", dpi=150)
    plt.close(fig)

    best_wdf = dyn_curves[best_dyn_name][0].copy()
    best_wdf["day"] = daily["day"]
    best_wdf.to_csv(args.out_dir / f"{best_dyn_name}_weights.csv", index=False)
    fig, ax = plt.subplots(figsize=(15, 7))
    x = pd.to_datetime(best_wdf["day"])
    bottom = np.zeros(len(best_wdf))
    for sym in SYMBOLS:
        col = f"w_{safe_symbol(sym)}"
        vals = best_wdf[col].to_numpy(float)
        ax.fill_between(x, bottom, bottom + vals, label=sym, alpha=0.75)
        bottom += vals
    ax.set_ylim(0, 1.02)
    ax.grid(True, alpha=0.20)
    ax.legend(loc="upper left", ncol=3, fontsize=8)
    ax.set_title(f"Best BTC-phase dynamic weights: {best_dyn_name}")
    fig.tight_layout()
    fig.savefig(args.out_dir / "best_btc_phase_dynamic_weights.png", dpi=150)
    plt.close(fig)

    # Report.
    lines = [
        "# BTC Phase Dynamic Weights Research",
        "",
        f"- NPZ: `{args.npz}`",
        f"- Window: `{daily['day'].iloc[0]}` to `{daily['day'].iloc[-1]}`",
        f"- Symbols: `{', '.join(SYMBOLS)}`",
        "- BTC phase plane: `velocity = D log(BTC)` over 3 days, `acceleration = D velocity`.",
        "- ENA uses the HYPE liquid-money template for this first pass; UNI uses the SOL template. They still need their own tune.",
        "",
        "## Comparison",
    ]
    for row in metric_rows:
        lines.append(
            f"- `{row['name']}`: total `{row['total_pct']:.2f}%`, monthly `{row['monthly_pct']:.2f}%`, "
            f"annualized `{row['annualized_pct']:.2f}%`, MDD `{row['mdd_pct']:.2f}%`, stagnation `{row['stagnation_days']:.0f}d`"
        )
    lines.extend(["", "## Best Static Grid"])
    lines.append(
        f"- total `{best_static['total_pct']:.2f}%`, MDD `{best_static['mdd_pct']:.2f}%`, stagnation `{best_static['stagnation_days']:.0f}d`, weights `{best_static['weights']}`"
    )
    lines.extend(["", "## Best BTC-Phase Dynamic"])
    lines.append(
        f"- `{best_dyn_name}`: total `{best_dyn['total_pct']:.2f}%`, MDD `{best_dyn['mdd_pct']:.2f}%`, stagnation `{best_dyn['stagnation_days']:.0f}d`, score `{best_dyn['score']:.2f}`"
    )
    lines.extend(["", "## Files"])
    for fname in [
        "btc_feature_correlations.csv",
        "btc_phase_conditional_returns.csv",
        "static_weight_grid.csv",
        "btc_phase_dynamic_weight_grid.csv",
        "comparison_metrics.csv",
        "btc_phase_portfolio_comparison.png",
        "best_btc_phase_dynamic_weights.png",
    ]:
        lines.append(f"- `{args.out_dir / fname}`")
    (args.out_dir / "BTC_PHASE_DYNAMIC_WEIGHTS_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(args.out_dir)
    print(pd.DataFrame(metric_rows).to_string(index=False))
    print("best_static", best_static.to_dict())
    print("best_dynamic", best_dyn.to_dict())


if __name__ == "__main__":
    main()
