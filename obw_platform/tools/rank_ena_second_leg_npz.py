#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np


REQUIRED_KEYS = {"symbols", "offsets", "timestamp_s", "close", "volume"}


def _decode_symbol(x) -> str:
    if isinstance(x, bytes):
        return x.decode("utf-8")
    return str(x)


def _symbol_bounds(offsets: np.ndarray, symbol_count: int, total_rows: int) -> List[Tuple[int, int]]:
    vals = [int(x) for x in offsets.tolist()]
    if len(vals) == symbol_count + 1:
        return [(vals[i], vals[i + 1]) for i in range(symbol_count)]
    if len(vals) == symbol_count:
        ends = vals[1:] + [total_rows]
        return [(vals[i], ends[i]) for i in range(symbol_count)]
    if symbol_count == 1:
        return [(0, total_rows)]
    raise ValueError(f"Cannot infer symbol bounds: offsets={len(vals)} symbols={symbol_count} rows={total_rows}")


def load_npz_series(path: Path) -> Dict[str, dict]:
    data = np.load(path, allow_pickle=True)
    missing = REQUIRED_KEYS.difference(data.files)
    if missing:
        raise ValueError(f"{path} missing required keys: {sorted(missing)}")

    symbols = [_decode_symbol(x) for x in data["symbols"]]
    total_rows = int(len(data["timestamp_s"]))
    bounds = _symbol_bounds(data["offsets"], len(symbols), total_rows)
    out: Dict[str, dict] = {}
    for sym, (lo, hi) in zip(symbols, bounds):
        close = data["close"][lo:hi].astype(np.float64)
        volume = data["volume"][lo:hi].astype(np.float64)
        ts = data["timestamp_s"][lo:hi].astype(np.int64)
        ok = np.isfinite(close) & (close > 0) & np.isfinite(volume) & np.isfinite(ts)
        out[sym] = {
            "path": str(path),
            "timestamp_s": ts[ok],
            "close": close[ok],
            "volume": volume[ok],
        }
    return out


def pct_change_log(x: np.ndarray) -> np.ndarray:
    return np.diff(np.log(x))


