#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bt_live_paper_runner_separated_universe.py
------------------------------------------
A drop‑in runner for backtest/paper/live with *universe* support and lightweight
overhead. It keeps the same CLI surface but additionally:
  - Reads a universe file (and/or allow/deny CSV) and injects it into cfg["universe"]
  - Exposes hints to down‑stream builders (ENV + cfg keys) to restrict symbols early
  - For PAPER(API) tries to call your project's `runners.paper_api_runner.run_paper_api`.
    If unavailable, falls back to a heartbeat loop so the script still runs.

This file does **not** change your strategy logic.
"""

from __future__ import annotations

import os, sys, argparse, time
from typing import List, Tuple, Dict, Any
from utils.trace_calls import auto_instrument_module, get_store

TRACE = False

# ------------------------- Utilities -------------------------

def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def _load_yaml_or_json(path: str) -> Dict[str, Any]:
    if path.endswith(".json"):
        import json as _json
        return _json.loads(_read_text(path))
    import yaml as _yaml
    return _yaml.safe_load(_read_text(path))

def _split_csv(s: str) -> List[str]:
    if not s:
        return []
    return [x.strip() for x in str(s).split(",") if x.strip()]

def _read_universe_file(path: str) -> List[str]:
    syms = []
    if not path:
        return syms
    try:
        # Normalise the path similar to the backtester.  If a relative path
        # is provided we treat it as residing in the local ``universe``
        # directory next to this script.  Only keep the basename to avoid
        # doubling the directory when users pass ``universe/foo.txt``.
        if not os.path.isabs(path):
            udir = os.path.join(os.path.dirname(__file__), "universe")
            path = os.path.join(udir, os.path.basename(path))
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    syms.append(ln)
    except Exception:
        pass
    return syms

def _merge_universe(cfg_uni: Dict[str, Any], file_syms: List[str], allow_cli: List[str], deny_cli: List[str]) -> Dict[str, Any]:
    uni = dict(cfg_uni or {})
    allow = set(uni.get("allow", []) or [])
    deny  = set(uni.get("deny",  []) or [])
    for s in file_syms: allow.add(s)
    for s in allow_cli: allow.add(s)
    for s in deny_cli:  deny.add(s)
    if file_syms and "file" not in uni:
        uni["file"] = "<cli>"
    uni["allow"] = sorted(allow)
    uni["deny"]  = sorted(deny)
    return uni

def _hint_restrict_symbols(cfg: Dict[str, Any], allow: List[str]) -> None:
    """Provide 'strong hints' for downstream code to restrict early."""
    if allow:
        # 1) Put into cfg in multiple conventional places
        cfg.setdefault("universe", {})["allow"] = list(allow)
        cfg.setdefault("md_builder", {})["restrict_to_symbols"] = list(allow)
        cfg["symbols_whitelist"] = list(allow)
        # 2) Also export environment variables (many builders read these)
        os.environ["RS_UNIVERSE_ALLOW"] = ",".join(allow)
        os.environ["RS_SYMBOLS_WHITELIST"] = ",".join(allow)

# ------------------------- Backtest/LIVE delegates -------------------------

def _run_backtest(cfg_path: str, limit_bars: int | None):
    bt_entry = os.path.join(os.getcwd(), "backtester_core.py")
    if not os.path.exists(bt_entry):
        raise SystemExit("backtester_core.py not found. Run from the project root.")
    cmd = [sys.executable, bt_entry, "--cfg", cfg_path]
    if limit_bars and int(limit_bars) > 0:
        cmd += ["--limit-bars", str(int(limit_bars))]
    print("[backtest] exec:", " ".join(cmd))
    os.execvp(sys.executable, cmd)

def _run_live(cfg: Dict[str, Any], args):
    # Try to call your live runner
    try:
        try:
            from .runners import live_runner_2 as _lr
        except Exception:  # allow running from repo root or package
            import runners.live_runner_2 as _lr  # type: ignore
        if TRACE:
            auto_instrument_module(_lr, cfg=cfg,
                include=cfg.get("trace_calls", {}).get("include"),
                exclude=cfg.get("trace_calls", {}).get("exclude"))
        return _lr.run_live(cfg, args)
    except Exception as e:
        print("[live] runners.live_runner.run_live not available:", e)
        print("[live] Falling back to heartbeat... (CTRL+C to exit)")
        while True:
            print(".", end="", flush=True); time.sleep(max(1, int(getattr(args, "poll_sec", 10) or 10)))

def _run_paper_api(cfg: Dict[str, Any], args):
    # Try your project's paper runner first
    try:
        try:
            from .runners.paper_api_runner import run_paper_api as _run_paper_api
        except Exception:  # allow running from repo root or package
            from runners.paper_api_runner import run_paper_api as _run_paper_api  # type: ignore
        return _run_paper_api(cfg, args)
    except Exception as e:
        print("[paper-api] runners.paper_api_runner.run_paper_api not available:", e)
        print("[paper-api] Using minimal fallback loop with heartbeat only.")
        poll = int(getattr(args, "poll_sec", 10) or 10)
        dots=0
        while True:
            print(".", end="", flush=True); dots+=1
            if dots%60==0: print("")
            time.sleep(poll)

# ------------------------- Main CLI -------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["backtest", "paper", "live"], required=True)
    ap.add_argument("--paper-source", choices=["db","api"], default="api")
    ap.add_argument("--orders-db", default="", help="Orders DB path for paper/live if downstream expects it")

    ap.add_argument("--cfg", required=True, help="YAML/JSON config path")
    ap.add_argument("--db", help="Cache DB path for paper --paper-source=db")

    # Common runtime options
    ap.add_argument("--results-dir", default="",
                    help="Results directory (default: auto under _reports/_live)")
    ap.add_argument("--limit-bars", type=int, default=0)

    # Session & cache
    ap.add_argument("--session-db", default="",
                    help="Session DB path (default: session.sqlite in results dir)")
    ap.add_argument("--cache-out", default="",
                    help="Cache output path (default: combined_cache_session.db in results dir)")
    ap.add_argument("--hour-cache", choices=["off","save","load"], default="off")

    # Exchange/API for PAPER API + LIVE
    ap.add_argument("--env-file", default="")
    ap.add_argument("--exchange", default="bingx")
    ap.add_argument("--symbol-format", default="usdtm")
    ap.add_argument("--poll-sec", type=int, default=2)
    ap.add_argument("--bar-delay-sec", type=int, default=1)
    ap.add_argument("--limit_klines", type=int, default=180)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--heat-report", action="store_true")

    # Universe controls
    ap.add_argument("--universe-file", default="", help="Text file: one symbol per line")
    ap.add_argument("--allow-symbols", default="", help="Comma-separated allow-list")
    ap.add_argument("--deny-symbols",  default="", help="Comma-separated deny-list")

    args = ap.parse_args()
    if not hasattr(args, "orders_db"):
        setattr(args, "orders_db", "")

    cfg_name = os.path.splitext(os.path.basename(args.cfg))[0]

    # Load config early to extract timeframe for results directory
    cfg = _load_yaml_or_json(args.cfg)
    timeframe = str(cfg.get("timeframe") or "na")

    global TRACE
    TRACE = (cfg.get("trace_calls", {}).get("enabled", False) or bool(int(os.environ.get("TRACE_CALLS","0"))))
    if TRACE:
        auto_instrument_module(sys.modules[__name__], cfg=cfg,
            include=cfg.get("trace_calls", {}).get("include"),
            exclude=cfg.get("trace_calls", {}).get("exclude"))
        get_store(cfg)

    if not args.results_dir:
        args.results_dir = os.path.join("_reports", "_live", f"livecfg_{cfg_name}_{timeframe}")
    os.makedirs(args.results_dir, exist_ok=True)

    # Resolve session/cache paths relative to results directory if needed
    if args.session_db:
        if not os.path.isabs(args.session_db) and os.path.dirname(args.session_db) == "":
            args.session_db = os.path.join(args.results_dir, args.session_db)
    else:
        args.session_db = os.path.join(args.results_dir, "session.sqlite")

    if args.cache_out:
        if not os.path.isabs(args.cache_out) and os.path.dirname(args.cache_out) == "":
            args.cache_out = os.path.join(args.results_dir, args.cache_out)
    else:
        args.cache_out = os.path.join(args.results_dir, "combined_cache_session.db")

    if not args.orders_db:
        args.orders_db = args.session_db

    print(f"[results] dir: {args.results_dir}")

    # Build universe
    cfg_uni = cfg.get("universe", {}) or {}
    file_syms = _read_universe_file(args.universe_file or cfg.get("universe_file", ""))
    allow_cli = _split_csv(args.allow_symbols)
    deny_cli  = _split_csv(args.deny_symbols)
    uni = _merge_universe(cfg_uni, file_syms, allow_cli, deny_cli)
    cfg["universe"] = uni

    # Strong hints to restrict early (speed-up for small universes)
    _hint_restrict_symbols(cfg, uni.get("allow", []))

    # Export session/cache hints for downstream code
    if args.orders_db: os.environ["RS_ORDERS_DB"] = args.orders_db
    if args.session_db: os.environ["RS_SESSION_DB"] = args.session_db
    if args.cache_out:  os.environ["RS_CACHE_OUT"]  = args.cache_out
    if args.hour_cache: os.environ["RS_HOUR_CACHE"] = args.hour_cache

    # Backtest → defer to backtester
    if args.mode == "backtest":
        return _run_backtest(args.cfg, args.limit_bars if args.limit_bars>0 else None)

    # PAPER
    if args.mode == "paper":
        if args.paper_source == "db":
            if not args.db:
                raise SystemExit("--db is required for PAPER with --paper-source=db")
            # You can have your DB paper runner here if needed.
            print("[paper-db] is not implemented in this runner. Use your DB playback tool.")
            return 2
        # PAPER API
        # Expose a compact runtime bundle so downstream can avoid global lookups
        cfg.setdefault("_runner", {})
        cfg["_runner"].update({
            "results_dir": args.results_dir,
            "session_db":  args.session_db,
            "orders_db":   args.orders_db,
            "cache_out":   args.cache_out,
            "hour_cache":  args.hour_cache,
            "poll_sec":    args.poll_sec,
            "bar_delay_sec": args.bar_delay_sec,
            "limit_klines":  args.limit_klines,
            "exchange":    args.exchange,
            "symbol_format": args.symbol_format,
            "heat_report": args.heat_report,
            "debug":       args.debug,
        })
        # Hand off to your project paper runner (with fallback)
        return _run_paper_api(cfg, args)

    # LIVE
    return _run_live(cfg, args)


if __name__ == "__main__":
    main()
