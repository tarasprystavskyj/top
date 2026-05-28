#!/usr/bin/env python3
"""Equivalence harness for experimental Numba HYPE DCA replay core.

The old Python engine is authoritative. This script compares compact summary
metrics and fails closed if tolerances are exceeded.

Research-only. Does not place orders, read secrets, or call network APIs.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import numpy as np

CHAMPION_NAME = "rnd5337_t500_b12_s0p953-1p3-1p442-1p767_w0p597-0p82-1p151-1p868"
DEFAULT_TOLERANCES = {
    "equity_end": 1e-6,
    "net_pct": 1e-6,
    "max_mtm_dd_pct": 1e-6,
    "avg_dca_fills": 1e-12,
    "notional_gt_equity_before_count": 0.0,
    "margin_call_count": 0.0,
}


def _load_module(path: Path, name: str) -> Any:
    path = path.resolve()
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _target_module(script_path: Path) -> Any:
    return _load_module(script_path, "hype_dca_parameter_search_reference_target")


def _numba_module(script_path: Path) -> Any:
    return _load_module(script_path, "hype_dca_numba_exact_replay_core")


def _candidate_champion(mod: Any) -> Any:
    return mod.Candidate(
        CHAMPION_NAME,
        500.0,
        0.12,
        (0.953, 1.3, 1.442, 1.767),
        (0.597, 0.82, 1.151, 1.868),
    )


def _candidate_plain(mod: Any) -> Any:
    return mod.Candidate("plain_no_dca_t500", 500.0, 1.0, (), ())


def _candidate_current_like_dca3(mod: Any) -> Any:
    return mod.Candidate(
        "current_like_dca3_t500",
        500.0,
        0.21739130434782608,
        (0.45, 0.35, 0.60),
        (1.1, 1.0, 1.5),
    )


def _candidate_set(mod: Any, include_random: int, seed: int, max_target_notional: float | None) -> List[Any]:
    out = [_candidate_champion(mod), _candidate_plain(mod), _candidate_current_like_dca3(mod)]
    if include_random > 0:
        generated = mod.candidate_grid(1.0, max(include_random, 100), seed, max_target_notional)
        out.extend(generated[:include_random])
    # Deduplicate by name while preserving order.
    seen = set()
    deduped = []
    for c in out:
        if c.name in seen:
            continue
        seen.add(c.name)
        deduped.append(c)
    return deduped


def _python_summary(mod: Any, positions: Sequence[Any], arrays: Dict[str, np.ndarray], candidate: Any, *, initial_equity: float, fill_mode: str, position_sizing_mode: str, leverage: float, min_trade_mtm_pct: float) -> Dict[str, Any]:
    rows = mod.simulate_candidate_rows(
        positions,
        candidate,
        arrays,
        fill_mode=fill_mode,
        initial_equity=initial_equity,
        position_sizing_mode=position_sizing_mode,
        leverage=leverage,
    )
    mod.annotate_trade_equity_metrics(rows, initial_equity)
    s = mod.summarize(rows, initial_equity)
    s.update(mod.grounding_stats(rows, fill_mode=fill_mode, min_trade_mtm_pct=min_trade_mtm_pct))
    s.update(mod.leverage_stats(rows, leverage=leverage, min_trade_mtm_pct=min_trade_mtm_pct))
    return s


def _compare(py: Dict[str, Any], nb: Dict[str, Any], tolerances: Dict[str, float]) -> Dict[str, Any]:
    checks = []
    ok = True
    aliases = {
        "notional_gt_equity_before_count": "notional_gt_equity_before_count",
        "margin_call_count": "margin_call_count",
    }
    for key, tol in tolerances.items():
        py_key = aliases.get(key, key)
        py_val = float(py.get(py_key, 0.0))
        nb_val = float(nb.get(key, 0.0))
        diff = abs(py_val - nb_val)
        passed = diff <= tol
        ok = ok and passed
        checks.append({"metric": key, "python": py_val, "numba": nb_val, "abs_diff": diff, "tolerance": tol, "pass": passed})
    return {"pass": ok, "checks": checks}


def _safe_json(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (np.integer, np.floating)):
        return obj.item()
    raise TypeError(f"not json serializable: {type(obj).__name__}")


def main() -> None:
    here = Path(__file__).resolve()
    default_target = here.parents[1] / "run_hype_dca_parameter_search.py"
    default_numba = here.parents[1] / "experimental" / "numba_exact_replay_core.py"
    ap = argparse.ArgumentParser(description="Check Numba DCA replay equivalence against Python reference.")
    ap.add_argument("--target-script", default=str(default_target))
    ap.add_argument("--numba-core", default=str(default_numba))
    ap.add_argument("--positions-csv", required=True)
    ap.add_argument("--npz", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--entry-source", default="next_bar_open")
    ap.add_argument("--slippage-bp", type=float, default=4.25)
    ap.add_argument("--initial-equity", type=float, default=500.0)
    ap.add_argument("--position-sizing-mode", choices=("fixed", "compound"), default="compound")
    ap.add_argument("--fill-mode", default="close_beyond_skip_boundary")
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--min-order-usd", type=float, default=2.0)
    ap.add_argument("--min-trade-mtm-pct", type=float, default=-50.0)
    ap.add_argument("--include-random", type=int, default=0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-target-notional", type=float, default=500.0)
    args = ap.parse_args()

    ref = _target_module(Path(args.target_script))
    nb = _numba_module(Path(args.numba_core))
    ref.MIN_ORDER_USD = float(args.min_order_usd)

    arrays = ref.load_npz_arrays(Path(args.npz))
    positions = ref.read_positions(Path(args.positions_csv))
    positions = ref.apply_entry_source(positions, arrays, args.entry_source)
    candidates = ref.apply_candidate_slippage(
        _candidate_set(ref, args.include_random, args.seed, args.max_target_notional),
        args.slippage_bp,
    )
    replay_inputs = nb.build_replay_inputs_from_python(positions, arrays, ref.ms)

    results = []
    all_ok = True
    for c in candidates:
        py = _python_summary(
            ref, positions, arrays, c,
            initial_equity=args.initial_equity,
            fill_mode=args.fill_mode,
            position_sizing_mode=args.position_sizing_mode,
            leverage=args.leverage,
            min_trade_mtm_pct=args.min_trade_mtm_pct,
        )
        nb_candidate = nb.candidate_to_arrays(c)
        nbs = nb.simulate_candidate_summary(
            replay_inputs,
            nb_candidate,
            initial_equity=args.initial_equity,
            fill_mode=args.fill_mode,
            position_sizing_mode=args.position_sizing_mode,
            leverage=args.leverage,
            min_order_usd_gate=args.min_order_usd,
        )
        cmp = _compare(py, nbs, DEFAULT_TOLERANCES)
        all_ok = all_ok and bool(cmp["pass"])
        results.append({"candidate": c.name, "pass": cmp["pass"], "comparison": cmp["checks"], "python": py, "numba": nbs})

    out = {
        "status": "PASS" if all_ok else "FAIL",
        "candidate_count": len(candidates),
        "tolerances": DEFAULT_TOLERANCES,
        "entry_source": args.entry_source,
        "fill_mode": args.fill_mode,
        "position_sizing_mode": args.position_sizing_mode,
        "results": results,
    }
    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=_safe_json), encoding="utf-8")
    print(json.dumps({"status": out["status"], "out_json": str(out_path), "candidate_count": len(candidates)}, ensure_ascii=False, indent=2))
    if not all_ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