def corr(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3 or len(b) < 3:
        return float("nan")
    aa = a - np.mean(a)
    bb = b - np.mean(b)
    den = float(np.sqrt(np.sum(aa * aa) * np.sum(bb * bb)))
    if den <= 0:
        return float("nan")
    return float(np.sum(aa * bb) / den)


def ols_alpha_beta(y: np.ndarray, x: np.ndarray) -> Tuple[float, float]:
    xm = float(np.mean(x))
    ym = float(np.mean(y))
    xv = float(np.sum((x - xm) ** 2))
    if xv <= 0:
        return ym, 0.0
    beta = float(np.sum((x - xm) * (y - ym)) / xv)
    alpha = ym - beta * xm
    return alpha, beta


def adf_proxy(spread: np.ndarray) -> Tuple[float, float]:
    if len(spread) < 100:
        return float("nan"), float("nan")
    lag = spread[:-1]
    delta = np.diff(spread)
    alpha, beta = ols_alpha_beta(delta, lag)
    resid = delta - (alpha + beta * lag)
    dof = max(1, len(delta) - 2)
    s2 = float(np.sum(resid * resid) / dof)
    lag_centered = lag - np.mean(lag)
    xx = float(np.sum(lag_centered * lag_centered))
    t_stat = beta / math.sqrt(s2 / xx) if s2 > 0 and xx > 0 else float("nan")

    phi = 1.0 + beta
    if 0.0 < phi < 1.0:
        half_life = -math.log(2.0) / math.log(phi)
    else:
        half_life = float("nan")
    return float(t_stat), float(half_life)


def rolling_beta_stability(y_log: np.ndarray, x_log: np.ndarray, bars_per_day: int) -> Tuple[float, float, int]:
    window = max(1000, 30 * bars_per_day)
    step = max(1000, 7 * bars_per_day)
    if len(y_log) < window * 2:
        return float("nan"), float("nan"), 0

    betas: List[float] = []
    for start in range(0, len(y_log) - window + 1, step):
        _, beta = ols_alpha_beta(y_log[start:start + window], x_log[start:start + window])
        if np.isfinite(beta):
            betas.append(beta)
    if not betas:
        return float("nan"), float("nan"), 0
    arr = np.asarray(betas, dtype=np.float64)
    mean_beta = float(np.mean(arr))
    stability = float(np.std(arr) / max(abs(mean_beta), 1e-12))
    return mean_beta, stability, int(len(arr))


def zero_crosses_per_day(spread: np.ndarray, bars_per_day: int) -> float:
    if len(spread) < 3:
        return float("nan")
    centered = spread - np.mean(spread)
    signs = np.sign(centered)
    crosses = np.sum((signs[1:] * signs[:-1]) < 0)
    days = len(spread) / float(bars_per_day)
    return float(crosses / max(days, 1e-12))


def align_series(base: dict, cand: dict) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    ts_base = base["timestamp_s"]
    ts_cand = cand["timestamp_s"]
    common, ib, ic = np.intersect1d(ts_base, ts_cand, assume_unique=False, return_indices=True)
    if len(common) < 100:
        return None
    return common, base["close"][ib], cand["close"][ic], cand["volume"][ic]


def score_row(row: dict) -> float:
    def clamp(v: float, lo: float, hi: float) -> float:
        if not np.isfinite(v):
            return lo
        return max(lo, min(hi, v))

    coverage = clamp(row["coverage_ena"], 0.0, 1.0)
    corr_abs = clamp(abs(row["return_corr"]), 0.0, 1.0)
    adf = clamp((-row["adf_t_stat"] - 1.5) / 3.0, 0.0, 1.0)
    half_life = row["half_life_bars"]
    if np.isfinite(half_life):
        hl_score = clamp(1.0 - abs(half_life - 2880.0) / 8640.0, 0.0, 1.0)
    else:
        hl_score = 0.0
    hedge = clamp(1.0 - row["rolling_beta_cv"], 0.0, 1.0)
    volume = 1.0 if row["median_quote_volume_30s"] > 0 else 0.0
    crosses = clamp(row["zero_crosses_per_day"] / 20.0, 0.0, 1.0)
    return float(0.25 * coverage + 0.20 * corr_abs + 0.20 * adf + 0.15 * hedge + 0.10 * hl_score + 0.05 * volume + 0.05 * crosses)


def analyze_candidate(base_sym: str, base: dict, cand_sym: str, cand: dict, candidate_path: str) -> Optional[dict]:
    aligned = align_series(base, cand)
    if aligned is None:
        return None
    ts, base_close, cand_close, cand_vol = aligned

    dts = np.diff(ts)
    median_step = int(np.median(dts)) if len(dts) else 30
    bars_per_day = max(1, int(round(86400 / max(1, median_step))))

    base_ret = pct_change_log(base_close)
    cand_ret = pct_change_log(cand_close)
    return_corr = corr(base_ret, cand_ret)

    y_log = np.log(base_close)
    x_log = np.log(cand_close)
    alpha, beta = ols_alpha_beta(y_log, x_log)
    spread = y_log - (alpha + beta * x_log)
    adf_t, half_life = adf_proxy(spread)
    _, beta_cv, beta_windows = rolling_beta_stability(y_log, x_log, bars_per_day)
    qv = cand_vol * cand_close

    row = {
        "candidate_symbol": cand_sym,
        "candidate_path": candidate_path,
        "base_symbol": base_sym,
        "overlap_rows": int(len(ts)),
        "coverage_ena": float(len(ts) / max(1, len(base["timestamp_s"]))),
        "coverage_candidate": float(len(ts) / max(1, len(cand["timestamp_s"]))),
        "from_utc": np.datetime_as_string(np.datetime64(int(ts[0]), "s"), unit="s"),
        "to_utc": np.datetime_as_string(np.datetime64(int(ts[-1]), "s"), unit="s"),
        "median_step_s": int(median_step),
        "gap_ratio_gt_step": float(np.mean(dts > median_step)) if len(dts) else 0.0,
        "return_corr": float(return_corr),
        "hedge_alpha": float(alpha),
        "hedge_beta": float(beta),
        "rolling_beta_cv": float(beta_cv),
        "rolling_beta_windows": int(beta_windows),
        "adf_t_stat": float(adf_t),
        "half_life_bars": float(half_life),
        "zero_crosses_per_day": zero_crosses_per_day(spread, bars_per_day),
        "median_quote_volume_30s": float(np.nanmedian(qv)),
        "score": 0.0,
    }
    row["score"] = score_row(row)
    return row


def write_outputs(rows: List[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "ena_second_leg_rank.csv"
    json_path = out_dir / "ena_second_leg_rank.json"
    md_path = out_dir / "ena_second_leg_rank.md"

    rows = sorted(rows, key=lambda r: r["score"], reverse=True)
    fields = [
        "score", "candidate_symbol", "candidate_path", "overlap_rows", "coverage_ena",
        "coverage_candidate", "from_utc", "to_utc", "median_step_s", "gap_ratio_gt_step",
        "return_corr", "hedge_beta", "rolling_beta_cv", "rolling_beta_windows",
        "adf_t_stat", "half_life_bars", "zero_crosses_per_day", "median_quote_volume_30s",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(rows)
    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    lines = ["# ENA second-leg NPZ rank", ""]
    if not rows:
        lines.append("No candidate NPZ files with a non-ENA symbol had enough overlapping timestamps to rank.")
    else:
        lines.append("| score | symbol | overlap | ret corr | beta | beta cv | adf t | half-life bars | qv median |")
        lines.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|")
        for r in rows[:20]:
            lines.append(
                f"| {r['score']:.4f} | `{r['candidate_symbol']}` | {r['overlap_rows']} | "
                f"{r['return_corr']:.4f} | {r['hedge_beta']:.4f} | {r['rolling_beta_cv']:.4f} | "
                f"{r['adf_t_stat']:.3f} | {r['half_life_bars']:.1f} | {r['median_quote_volume_30s']:.2f} |"
            )
    lines.append("")
    lines.append(f"CSV: `{csv_path}`")
    lines.append(f"JSON: `{json_path}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def iter_candidate_paths(patterns: Iterable[str], ena_path: Path) -> List[Path]:
    paths: List[Path] = []
    seen = set()
    for pattern in patterns:
        for raw in glob.glob(pattern):
            p = Path(raw)
            try:
                resolved = p.resolve()
            except Exception:
                resolved = p
            if resolved == ena_path.resolve():
                continue
            if str(resolved) in seen:
                continue
            seen.add(str(resolved))
            paths.append(p)
    return sorted(paths)


def main() -> None:
    ap = argparse.ArgumentParser(description="Rank second-leg 30s OHLCV NPZ candidates against ENA.")
    ap.add_argument("--ena-npz", default="DB/ena_ohlcv_30s_1y_from_ticks_compat_np1.npz")
    ap.add_argument("--candidate", action="append", default=[], help="Candidate NPZ file. Can be repeated.")
    ap.add_argument("--candidate-glob", action="append", default=[], help="Glob for candidate NPZ files. Can be repeated.")
    ap.add_argument("--out-dir", default="docs/ena_second_leg_data/reports")
    ap.add_argument("--allow-same-symbol", action="store_true")
    args = ap.parse_args()

    ena_path = Path(args.ena_npz)
    base_map = load_npz_series(ena_path)
    if not base_map:
        raise SystemExit(f"No symbols found in {ena_path}")
    base_sym = next(iter(base_map))
    base = base_map[base_sym]

    candidate_paths = [Path(x) for x in args.candidate]
    candidate_paths += iter_candidate_paths(args.candidate_glob or ["DB/*30s*.npz"], ena_path)

    rows: List[dict] = []
    errors: List[dict] = []
    for path in candidate_paths:
        try:
            cand_map = load_npz_series(path)
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        for cand_sym, cand in cand_map.items():
            if not args.allow_same_symbol and cand_sym == base_sym:
                continue
            row = analyze_candidate(base_sym, base, cand_sym, cand, str(path))
            if row is not None:
                rows.append(row)

    out_dir = Path(args.out_dir)
    write_outputs(rows, out_dir)
    manifest = {
        "ena_npz": str(ena_path),
        "base_symbol": base_sym,
        "base_rows": int(len(base["timestamp_s"])),
        "candidate_paths": [str(p) for p in candidate_paths],
        "ranked_candidates": len(rows),
        "errors": errors,
    }
    (out_dir / "ena_second_leg_rank_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[done] ranked={len(rows)} out_dir={out_dir}")


if __name__ == "__main__":
    main()
