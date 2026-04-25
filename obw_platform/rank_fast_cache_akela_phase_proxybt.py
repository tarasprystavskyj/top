#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rank_fast_cache_akela_phase_proxybt.py

Fast close-only ranker for "Akela missed" short candidates.

Adds to prior BTC/ETH ranker:
  1) BTC/ETH correlation, beta, relative strength, capture.
  2) Fall phase score: prefers beginning/middle of decline, not exhausted late dump.
  3) Close-only proxy short-leg DCA backtest using YAML short params:
       - opens short with USDT-based base order;
       - DCA when price rises against short by configured rise steps;
       - LIFO partial covers using subSellTPPercent;
       - full cover using tpPercent;
       - fees + slippage from YAML portfolio;
       - no intrabar high/low touch, because fast DB only has close.

This is NOT an exact replacement for the full v5 strategy backtest.
On full OHLCV DB, use the platform backtester with the uploaded v5 strategy and YAML.

Example:
  python3 rank_fast_cache_akela_phase_proxybt.py \
    --npz ../DB/futures_fast_cache_2m_2000b.npz \
    --cfg configs/candidate_free_margin_dema2h_early_delev_v5.yaml \
    --top 100 \
    --out _reports/akela_phase_proxybt_2m.csv \
    --json-out _reports/akela_phase_proxybt_2m_top100.json
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

try:
    import yaml
except Exception:
    yaml = None


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    try:
        if b is None or abs(float(b)) < 1e-12 or not np.isfinite(b):
            return default
        return float(a) / float(b)
    except Exception:
        return default


def norm_symbol(x) -> str:
    if isinstance(x, bytes):
        return x.decode("utf-8", errors="ignore")
    return str(x).strip()


def load_yaml(path: str) -> dict:
    if not path:
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML missing. Install: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_fast_cache(npz_path: str, min_bars: int) -> Dict[str, pd.DataFrame]:
    with np.load(npz_path, allow_pickle=True) as z:
        required = {"symbols", "offsets", "timestamp_s", "close"}
        missing = required - set(z.keys())
        if missing:
            raise RuntimeError(f"Missing keys: {sorted(missing)}. Keys={sorted(z.keys())}")

        symbols = [norm_symbol(x) for x in z["symbols"]]
        offsets = np.asarray(z["offsets"], dtype=np.int64)
        timestamp_s = np.asarray(z["timestamp_s"], dtype=np.int64)
        close = np.asarray(z["close"], dtype=float)

    out: Dict[str, pd.DataFrame] = {}
    for i, sym in enumerate(symbols):
        s, e = int(offsets[i]), int(offsets[i + 1])
        if e - s < min_bars:
            continue
        c = close[s:e]
        ts = timestamp_s[s:e]
        good = np.isfinite(c) & (c > 0) & np.isfinite(ts)
        if good.sum() < min_bars:
            continue

        df = (
            pd.DataFrame({
                "timestamp_s": ts[good],
                "datetime_utc": pd.to_datetime(ts[good], unit="s", utc=True),
                "close": c[good],
            })
            .drop_duplicates("timestamp_s")
            .sort_values("timestamp_s")
            .reset_index(drop=True)
        )
        if len(df) >= min_bars:
            df["ret"] = df["close"].pct_change()
            out[sym] = df
    return out


def find_symbol(data: Dict[str, pd.DataFrame], prefix: str) -> str:
    prefix = prefix.upper()
    candidates = []
    for s in data:
        su = s.upper()
        if su == prefix or su.startswith(prefix + "/") or su.startswith(prefix + "USDT"):
            candidates.append(s)
    if not candidates:
        raise RuntimeError(f"Could not find {prefix}. Available examples: {list(data)[:20]}")
    candidates.sort(key=lambda x: (":USDT" not in x, len(x)))
    return candidates[0]


