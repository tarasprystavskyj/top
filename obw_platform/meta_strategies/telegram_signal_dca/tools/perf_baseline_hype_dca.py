#!/usr/bin/env python3
"""Profile HYPE Veronica DCA parameter search without changing replay math.

This wrapper imports run_hype_dca_parameter_search.py, optionally constrains
candidate_grid to a bounded candidate set, runs target main() under cProfile,
and writes machine-readable profiler artifacts.

Research-only. Does not place orders, read secrets, or call network APIs.
"""
from __future__ import annotations

import argparse
import cProfile
import importlib.util
import io
import json
import os
import platform
import pstats
import sys
import time
from dataclasses import asdict, is_dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Sequence

import numpy as np

CHAMPION_NAME = "rnd5337_t500_b12_s0p953-1p3-1p442-1p767_w0p597-0p82-1p151-1p868"


def _load_target_module(script_path: Path) -> Any:
    script_path = script_path.resolve()
    if str(script_path.parent) not in sys.path:
        sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("hype_dca_parameter_search_profile_target", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import target script: {script_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class TimerRegistry:
    def __init__(self) -> None:
        self.data: Dict[str, Dict[str, float]] = {}

    def wrap(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        bucket = self.data.setdefault(name, {"calls": 0.0, "seconds": 0.0})

        @wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                dt = time.perf_counter() - t0
                bucket["calls"] += 1.0
                bucket["seconds"] += dt

        return inner

    def as_json(self) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        for k, v in self.data.items():
            calls = int(v["calls"])
            seconds = float(v["seconds"])
            out[k] = {
                "calls": calls,
                "seconds": seconds,
                "avg_ms_per_call": 1000.0 * seconds / max(calls, 1),
            }
        return out


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


def _patch_candidate_grid(mod: Any, mode: str, candidate_limit: int, random_candidates: int, seed: int) -> None:
    original = mod.candidate_grid

    def patched_candidate_grid(target_scale: float, random_candidates_arg: int, seed_arg: int, max_target_notional: float | None) -> List[Any]:
        if mode == "champion":
            return [_candidate_champion(mod)]
        if mode == "baselines":
            return [_candidate_plain(mod), _candidate_current_like_dca3(mod), _candidate_champion(mod)]
        if mode in {"sample100", "sample1000", "sample"}:
            # Generate a superset through the original factory, then slice. This keeps
            # target math unchanged but avoids an unbounded run when only profiling.
            needed = candidate_limit
            generated = original(target_scale, max(random_candidates, needed), seed, max_target_notional)
            return generated[:needed]
        if mode == "unpatched":
            return original(target_scale, random_candidates_arg, seed_arg, max_target_notional)
        raise ValueError(f"unsupported mode={mode!r}")

    mod.candidate_grid = patched_candidate_grid


def _position_stats(mod: Any, positions_csv: Path, npz_path: Path, entry_source: str) -> Dict[str, Any]:
    positions = mod.read_positions(positions_csv)
    arrays = mod.load_npz_arrays(npz_path)
    positions = mod.apply_entry_source(positions, arrays, entry_source)
    t = arrays["t"]
    max_window = 0
    total_window = 0
    nonempty = 0
    for pos in positions:
        start = int(np.searchsorted(t, mod.ms(pos.opened), side="left"))
        end = int(np.searchsorted(t, mod.ms(pos.closed), side="right"))
        n = max(0, end - start)
        if n > 0:
            nonempty += 1
            total_window += n
            max_window = max(max_window, n)
    return {
        "positions_total": len(positions),
        "positions_nonempty_ohlc_window": nonempty,
        "max_position_candle_window_length": max_window,
        "avg_position_candle_window_length": total_window / max(nonempty, 1),
        "npz_rows": int(len(t)),
    }


def _stats_top_text(stats: pstats.Stats, limit: int = 30) -> str:
    buf = io.StringIO()
    stats.stream = buf
    stats.strip_dirs().sort_stats("cumulative").print_stats(limit)
    return buf.getvalue()


def _function_cumtime(stats: pstats.Stats, function_name: str) -> float:
    total = 0.0
    for (_filename, _lineno, name), stat in stats.stats.items():
        # stat tuple: cc, nc, tt, ct, callers
        if name == function_name:
            total += float(stat[3])
    return total


def _safe_json(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (np.integer, np.floating)):
        return obj.item()
    raise TypeError(f"not json serializable: {type(obj).__name__}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Profile HYPE DCA parameter search.")
    ap.add_argument("--target-script", default=str(Path(__file__).resolve().parents[1] / "run_hype_dca_parameter_search.py"))
    ap.add_argument("--mode", choices=("champion", "baselines", "sample100", "sample1000", "sample", "unpatched"), default="champion")
    ap.add_argument("--candidate-limit", type=int, default=None, help="Used by --mode sample. sample100/sample1000 override this.")
    ap.add_argument("--random-candidates", type=int, default=6000, help="Superset size for patched sample modes.")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--positions-csv", required=True)
    ap.add_argument("--npz", required=True)
    ap.add_argument("--out-dir", required=True, help="Target script output directory.")
    ap.add_argument("--perf-out-dir", required=True, help="Directory for perf_baseline.json/.prof/top30.")
    ap.add_argument("--entry-source", default="next_bar_open")
    ap.add_argument("--slippage-bp", type=float, default=4.25)
    ap.add_argument("--initial-equity", type=float, default=500.0)
    ap.add_argument("--position-sizing-mode", choices=("fixed", "compound"), default="compound")
    ap.add_argument("--fill-mode", default="close_beyond_skip_boundary")
    ap.add_argument("--strict-fill-mode", default="close_beyond_skip_boundary")
    ap.add_argument("--max-target-notional", type=float, default=500.0)
    ap.add_argument("--min-trade-mtm-pct", type=float, default=-50.0)
    ap.add_argument("--topn", type=int, default=5)
    ap.add_argument("--extra-arg", action="append", default=[], help="Extra raw argument passed to target, e.g. --extra-arg=--leverage --extra-arg=1")
    args = ap.parse_args()

    if args.mode == "sample100":
        candidate_limit = 100
    elif args.mode == "sample1000":
        candidate_limit = 1000
    else:
        candidate_limit = int(args.candidate_limit or 0)

    target_script = Path(args.target_script).resolve()
    perf_out_dir = Path(args.perf_out_dir).resolve()
    perf_out_dir.mkdir(parents=True, exist_ok=True)

    mod = _load_target_module(target_script)
    timers = TimerRegistry()
    # Monkeypatch only for measurement. Replay math remains inside original functions.
    for name in ("simulate_position", "simulate_candidate_rows", "summarize", "write_csv"):
        if hasattr(mod, name):
            setattr(mod, name, timers.wrap(name, getattr(mod, name)))
    _patch_candidate_grid(mod, args.mode, candidate_limit, args.random_candidates, args.seed)

    pos_stats = _position_stats(mod, Path(args.positions_csv), Path(args.npz), args.entry_source)

    target_argv = [
        str(target_script),
        "--positions-csv", str(args.positions_csv),
        "--npz", str(args.npz),
        "--out-dir", str(args.out_dir),
        "--entry-source", str(args.entry_source),
        "--slippage-bp", str(args.slippage_bp),
        "--initial-equity", str(args.initial_equity),
        "--position-sizing-mode", str(args.position_sizing_mode),
        "--fill-mode", str(args.fill_mode),
        "--strict-fill-mode", str(args.strict_fill_mode),
        "--min-trade-mtm-pct", str(args.min_trade_mtm_pct),
        "--topn", str(args.topn),
        "--seed", str(args.seed),
    ]
    if args.max_target_notional is not None:
        target_argv += ["--max-target-notional", str(args.max_target_notional)]
    target_argv += list(args.extra_arg)

    old_argv = sys.argv[:]
    sys.argv = target_argv
    prof = cProfile.Profile()
    t0 = time.perf_counter()
    try:
        prof.enable()
        mod.main()
        prof.disable()
        status = "PASS"
        error = None
    except SystemExit as exc:
        prof.disable()
        status = "PASS" if exc.code in (0, None) else "FAIL"
        error = None if status == "PASS" else f"SystemExit({exc.code})"
        if status == "FAIL":
            raise
    except Exception as exc:
        prof.disable()
        status = "FAIL"
        error = repr(exc)
        raise
    finally:
        total_runtime = time.perf_counter() - t0
        sys.argv = old_argv
        prof_path = perf_out_dir / "perf_baseline.prof"
        prof.dump_stats(str(prof_path))
        stats = pstats.Stats(prof)
        top_txt = _stats_top_text(stats, 30)
        (perf_out_dir / "perf_top30.txt").write_text(top_txt, encoding="utf-8")

        timers_json = timers.as_json()
        candidate_count = 1 if args.mode == "champion" else (3 if args.mode == "baselines" else candidate_limit)
        summary = {
            "status": status,
            "error": error,
            "mode": args.mode,
            "target_script": str(target_script),
            "target_argv": target_argv,
            "output_dir": str(args.out_dir),
            "perf_out_dir": str(perf_out_dir),
            "artifacts": {
                "perf_baseline_json": str(perf_out_dir / "perf_baseline.json"),
                "cprofile_prof": str(prof_path),
                "perf_top30_txt": str(perf_out_dir / "perf_top30.txt"),
            },
            "runtime": {
                "total_seconds": total_runtime,
                "candidate_count": candidate_count,
                "avg_ms_per_candidate": 1000.0 * total_runtime / max(candidate_count, 1),
                "avg_ms_per_position": 1000.0 * total_runtime / max(int(pos_stats["positions_total"]) * max(candidate_count, 1), 1),
            },
            "position_stats": pos_stats,
            "timed_functions": timers_json,
            "cprofile_function_cum_seconds": {
                "simulate_position": _function_cumtime(stats, "simulate_position"),
                "simulate_candidate_rows": _function_cumtime(stats, "simulate_candidate_rows"),
                "summarize": _function_cumtime(stats, "summarize"),
                "write_csv": _function_cumtime(stats, "write_csv"),
            },
            "environment": {
                "python_version": sys.version,
                "platform": platform.platform(),
                "processor": platform.processor(),
                "cwd": os.getcwd(),
            },
        }
        (perf_out_dir / "perf_baseline.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_safe_json), encoding="utf-8")


if __name__ == "__main__":
    main()
