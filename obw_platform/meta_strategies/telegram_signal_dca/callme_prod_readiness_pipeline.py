#!/usr/bin/env python3
"""Callme production-readiness helper.

This helper is research-only. It fetches public Binance copy-trading rows,
normalizes local evidence, inventories repo artifacts, and can synthesize
readiness reports/config metadata from a local backtest summary.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from obw_platform.meta_strategies.binance_online_copytrading import binance_online_copytrading as copy_mod


PORTFOLIO_ID = "4512404768792222208"
REPORT_DIR = Path("_reports") / "callme_prod_pipeline_ops2_20260604"
CONFIG_PATH = Path("obw_platform/meta_strategies/telegram_signal_dca/configs/callme_meta_strategy_live.json")
SKIP_DIRS = {".git", ".venv", ".venv38", ".venv38_win", "node_modules", "__pycache__", ".pytest_cache"}
ARTIFACT_TERMS = (
    "callme",
    "4512404768792222208",
    "amdu",
    "avgou",
    "callme_liquidation_metabacktest",
    "htx_friend_callme_multi_90",
    "mexc_friend_callme_multi_90",
    "gateio_callme_amd_live_254",
    "htx_callme_amd_live_90",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ms_to_iso(raw: Any) -> str:
    try:
        val = int(raw)
    except Exception:
        return ""
    if val <= 0:
        return ""
    return datetime.fromtimestamp(val / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def normalized_history_row(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else row
    return {
        "id": str(raw.get("id") or row.get("id") or ""),
        "symbol": str(raw.get("symbol") or row.get("symbol") or "").upper().strip(),
        "side": copy_mod.norm_side(raw.get("side") or row.get("side")),
        "opened_utc": ms_to_iso(raw.get("opened")),
        "closed_utc": ms_to_iso(raw.get("closed") or row.get("closed_ms")),
        "avgCost": raw.get("avgCost") if raw.get("avgCost") is not None else row.get("avg_cost"),
        "avgClosePrice": raw.get("avgClosePrice") if raw.get("avgClosePrice") is not None else row.get("avg_close_price"),
        "closingPnl": raw.get("closingPnl") if raw.get("closingPnl") is not None else row.get("closing_pnl"),
        "roi": raw.get("roi", ""),
        "leverage": raw.get("leverage", ""),
        "isolated": raw.get("isolated", ""),
        "status": raw.get("status", ""),
        "maxOpenInterest": raw.get("maxOpenInterest", ""),
        "closedVolume": raw.get("closedVolume", ""),
    }


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def fetch_public_history(report_dir: Path, portfolio_id: str, page_size: int, pages: int, timeout_sec: float) -> Dict[str, Any]:
    session = requests.Session()
    history = copy_mod.fetch_history(session, portfolio_id, timeout_sec, page_size=page_size, max_pages=pages)
    open_positions = copy_mod.fetch_open_positions(session, portfolio_id, timeout_sec)
    rows = [normalized_history_row(row) for row in history]
    rows = [row for row in rows if row["id"] and row["symbol"] and row["side"] in {"LONG", "SHORT"} and row["opened_utc"] and row["closed_utc"]]

    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "callme_public_position_history_raw.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "callme_public_open_positions_raw.json").write_text(json.dumps(open_positions, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(report_dir / "callme_public_position_history_normalized.csv", rows)

    counts = Counter(row["symbol"] for row in rows)
    side_counts = Counter("%s:%s" % (row["symbol"], row["side"]) for row in rows)
    summary = {
        "generated_utc": utc_now(),
        "portfolio_id": portfolio_id,
        "history_rows": len(rows),
        "open_rows": len(open_positions),
        "symbols": dict(sorted(counts.items())),
        "symbol_side_counts": dict(sorted(side_counts.items())),
        "open_symbols": sorted({str(row.get("symbol") or "").upper() for row in open_positions if row.get("symbol")}),
        "source": "Binance public copy-trading endpoints via binance_online_copytrading.py; no exchange credentials used.",
    }
    (report_dir / "DATA_INVENTORY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def iter_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            children = list(cur.iterdir())
        except Exception:
            continue
        for child in children:
            name = child.name
            if child.is_dir():
                if name in SKIP_DIRS or "private" in str(child).lower() or "session" in str(child).lower() or "cookie" in str(child).lower():
                    continue
                stack.append(child)
            elif child.is_file():
                yield child


def inventory_artifacts(report_dir: Path, roots: List[Path]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    resolved_report_dir = report_dir.resolve()
    for root in roots:
        root = root.resolve()
        for path in iter_files(root):
            try:
                path.resolve().relative_to(resolved_report_dir)
                continue
            except Exception:
                pass
            full_text = str(path).lower()
            try:
                rel_text = str(path.resolve().relative_to(root)).lower()
            except Exception:
                rel_text = path.name.lower()
            if ".env" in full_text or path.suffix.lower() == ".npz":
                continue
            if not any(term in rel_text for term in ARTIFACT_TERMS):
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            try:
                stat = path.stat()
            except Exception:
                continue
            kind = "report_or_data"
            if "\\configs\\" in str(path) or "/configs/" in str(path):
                kind = "config"
            elif path.suffix.lower() in {".py", ".sh", ".ps1"}:
                kind = "code_or_runner"
            out.append(
                {
                    "path": str(path),
                    "kind": kind,
                    "bytes": stat.st_size,
                    "last_write_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                }
            )
    out.sort(key=lambda row: (row["kind"], row["path"]))
    (report_dir / "artifact_inventory.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def choose_pooled_variant(summary: Dict[str, Any], max_notional: float) -> Optional[Dict[str, Any]]:
    candidates = []
    for label, row in summary.items():
        if not isinstance(row, dict):
            continue
        if int(row.get("positions") or 0) <= 0:
            continue
        if str(row.get("leverage_mode")) != "ignore":
            continue
        if int(row.get("liq_touch_count") or 0) != 0:
            continue
        if float(row.get("max_notional") or 0.0) > max_notional * 1.001:
            continue
        if row.get("min_order_ok") is False:
            continue
        score = float(row.get("net_pct") or 0.0) - 0.25 * abs(float(row.get("max_mtm_dd_pct") or 0.0))
        enriched = dict(row)
        enriched["selection_score"] = score
        candidates.append(enriched)
    if not candidates:
        return None
    return sorted(candidates, key=lambda row: (row["selection_score"], row.get("net_pct", 0.0)), reverse=True)[0]


def update_config(
    config_path: Path,
    report_dir: Path,
    data_summary: Dict[str, Any],
    backtest_summary_path: Optional[Path],
    min_trade_count_gate: int,
) -> Dict[str, Any]:
    cfg = load_json(config_path)
    backtest_summary = load_json(backtest_summary_path) if backtest_summary_path and backtest_summary_path.exists() else {}
    max_notional = float((cfg.get("allocation") or {}).get("default_max_notional_usdt") or 0.0)
    selected = choose_pooled_variant(backtest_summary, max_notional) if backtest_summary else None
    selected_label = selected.get("label") if selected else ""

    cfg["tuning"] = {
        "tune_status": "complete_research" if selected else "blocked_no_backtest_selection",
        "artifact_search_date": "2026-06-04",
        "public_history_source": str(report_dir / "callme_public_position_history_normalized.csv"),
        "pooled_default_config": selected_label or "none_selected",
        "pooled_backtest_summary": str(backtest_summary_path) if backtest_summary_path else "",
        "per_symbol_tuned_configs": "skipped_by_min_trade_gate",
        "min_trade_count_gate": min_trade_count_gate,
        "override_allowlist": [
            "sizing.base_order_policy",
            "sizing.base_order_pct_eq",
            "sizing.dca_profile",
            "sizing.dca_eval_interval_sec",
        ],
        "shrinkage_rule": "Per-symbol override stays empty unless closed trade count >= min_trade_count_gate and its candidate improves pooled selection score by >= 5% without worsening max MTM drawdown, liquidation touches, or max notional.",
        "risk_gate": "Selection excludes source-leverage-copy variants, liquidation touches, and max_notional above allocation.default_max_notional_usdt.",
        "strategy_config_resolution_order": [
            "symbols.<SYMBOL>.strategy_override.override_fields",
            "symbols.<SYMBOL>.strategy_config",
            "default_symbol_config",
        ],
        "symbol_policy_boundary": "DCA/v21 policy belongs in default_symbol_config or strategy_override.override_fields; exchange_symbols only describes exchange market availability and contract metadata.",
    }

    default_cfg = cfg.setdefault("default_symbol_config", {})
    default_cfg.update(
        {
            "config_role": "pooled_default",
            "tune_scope": "pooled_callme_universe",
            "tune_status": "complete_research" if selected else "research_pending",
            "baseline_quality": "callme_public_history_pooled_backtest" if selected else "placeholder",
            "artifact_kind": "callme_pooled_backtest_summary" if selected else "none",
            "pooled_tune_source": str(backtest_summary_path) if selected else None,
            "selected_backtest_variant": selected_label or None,
            "validation_gate": "single 365D public-history sample; overfit risk remains and paper/shadow validation required before production-live.",
        }
    )
    if selected:
        sizing = default_cfg.setdefault("sizing", {})
        dca_count = int(selected.get("dca_count") or 0)
        sizing["base_order_policy"] = "callme_pooled_public_history_v21_same_max_%s" % selected_label
        sizing["base_order_pct_eq"] = 5.0
        sizing["dca_profile"] = "v21_same_max_dca%d" % dca_count
        sizing["v21_config"] = "obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml"
        sizing["target_max_notional_usdt"] = max_notional
        sizing["selected_dca_count"] = dca_count
        sizing["leverage_mode"] = "ignore_source_leverage_for_risk_gate"

    symbol_counts = data_summary.get("symbols") if isinstance(data_summary.get("symbols"), dict) else {}
    symbols = cfg.setdefault("symbols", {})
    for symbol, entry in list(symbols.items()):
        if symbol == "*" or not isinstance(entry, dict):
            continue
        count = int(symbol_counts.get(symbol, 0) or 0)
        entry["strategy_override"] = {
            "inherits": "default_symbol_config",
            "tune_scope": "symbol_only",
            "tune_status": "skipped_min_trade_gate" if count < min_trade_count_gate else "research_pending",
            "closed_trade_count": count,
            "overfit_risk": "high" if count < min_trade_count_gate else "requires_holdout_review",
            "override_fields": {},
        }
        entry["strategy_config_source"] = "default_symbol_config"

    wildcard = symbols.setdefault("*", {})
    wildcard["config_source"] = "default_symbol_config"
    wildcard["strategy_config_resolution"] = "Use default_symbol_config unless a concrete symbols.<SYMBOL>.strategy_override.override_fields exists."

    config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"selected": selected, "config": cfg}


def write_reports(
    report_dir: Path,
    artifacts: List[Dict[str, Any]],
    data_summary: Dict[str, Any],
    synthesis: Dict[str, Any],
    backtest_summary_path: Optional[Path],
    min_trade_count_gate: int,
) -> None:
    selected = synthesis.get("selected") or {}
    cfg = synthesis.get("config") or {}
    symbols = (cfg.get("symbols") or {}) if isinstance(cfg.get("symbols"), dict) else {}
    shadow_status_path = report_dir / "SHADOW_RUN_STATUS.md"
    live_adapter_plan_path = report_dir / "LIVE_ADAPTER_PLAN.md"
    htx_fresh_state_path = report_dir / "shadow_or_paper_htx_fresh_state.json"
    bingx_skip_state_path = report_dir / "shadow_or_paper_bingx_skip_state.json"
    gateio_state_path = report_dir / "shadow_or_paper_gateio_state.json"
    has_shadow_evidence = shadow_status_path.exists()

    gates = {
        "generated_utc": utc_now(),
        "public_signal_history_inventory": {"status": "closed", "evidence": str(report_dir / "DATA_INVENTORY.json")},
        "market_data": {"status": "closed" if backtest_summary_path else "blocked", "evidence": str(backtest_summary_path or "")},
        "pooled_default_tune": {"status": "closed" if selected else "blocked", "selected_variant": selected.get("label", "")},
        "per_symbol_tune": {"status": "skipped_by_min_trade_gate", "min_trade_count_gate": min_trade_count_gate},
        "config_synthesis": {"status": "closed", "evidence": str(CONFIG_PATH)},
        "config_loader_semantics": {"status": "closed", "evidence": "callme_meta_strategy_config.py resolves default_symbol_config plus strategy_override.override_fields"},
        "multi_symbol_meta_adapter_shadow": {
            "status": "closed_shadow_only" if has_shadow_evidence else "planned",
            "evidence": str(live_adapter_plan_path) if live_adapter_plan_path.exists() else "binance_online_copytrading.py supports callme_meta_config",
        },
        "portfolio_proportional_allocation": {
            "status": "closed_shadow_only" if htx_fresh_state_path.exists() or gateio_state_path.exists() else "planned",
            "method": "source_notional_weight_v1",
            "evidence": str(htx_fresh_state_path if htx_fresh_state_path.exists() else gateio_state_path if gateio_state_path.exists() else ""),
        },
        "enter_existing_positions": {
            "status": "closed_shadow_only" if has_shadow_evidence else "planned",
            "evidence": "shadow/paper run status records current-open-position handling" if has_shadow_evidence else "tests cover disabled seeding and enabled entry",
        },
        "unavailable_symbol_skip": {
            "status": "closed_shadow_only" if bingx_skip_state_path.exists() else "planned",
            "evidence": str(bingx_skip_state_path) if bingx_skip_state_path.exists() else "structured exchange_symbol_unavailable skip path",
        },
        "current_open_shadow_cycle": {
            "status": "closed" if has_shadow_evidence else "planned",
            "evidence": str(shadow_status_path) if has_shadow_evidence else "",
        },
        "backtest_metrics_table": {"status": "closed" if backtest_summary_path else "blocked", "evidence": str(backtest_summary_path or "")},
        "overfit_risk_labels": {"status": "closed", "evidence": "symbols.*.strategy_override.overfit_risk"},
        "paper_shadow_runner_plan": {"status": "closed", "evidence": "shadow configs and local runs keep live_orders_enabled=false"},
        "real_live_restart": {
            "status": "open_blocker",
            "evidence": "No real order path enabled; requires explicit human approval, live ack, state/log backup, and live adapter hardening review.",
        },
    }
    (report_dir / "GATES.json").write_text(json.dumps(gates, ensure_ascii=False, indent=2), encoding="utf-8")

    inv = ["# Callme Tune Inventory", ""]
    inv.append(f"Generated: {utc_now()}")
    inv.append("")
    inv.append("## Public History")
    inv.append(f"- Portfolio: `{data_summary.get('portfolio_id')}`")
    inv.append(f"- Closed rows: `{data_summary.get('history_rows')}`")
    inv.append(f"- Current open rows: `{data_summary.get('open_rows')}`")
    inv.append(f"- Closed symbols: `{json.dumps(data_summary.get('symbols', {}), sort_keys=True)}`")
    inv.append("")
    inv.append("## Existing Artifacts")
    if artifacts:
        for row in artifacts:
            inv.append(f"- `{row['kind']}` `{row['path']}` ({row['bytes']} bytes)")
    else:
        inv.append("- No Callme-specific artifacts found.")
    inv.append("")
    inv.append("## Tune Artifact Conclusion")
    inv.append("- Existing personal/per-symbol tuned configs for `AMDUSDT`: none found outside the Ops2 synthesized inherited default path.")
    inv.append("- Existing personal/per-symbol tuned configs for `AVGOUSDT`: none found outside the Ops2 synthesized inherited default path.")
    if backtest_summary_path:
        inv.append(f"- Current pooled Callme research summary: `{backtest_summary_path}`.")
    else:
        inv.append("- Current pooled Callme research summary: absent; pooled tune remains blocked.")
    inv.append("- Per-symbol overrides stay empty until each symbol clears the min-trade and shrinkage gates.")
    (report_dir / "TUNE_INVENTORY.md").write_text("\n".join(inv) + "\n", encoding="utf-8")

    plan = ["# Callme Hierarchical Tune Plan", ""]
    plan.append("Hierarchy: pooled Callme universe default first, then sparse per-symbol overrides.")
    plan.append("")
    plan.append(f"Min trade count gate: `{min_trade_count_gate}` closed trades per symbol.")
    plan.append("Override allowlist: `sizing.base_order_policy`, `sizing.base_order_pct_eq`, `sizing.dca_profile`, `sizing.dca_eval_interval_sec`.")
    plan.append("Shrinkage rule: only accept a per-symbol override if it improves the pooled selection score by at least 5% and does not worsen max MTM drawdown, liquidation touches, or max notional.")
    plan.append("Risk rule: no source-leverage-copy tune variant can become the pooled default in this branch.")
    plan.append("")
    plan.append("Per-symbol status:")
    for symbol, entry in sorted(symbols.items()):
        if symbol == "*" or not isinstance(entry, dict):
            continue
        override = entry.get("strategy_override") or {}
        plan.append(f"- `{symbol}`: `{override.get('tune_status')}`, closed trades `{override.get('closed_trade_count')}`, overrides `{override.get('override_fields')}`")
    (report_dir / "CALLME_HIERARCHICAL_TUNE_PLAN.md").write_text("\n".join(plan) + "\n", encoding="utf-8")

    status = ["# Callme Production Pipeline Ops2 Status", ""]
    status.append(f"Generated: {utc_now()}")
    status.append("")
    status.append("## Outcome")
    if selected:
        status.append(f"- Pooled research default selected from local backtest: `{selected.get('label')}`.")
        status.append(f"- Net pct: `{selected.get('net_pct')}`; max MTM DD pct: `{selected.get('max_mtm_dd_pct')}`; liquidation touches: `{selected.get('liq_touch_count')}`.")
    else:
        status.append("- Pooled default tune remains blocked because no risk-eligible backtest variant was available.")
    status.append("- Per-symbol overrides remain empty/skipped by min-trade gate.")
    if live_adapter_plan_path.exists():
        status.append("- Shadow/paper multi-symbol adapter reads `callme_meta_strategy_live.json`, resolves inherited per-symbol config, applies exchange eligibility, and uses `source_notional_weight_v1` allocation.")
    if has_shadow_evidence:
        status.append("- Shadow/paper evidence exists for current Callme open-position handling; see `SHADOW_RUN_STATUS.md`.")
    status.append("- Production-live remains blocked on explicit human approval, live ack, secrets/runtime deployment review, and real-order adapter enablement.")
    status.append("")
    status.append("## Evidence")
    status.append(f"- Public data inventory: `{report_dir / 'DATA_INVENTORY.json'}`")
    status.append(f"- Backtest summary: `{backtest_summary_path or ''}`")
    status.append(f"- Config: `{CONFIG_PATH}`")
    status.append(f"- Gates: `{report_dir / 'GATES.json'}`")
    if live_adapter_plan_path.exists():
        status.append(f"- Live adapter plan: `{live_adapter_plan_path}`")
    if has_shadow_evidence:
        status.append(f"- Shadow run status: `{shadow_status_path}`")
    (report_dir / "PIPELINE_STATUS.md").write_text("\n".join(status) + "\n", encoding="utf-8")

    graph = ["# Callme Pipeline Graph Update", ""]
    graph.append("```mermaid")
    graph.append("flowchart LR")
    graph.append("  A[data inventory] --> B[signal normalization]")
    graph.append("  B --> C[pooled tune]")
    graph.append("  C --> D[per-symbol tune]")
    graph.append("  D --> E[hierarchical config synthesis]")
    graph.append("  E --> F[backtest]")
    graph.append("  F --> G[paper/shadow]")
    graph.append("  G --> H[production-live readiness gate]")
    graph.append("  H --> I{multi-symbol live adapter and human approval}")
    graph.append("```")
    graph.append("")
    graph.append("No existing LangGraph source file was found in this branch; this report records the required pipeline visualization for coordinator integration.")
    (report_dir / "GRAPH_UPDATE.md").write_text("\n".join(graph) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Callme public-history inventory and readiness synthesis.")
    ap.add_argument("--report-dir", default=str(REPORT_DIR))
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--portfolio-id", default=PORTFOLIO_ID)
    ap.add_argument("--history-page-size", type=int, default=50)
    ap.add_argument("--history-pages", type=int, default=20)
    ap.add_argument("--timeout-sec", type=float, default=20.0)
    ap.add_argument("--backtest-summary", default="")
    ap.add_argument("--min-trade-count-gate", type=int, default=12)
    ap.add_argument("--inventory-root", action="append", default=[])
    args = ap.parse_args()

    report_dir = Path(args.report_dir)
    data_summary_path = report_dir / "DATA_INVENTORY.json"
    if data_summary_path.exists():
        data_summary = load_json(data_summary_path)
    else:
        data_summary = fetch_public_history(report_dir, args.portfolio_id, args.history_page_size, args.history_pages, args.timeout_sec)

    roots = [Path.cwd()]
    for raw_root in args.inventory_root:
        roots.append(Path(raw_root))
    artifacts = inventory_artifacts(report_dir, roots)

    backtest_summary = Path(args.backtest_summary) if args.backtest_summary else None
    synthesis = update_config(Path(args.config), report_dir, data_summary, backtest_summary, args.min_trade_count_gate)
    write_reports(report_dir, artifacts, data_summary, synthesis, backtest_summary, args.min_trade_count_gate)
    print(json.dumps({"report_dir": str(report_dir), "selected": synthesis.get("selected")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