def build_market_proxy(data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    parts = []
    for sym, df in data.items():
        x = df[["timestamp_s", "ret"]].copy()
        x["symbol"] = sym
        parts.append(x)
    all_ret = pd.concat(parts, ignore_index=True)
    return all_ret.groupby("timestamp_s", as_index=False)["ret"].median().rename(columns={"ret": "market_ret"})


def regression_stats(alt_ret: pd.Series, ref_ret: pd.Series) -> Tuple[float, float, int]:
    x = pd.to_numeric(ref_ret, errors="coerce")
    y = pd.to_numeric(alt_ret, errors="coerce")
    ok = x.notna() & y.notna()
    x = x[ok].to_numpy(dtype=float)
    y = y[ok].to_numpy(dtype=float)
    n = len(x)
    if n < 20 or np.std(x) <= 0 or np.std(y) <= 0:
        return np.nan, np.nan, n
    corr = float(np.corrcoef(x, y)[0, 1])
    beta = float(np.cov(y, x, ddof=1)[0, 1] / np.var(x, ddof=1)) if np.var(x, ddof=1) > 1e-16 else np.nan
    return corr, beta, n


def capture_stats(alt_ret: pd.Series, ref_ret: pd.Series) -> Tuple[float, float]:
    x = pd.to_numeric(ref_ret, errors="coerce")
    y = pd.to_numeric(alt_ret, errors="coerce")
    pos = (x > 0) & x.notna() & y.notna()
    neg = (x < 0) & x.notna() & y.notna()
    up = safe_div(float(y[pos].mean()) if pos.any() else 0.0, float(x[pos].mean()) if pos.any() else 0.0, 0.0)
    down = safe_div(float(y[neg].mean()) if neg.any() else 0.0, float(x[neg].mean()) if neg.any() else 0.0, 0.0)
    return up, down


def count_pump_failures(close: np.ndarray, pump_window: int, fail_window: int, pump_pct: float, giveback_frac: float) -> Tuple[int, int, float]:
    n = len(close)
    pumps = fails = 0
    last_i = -10**9
    cooldown = max(1, pump_window // 3)
    for i in range(pump_window, max(pump_window, n - fail_window)):
        if i - last_i < cooldown:
            continue
        base, top = close[i - pump_window], close[i]
        if base <= 0 or not np.isfinite(base) or not np.isfinite(top):
            continue
        if top / base - 1.0 >= pump_pct:
            pumps += 1
            last_i = i
            future_min = np.nanmin(close[i : i + fail_window + 1])
            giveback = safe_div(top - future_min, top - base, 0.0)
            if giveback >= giveback_frac:
                fails += 1
    return fails, pumps, fails / max(1, pumps)


def count_local_short_setups(close: np.ndarray, up_window: int, down_window: int, min_up_pct: float, min_down_pct: float) -> Tuple[int, int, float]:
    n = len(close)
    events = wins = 0
    last_i = -10**9
    cooldown = max(1, down_window // 2)
    for i in range(up_window, max(up_window, n - down_window)):
        if i - last_i < cooldown:
            continue
        base, top = close[i - up_window], close[i]
        if base <= 0 or not np.isfinite(base) or not np.isfinite(top):
            continue
        if top / base - 1.0 >= min_up_pct:
            events += 1
            last_i = i
            future_min = np.nanmin(close[i : i + down_window + 1])
            down = top / future_min - 1.0 if future_min > 0 else 0.0
            if down >= min_down_pct:
                wins += 1
    return wins, events, wins / max(1, events)


def gaussian_score(x: float, target: float, sigma: float) -> float:
    if not np.isfinite(x) or sigma <= 0:
        return 0.0
    return float(math.exp(-0.5 * ((x - target) / sigma) ** 2))


def fall_phase_metrics(close: np.ndarray, recent_window: int, late_dd_pct: float) -> dict:
    n = len(close)
    if n < 10:
        return {}

    high_idx = int(np.nanargmax(close))
    high = float(close[high_idx])
    last = float(close[-1])
    low_after_high = float(np.nanmin(close[high_idx:])) if high_idx < n else last

    dd_from_high_pct = (high - last) / high * 100.0 if high > 0 else 0.0
    bars_since_high = n - 1 - high_idx
    phase_pos = bars_since_high / max(1, n - 1)

    rw = min(recent_window, n - 1)
    recent_ret_pct = (last / close[-rw] - 1.0) * 100.0 if rw > 0 and close[-rw] > 0 else 0.0

    # Close location in post-high range:
    # 0 = at post-high low, 1 = back at high.
    high_to_low = max(high - low_after_high, 1e-12)
    post_high_location = (last - low_after_high) / high_to_low

    # Want confirmed decline, but not exhausted terminal collapse.
    # Targets: drawdown ~18-32%, high happened in first/middle, recent trend still negative.
    dd_score = max(
        gaussian_score(dd_from_high_pct, 18.0, 11.0),
        gaussian_score(dd_from_high_pct, 30.0, 14.0),
    )
    timing_score = max(
        gaussian_score(phase_pos, 0.35, 0.22),
        gaussian_score(phase_pos, 0.55, 0.20),
    )
    recent_score = gaussian_score(recent_ret_pct, -6.0, 10.0)

    too_late_penalty = 0.0
    if dd_from_high_pct >= late_dd_pct:
        too_late_penalty += min(1.0, (dd_from_high_pct - late_dd_pct) / 25.0)
    if phase_pos > 0.82:
        too_late_penalty += min(1.0, (phase_pos - 0.82) / 0.18)
    # If it is at the low after a huge dump and late in the window, edge may be mostly consumed.
    if post_high_location < 0.08 and dd_from_high_pct > 40 and phase_pos > 0.55:
        too_late_penalty += 0.35

    fall_phase_score = max(0.0, 1.15 * dd_score + 1.00 * timing_score + 0.75 * recent_score - 1.20 * too_late_penalty)

    return {
        "dd_from_window_high_pct": dd_from_high_pct,
        "bars_since_window_high": bars_since_high,
        "fall_phase_pos": phase_pos,
        "recent_ret_pct": recent_ret_pct,
        "post_high_location": post_high_location,
        "too_late_penalty": too_late_penalty,
        "fall_phase_score": fall_phase_score,
    }


def mdd_pct(curve: List[float]) -> float:
    a = np.asarray(curve, dtype=float)
    if len(a) < 2:
        return 0.0
    p = np.maximum.accumulate(a)
    dd = (a - p) / np.maximum(p, 1e-12)
    return float(np.nanmin(dd) * 100.0)


def proxy_short_dca_backtest(close: np.ndarray, cfg: dict) -> dict:
    sp = cfg.get("strategy_params_short", {}) or {}
    pf = cfg.get("portfolio", {}) or {}
    sm = cfg.get("sharedMargin", {}) or {}

    eq0 = float(pf.get("initial_equity_per_leg", 100.0))
    fee = float(pf.get("fee_rate", 0.001))
    slip_raw = float(pf.get("slippage_per_side", 4.0))
    # Backtester interprets slippage_per_side > 1 as bp.
    slip = slip_raw / 10000.0 if slip_raw > 1.0 else slip_raw

    base_pct = float(sp.get("baseOrderPctEq", 2.0)) / 100.0
    equity_for_sizing = float(sp.get("equityForSizingUSDT", eq0))
    min_usdt = float(sp.get("minOrderUSDT", 1.2))
    base_notional = max(min_usdt, equity_for_sizing * base_pct)

    side_cap = float(sm.get("sideHardCapUSDT", pf.get("position_notional_short", 1.2) * eq0))
    if side_cap <= 0:
        side_cap = eq0 * 1.45

    tp = float(sp.get("tpPercent", 0.19)) / 100.0
    sub_tp = float(sp.get("subSellTPPercent", sp.get("subCoverTPPercent", 0.42))) / 100.0

    rises = [
        float(sp.get("rise1", 0.33)),
        float(sp.get("rise2", 0.40)),
        float(sp.get("rise3", 0.60)),
        float(sp.get("rise4", 0.80)),
        float(sp.get("rise5", 0.80)),
    ]
    rises = [r / 100.0 for r in rises]
    linear = float(sp.get("linearRisePercent", 0.16)) / 100.0

    mults = [
        1.0,
        float(sp.get("mult2", 1.6)),
        float(sp.get("mult3", 1.0)),
        float(sp.get("mult4", 2.0)),
        float(sp.get("mult5", 3.5)),
    ]
    margin_limit = int(sp.get("marginCallLimit", 244))

    lots: List[Tuple[float, float, float]] = []  # qty, entry_px, notional
    realized = 0.0
    eq_curve = [eq0]
    mtm_curve = [eq0]
    trades = 0
    wins = 0
    gross_pos = 0.0
    max_gross = 0.0
    dca_count = 0
    margin_events = 0
    prev_margin = False

    def open_short(px: float, notional: float):
        nonlocal realized, gross_pos, max_gross, dca_count
        notional = min(notional, max(0.0, side_cap - gross_pos))
        if notional < min_usdt or px <= 0:
            return False
        exec_px = px * (1.0 - slip)
        qty = notional / exec_px
        lots.append((qty, exec_px, notional))
        gross_pos += notional
        max_gross = max(max_gross, gross_pos)
        realized -= fee * notional
        dca_count += 1
        return True

    def close_lot(px: float, idx: int):
        nonlocal realized, gross_pos, trades, wins
        qty, entry, notional = lots.pop(idx)
        exec_px = px * (1.0 + slip)
        pnl = (entry - exec_px) * qty - fee * exec_px * qty
        realized += pnl
        gross_pos -= notional
        trades += 1
        wins += 1 if pnl > 0 else 0
        return pnl

    def avg_entry() -> float:
        q = sum(x[0] for x in lots)
        return sum(qty * entry for qty, entry, _ in lots) / q if q > 1e-12 else 0.0

    def unreal(px: float) -> float:
        return sum((entry - px) * qty for qty, entry, _ in lots)

    next_level_idx = 0

    for i, raw_px in enumerate(close):
        px = float(raw_px)
        if not np.isfinite(px) or px <= 0:
            continue

        # Open if flat.
        if not lots:
            open_short(px, base_notional)
            next_level_idx = 0

        # Full TP on average entry.
        if lots:
            ae = avg_entry()
            if ae > 0 and px <= ae * (1.0 - tp):
                # close all LIFO
                while lots:
                    close_lot(px, len(lots) - 1)
                next_level_idx = 0

        # Partial LIFO cover for last lot.
        if lots:
            last_qty, last_entry, _ = lots[-1]
            if px <= last_entry * (1.0 - sub_tp) and len(lots) > 1:
                close_lot(px, len(lots) - 1)
                next_level_idx = max(0, next_level_idx - 1)

        # DCA against short if price rises from last entry.
        if lots and len(lots) < margin_limit and gross_pos < side_cap:
            last_entry = lots[-1][1]
            step = rises[next_level_idx] if next_level_idx < len(rises) else linear
            if px >= last_entry * (1.0 + step):
                mult = mults[next_level_idx] if next_level_idx < len(mults) else mults[-1]
                notional = base_notional * mult
                if open_short(px, notional):
                    next_level_idx += 1

        eq = eq0 + realized
        mtm = eq + unreal(px)
        in_margin = gross_pos > side_cap + 1e-9
        if in_margin and not prev_margin:
            margin_events += 1
        prev_margin = in_margin
        eq_curve.append(eq)
        mtm_curve.append(mtm)

    final_px = float(close[-1])
    end_unreal = unreal(final_px)
    return {
        "proxy_realized_pnl_short": realized,
        "proxy_unrealized_pnl_short": end_unreal,
        "proxy_total_pnl_short": realized + end_unreal,
        "proxy_return_realized_pct": realized / eq0 * 100.0 if eq0 else 0.0,
        "proxy_return_total_pct": (realized + end_unreal) / eq0 * 100.0 if eq0 else 0.0,
        "proxy_trades_short": trades,
        "proxy_win_rate_short_pct": wins * 100.0 / max(1, trades),
        "proxy_mdd_realized_pct": mdd_pct(eq_curve),
        "proxy_mdd_mtm_pct": mdd_pct(mtm_curve),
        "proxy_max_gross_notional": max_gross,
        "proxy_margin_events": margin_events,
        "proxy_open_lots_end": len(lots),
        "proxy_dca_count": dca_count,
    }


def compute_all(data: Dict[str, pd.DataFrame], cfg: dict, args) -> pd.DataFrame:
    btc_sym = find_symbol(data, "BTC")
    eth_sym = find_symbol(data, "ETH")
    market = build_market_proxy(data)
    btc = data[btc_sym][["timestamp_s", "close", "ret"]].rename(columns={"close": "btc_close", "ret": "btc_ret"})
    eth = data[eth_sym][["timestamp_s", "close", "ret"]].rename(columns={"close": "eth_close", "ret": "eth_ret"})

    rows = []
    for sym, df0 in data.items():
        df = (
            df0[["timestamp_s", "datetime_utc", "close", "ret"]]
            .merge(market, on="timestamp_s", how="left")
            .merge(btc, on="timestamp_s", how="left")
            .merge(eth, on="timestamp_s", how="left")
            .sort_values("timestamp_s")
        )
        close = df["close"].to_numpy(dtype=float)
        if len(close) < args.min_bars or close[0] <= 0:
            continue

        def ref_total(col: str) -> float:
            r = df[col].fillna(0).to_numpy(dtype=float)
            r = r[np.isfinite(r)]
            return (np.prod(1.0 + r) - 1.0) * 100.0 if len(r) else 0.0

        ret_total_pct = (close[-1] / close[0] - 1.0) * 100.0
        market_total_pct = ref_total("market_ret")
        btc_total_pct = ref_total("btc_ret")
        eth_total_pct = ref_total("eth_ret")

        corr_btc, beta_btc, _ = regression_stats(df["ret"], df["btc_ret"])
        corr_eth, beta_eth, _ = regression_stats(df["ret"], df["eth_ret"])
        btc_up, btc_down = capture_stats(df["ret"], df["btc_ret"])
        eth_up, eth_down = capture_stats(df["ret"], df["eth_ret"])
        market_up, market_down = capture_stats(df["ret"], df["market_ret"])

        pfails, pevents, pfr = count_pump_failures(close, args.pump_window, args.pump_fail_window, args.pump_pct, args.pump_giveback_frac)
        swins, sevents, ssr = count_local_short_setups(close, args.setup_up_window, args.setup_down_window, args.setup_min_up_pct, args.setup_min_down_pct)

        cret = pd.Series(close).pct_change().to_numpy(dtype=float)
        downside_1pct = float(np.nansum(cret <= -0.010) * 1000.0 / max(1, len(close)))
        move_1pct = float(np.nansum(np.abs(cret) >= 0.010) * 1000.0 / max(1, len(close)))
        bar_vol_pct = float(np.nanstd(cret) * 100.0)

        phase = fall_phase_metrics(close, recent_window=args.phase_recent_window, late_dd_pct=args.late_dd_pct)
        proxy = proxy_short_dca_backtest(close, cfg) if not args.no_proxy_bt else {}

        rows.append({
            "symbol": sym,
            "bars": len(close),
            "start_utc": str(df["datetime_utc"].iloc[0]),
            "end_utc": str(df["datetime_utc"].iloc[-1]),
            "ret_total_pct": ret_total_pct,
            "market_total_pct": market_total_pct,
            "btc_total_pct": btc_total_pct,
            "eth_total_pct": eth_total_pct,
            "rel_total_pct": ret_total_pct - market_total_pct,
            "rel_to_btc_pct": ret_total_pct - btc_total_pct,
            "rel_to_eth_pct": ret_total_pct - eth_total_pct,
            "corr_btc": corr_btc,
            "corr_eth": corr_eth,
            "beta_btc": beta_btc,
            "beta_eth": beta_eth,
            "btc_up_capture": btc_up,
            "btc_down_capture": btc_down,
            "eth_up_capture": eth_up,
            "eth_down_capture": eth_down,
            "market_up_capture": market_up,
            "market_down_capture": market_down,
            "pump_fail_ratio": pfr,
            "pump_event_count": pevents,
            "short_setup_success_ratio": ssr,
            "short_setup_event_count": sevents,
            "downside_1pct_events_per_1000b": downside_1pct,
            "move_1pct_events_per_1000b": move_1pct,
            "bar_vol_pct": bar_vol_pct,
            **phase,
            **proxy,
        })

    feat = pd.DataFrame(rows)
    if feat.empty:
        return feat

    # Existing weakness scores.
    feat["rs_weak_market_score"] = (-feat["rel_total_pct"]).rank(pct=True)
    feat["rs_weak_btc_score"] = (-feat["rel_to_btc_pct"]).rank(pct=True)
    feat["rs_weak_eth_score"] = (-feat["rel_to_eth_pct"]).rank(pct=True)
    feat["weak_btc_up_score"] = (1.0 - feat["btc_up_capture"]).clip(-2, 3).rank(pct=True)
    feat["bad_btc_down_score"] = (feat["btc_down_capture"] - 1.0).clip(-2, 4).rank(pct=True)
    feat["pump_selloff_score"] = feat["pump_fail_ratio"].rank(pct=True)
    feat["short_setup_score"] = feat["short_setup_success_ratio"].rank(pct=True)
    feat["downside_move_score"] = feat["downside_1pct_events_per_1000b"].rank(pct=True)
    feat["volatility_score"] = feat["bar_vol_pct"].rank(pct=True)
    feat["phase_rank_score"] = feat["fall_phase_score"].rank(pct=True)

    # Proxy BT scores.
    if not args.no_proxy_bt:
        feat["proxy_return_score"] = feat["proxy_return_total_pct"].rank(pct=True)
        feat["proxy_realized_score"] = feat["proxy_return_realized_pct"].rank(pct=True)
        feat["proxy_mdd_score"] = feat["proxy_mdd_mtm_pct"].rank(pct=True)
        feat["proxy_trade_score"] = feat["proxy_trades_short"].rank(pct=True)
        feat["proxy_bt_score"] = (
            1.25 * feat["proxy_return_score"]
            + 0.75 * feat["proxy_realized_score"]
            + 0.75 * feat["proxy_mdd_score"]
            + 0.35 * feat["proxy_trade_score"]
        )
    else:
        feat["proxy_bt_score"] = 0.0

    feat["akela_btc_eth_score"] = (
        1.25 * feat["rs_weak_market_score"]
        + 1.35 * feat["rs_weak_btc_score"]
        + 1.10 * feat["rs_weak_eth_score"]
        + 0.95 * feat["weak_btc_up_score"]
        + 0.90 * feat["bad_btc_down_score"]
        + 1.00 * feat["pump_selloff_score"]
        + 1.00 * feat["short_setup_score"]
        + 0.65 * feat["downside_move_score"]
        + 0.45 * feat["volatility_score"]
    )

    # Final: prefer structural weakness + beginning/middle fall + proxy profitability.
    feat["final_phase_short_score"] = (
        0.45 * feat["akela_btc_eth_score"]
        + 1.65 * feat["phase_rank_score"]
        + 0.85 * feat["proxy_bt_score"]
    )

    # Optional hard-ish filters applied as score penalties rather than deletion.
    # Late fall penalty is already in phase score, but emphasize it again.
    feat["final_phase_short_score"] -= 0.85 * feat["too_late_penalty"].fillna(0)

    btc_sym = find_symbol(data, "BTC")
    eth_sym = find_symbol(data, "ETH")
    feat["is_btc_eth"] = feat["symbol"].isin([btc_sym, eth_sym])

    return feat.sort_values(["is_btc_eth", "final_phase_short_score"], ascending=[True, False])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", required=True)
    ap.add_argument("--cfg", default="")
    ap.add_argument("--out", default="_reports/akela_phase_proxybt.csv")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--min-bars", type=int, default=1000)

    ap.add_argument("--pump-window", type=int, default=180)
    ap.add_argument("--pump-fail-window", type=int, default=60)
    ap.add_argument("--pump-pct", type=float, default=0.04)
    ap.add_argument("--pump-giveback-frac", type=float, default=0.50)

    ap.add_argument("--setup-up-window", type=int, default=60)
    ap.add_argument("--setup-down-window", type=int, default=60)
    ap.add_argument("--setup-min-up-pct", type=float, default=0.012)
    ap.add_argument("--setup-min-down-pct", type=float, default=0.010)

    ap.add_argument("--phase-recent-window", type=int, default=240, help="2m bars: 240 ~= 8h")
    ap.add_argument("--late-dd-pct", type=float, default=55.0)
    ap.add_argument("--no-proxy-bt", action="store_true")

    args = ap.parse_args()
    cfg = load_yaml(args.cfg) if args.cfg else {}

    data = load_fast_cache(args.npz, min_bars=args.min_bars)
    print(f"[data] symbols={len(data)}")

    ranked = compute_all(data, cfg, args)

    front = [
        "symbol",
        "final_phase_short_score",
        "akela_btc_eth_score",
        "fall_phase_score",
        "proxy_bt_score",
        "proxy_return_total_pct",
        "proxy_return_realized_pct",
        "proxy_unrealized_pnl_short",
        "proxy_trades_short",
        "proxy_mdd_mtm_pct",
        "proxy_open_lots_end",
        "ret_total_pct",
        "rel_to_btc_pct",
        "rel_to_eth_pct",
        "corr_btc",
        "beta_btc",
        "btc_up_capture",
        "btc_down_capture",
        "pump_fail_ratio",
        "short_setup_success_ratio",
        "dd_from_window_high_pct",
        "fall_phase_pos",
        "recent_ret_pct",
        "post_high_location",
        "too_late_penalty",
        "downside_1pct_events_per_1000b",
        "bar_vol_pct",
        "bars",
        "start_utc",
        "end_utc",
        "is_btc_eth",
    ]
    cols = [c for c in front if c in ranked.columns] + [c for c in ranked.columns if c not in front]
    ranked = ranked[cols]

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    ranked.to_csv(args.out, index=False)
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        ranked.head(args.top).to_json(args.json_out, orient="records", force_ascii=False, indent=2)

    print("\n[TOP compact]")
    compact_cols = [
        "symbol",
        "final_phase_short_score",
        "fall_phase_score",
        "proxy_return_total_pct",
        "proxy_trades_short",
        "proxy_mdd_mtm_pct",
        "ret_total_pct",
        "rel_to_btc_pct",
        "pump_fail_ratio",
        "short_setup_success_ratio",
        "dd_from_window_high_pct",
        "fall_phase_pos",
        "recent_ret_pct",
        "too_late_penalty",
    ]
    compact_cols = [c for c in compact_cols if c in ranked.columns]
    print(ranked[compact_cols].head(min(args.top, 40)).to_string(index=False))
    print(f"\n[out] {args.out}")
    if args.json_out:
        print(f"[json] {args.json_out}")


if __name__ == "__main__":
    main()
