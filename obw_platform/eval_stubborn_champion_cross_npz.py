#!/usr/bin/env python3
"""Evaluate the HYPE-tuned stubborn champion on other available NPZ symbols.

Research-only. Reads local NPZ OHLCV arrays, does not call exchanges, read
secrets, or place orders.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import numpy as np

from strategies.liquid_money_interval_ema_numba import (
    build_prior_interval,
    simulate_liquid_money_interval_stubborn_only_win,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_CHAMPION = ROOT / "_reports" / "liquid_money_hype_stubborn_overnight" / "run_20260708_005223" / "best_so_far.json"


def iter_default_npz() -> Iterable[Path]:
    db = ROOT.parent / "DB"
    patterns = [
        "new_consilium_btc_eth_exact1y_bybit_fixedts_20260523_224858.npz",
        "new_consilium_btc_eth_exact1y_bybit_20260523_223932.npz",
        "telegram_v21_btc_1m_525600b_bybit_20260523_210319.npz",
        "fast_cache_1m_maxxing_1y_bingx.npz",
        "fast_cache_1m_freedommoney_1y_bingx.npz",
        "akela_meta_short_1m_1y_idol_bingx.npz",
        "ohlcv_1m_ena_second_leg_candidates_1y.npz",
        "ena_ohlcv_1m_1y_from_30s_compat_np1.npz",
    ]
    for name in patterns:
        p = db / name
        if p.exists():
            yield p

    part_dir = db / "telegram_signals_1m_event_windows_720h_bingx_parts"
    if part_dir.exists():
        for p in sorted(part_dir.glob("*.npz")):
            yield p


def npz_symbols(path: Path) -> List[Tuple[str, int, int]]:
    try:
        data = np.load(path, allow_pickle=True)
        required = {"timestamp_s", "high", "low", "close"}
        if not required.issubset(set(data.files)):
            return []
        if "symbols" in data.files and "offsets" in data.files:
            symbols = [str(x) for x in data["symbols"]]
            offsets = data["offsets"].astype(np.int64)
            out = []
            n = len(data["close"])
            for i, sym in enumerate(symbols):
                start = int(offsets[i])
                end = int(offsets[i + 1]) if i + 1 < len(offsets) else n
                if end - start >= 1000:
                    out.append((sym, start, end))
            return out
        return [(path.stem, 0, len(data["close"]))]
    except Exception:
        return []


def load_slice(path: Path, start: int, end: int) -> Dict[str, np.ndarray]:
    data = np.load(path, allow_pickle=True)
    return {
        "timestamp_s": data["timestamp_s"][start:end].astype(np.int64),
        "high": data["high"][start:end].astype(np.float64),
        "low": data["low"][start:end].astype(np.float64),
        "close": data["close"][start:end].astype(np.float64),
    }


def write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: List[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--champion", default=str(DEFAULT_CHAMPION))
    ap.add_argument("--out", default=str(ROOT / "_reports" / "liquid_money_hype_stubborn_cross_npz"))
    ap.add_argument("--npz", action="append", default=[])
    ap.add_argument("--limit-bars", type=int, default=0)
    ap.add_argument("--initial-equity", type=float, default=100.0)
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--slippage", type=float, default=0.0009380229915652661)
    ap.add_argument("--include-hype", action="store_true")
    args = ap.parse_args()

    champion = json.loads(Path(args.champion).read_text(encoding="utf-8"))
    npz_paths = [Path(p) for p in args.npz] if args.npz else list(iter_default_npz())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    rows: List[Dict[str, object]] = []
    errors: List[Dict[str, str]] = []
    seen = set()
    for path in npz_paths:
        if not path.exists():
            continue
        for sym, start, end in npz_symbols(path):
            sym_upper = sym.upper()
            if "HYPE" in sym_upper and not args.include_hype:
                continue
            key = (str(path), sym, start, end)
            if key in seen:
                continue
            seen.add(key)
            try:
                d = load_slice(path, start, end)
                if args.limit_bars and args.limit_bars > 0:
                    for k in d:
                        d[k] = d[k][-args.limit_bars :]
                prior_hi, prior_lo = build_prior_interval(d["high"], d["low"], int(champion["lookback"]))
                res = simulate_liquid_money_interval_stubborn_only_win(
                    d["high"],
                    d["low"],
                    d["close"],
                    prior_hi,
                    prior_lo,
                    int(champion["fast_len"]),
                    int(champion["slow_len"]),
                    float(champion["interval_cap_pct"]),
                    float(champion["min_step_pct"]),
                    float(champion["gamma"]),
                    float(champion["min_profit_pct"]),
                    int(champion["max_hold_bars"]),
                    float(champion["loss_cut_mtm_pct"]),
                    float(args.fee),
                    float(args.slippage),
                    float(args.initial_equity),
                    float(champion["leverage"]),
                )
                rows.append(
                    {
                        "symbol": sym,
                        "npz": str(path),
                        "bars": int(len(d["close"])),
                        "net_pct": float(res[2]),
                        "realized_pct": float(res[0] / args.initial_equity * 100.0),
                        "unrealized_pct": float(res[1] / args.initial_equity * 100.0),
                        "max_dd_pct": float(res[3]),
                        "min_mtm_pct": float(res[4]),
                        "trades": int(res[5]),
                        "win_rate": float(res[7]),
                        "profit_factor": float(res[8]),
                        "max_exposure_pct": float(res[9]),
                        "open_exposure_pct": float(res[10]),
                        "forced_losses": int(res[13]),
                        "stubborn_adds": int(res[14]),
                    }
                )
                print("[ok]", sym, Path(path).name, "net", rows[-1]["net_pct"], "dd", rows[-1]["max_dd_pct"])
            except Exception as exc:
                errors.append({"npz": str(path), "symbol": sym, "error": repr(exc)})
                print("[err]", sym, path, repr(exc))

    rows.sort(key=lambda r: float(r["net_pct"]), reverse=True)
    write_csv(out / "stubborn_hype_champion_cross_npz_results.csv", rows)
    write_csv(out / "errors.csv", errors)
    summary = {
        "champion": champion,
        "count": len(rows),
        "positive_count": sum(1 for r in rows if float(r["net_pct"]) > 0),
        "profitable_pf_count": sum(1 for r in rows if float(r["profit_factor"]) > 1.0),
        "top10": rows[:10],
        "bottom10": rows[-10:],
        "errors": len(errors),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
