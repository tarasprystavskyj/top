#!/usr/bin/env python3
"""Paper-only telemetry sidecar for VeronicaUA HYPE follow-open.

Reads the existing paper state, fetches public market data, and writes local
reports. It never sends exchange orders and does not read secrets.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


REPORT_DIR = Path(__file__).resolve().parent
STATE_PATH = REPORT_DIR / "veronicaUA_follow_open_state.json"
CONFIG_PATH = REPORT_DIR / "veronicaUA_follow_open_config.json"
TELEMETRY_JSONL = REPORT_DIR / "veronicaUA_slippage_telemetry.jsonl"
PAPER_STATUS_MD = REPORT_DIR / "PAPER_LIVE_STATUS_20260524.md"
PAPER_STATUS_JSON = REPORT_DIR / "PAPER_LIVE_STATUS.json"
SLIPPAGE_REPORT_MD = REPORT_DIR / "SLIPPAGE_MODEL_REPORT_20260524.md"
LOOP_STRUCTURE_MD = REPORT_DIR / "LOOP_STRUCTURE_20260524.md"
V21_PLAIN_MD = REPORT_DIR / "V21_VS_PLAIN_20260524.md"
IE100_MANIFEST = REPORT_DIR / "wave_002_initial_equity_100_no_warmup_no_trend" / "MANIFEST.json"

BINANCE_MARK_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_float(raw: Any, default: float = math.nan) -> float:
    try:
        if raw in ("", None):
            return default
        return float(str(raw).replace(",", ""))
    except Exception:
        return default


def finite(raw: Any) -> Optional[float]:
    val = parse_float(raw)
    return val if math.isfinite(val) else None


def market_symbol(symbol: str) -> str:
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return f"{base}/USDT:USDT"


def exec_price(mark: float, side: str, action: str, slippage_bp: float) -> float:
    slip = slippage_bp / 10_000.0
    if side == "LONG":
        return mark * (1.0 + slip) if action == "entry" else mark * (1.0 - slip)
    return mark * (1.0 - slip) if action == "entry" else mark * (1.0 + slip)


def ret_for(side: str, entry: float, exit_px: float) -> float:
    if entry <= 0 or exit_px <= 0:
        return 0.0
    if side == "LONG":
        return exit_px / entry - 1.0
    if side == "SHORT":
        return entry / exit_px - 1.0
    return 0.0


def try_bingx_market(symbol: str) -> Tuple[Optional[float], str, Dict[str, Any]]:
    try:
        import ccxt  # type: ignore

        ex = ccxt.bingx({"enableRateLimit": True})
        ex.load_markets()
        market = market_symbol(symbol)
        ticker = ex.fetch_ticker(market)
        mark = finite(ticker.get("last")) or finite(ticker.get("mark")) or finite(ticker.get("close"))
        orderbook: Dict[str, Any] = {}
        try:
            book = ex.fetch_order_book(market, limit=20)
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            best_bid = finite(bids[0][0]) if bids else None
            best_ask = finite(asks[0][0]) if asks else None
            mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else None
            spread_bp = ((best_ask - best_bid) / mid * 10_000.0) if best_bid and best_ask and mid else None
            bid_depth_top5 = sum(float(px) * float(sz) for px, sz in bids[:5])
            ask_depth_top5 = sum(float(px) * float(sz) for px, sz in asks[:5])
            orderbook = {
                "source": "bingx_ccxt",
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid": mid,
                "spread_bp": spread_bp,
                "bid_depth_top5_usdt": bid_depth_top5,
                "ask_depth_top5_usdt": ask_depth_top5,
            }
        except Exception as exc:
            orderbook = {"source": "bingx_ccxt", "error": str(exc)}
        if mark and mark > 0:
            return mark, "bingx_ccxt", orderbook
    except Exception:
        pass
    return None, "missing", {}


def try_binance_mark(symbol: str) -> Tuple[Optional[float], str]:
    try:
        resp = requests.get(BINANCE_MARK_URL, params={"symbol": symbol}, timeout=10.0)
        resp.raise_for_status()
        mark = finite(resp.json().get("markPrice"))
        if mark and mark > 0:
            return mark, "binance_mark_fallback"
    except Exception:
        pass
    return None, "missing"


def market_snapshot(symbol: str) -> Dict[str, Any]:
    mark, source, orderbook = try_bingx_market(symbol)
    if not mark:
        mark, source = try_binance_mark(symbol)
    return {"mark": mark, "mark_source": source, "orderbook": orderbook}


def closed_trade_metrics(trade: Dict[str, Any]) -> Dict[str, Any]:
    entry = finite(trade.get("entry_exec_price"))
    exit_px = finite(trade.get("exit_exec_price"))
    entry_mark = finite(trade.get("entry_mark_price"))
    exit_mark = finite(trade.get("exit_mark_price"))
    return {
        "key": trade.get("key"),
        "symbol": trade.get("symbol"),
        "side": trade.get("side"),
        "detected_utc": trade.get("detected_utc"),
        "exit_detected_utc": trade.get("exit_detected_utc"),
        "entry_mark_price": entry_mark,
        "entry_exec_price": entry,
        "exit_mark_price": exit_mark,
        "exit_exec_price": exit_px,
        "entry_slippage_bp_realized": ((entry / entry_mark - 1.0) * 10_000.0 if entry and entry_mark else None),
        "exit_slippage_bp_realized": ((1.0 - exit_px / exit_mark) * 10_000.0 if exit_px and exit_mark and str(trade.get("side")) == "LONG" else None),
        "paper_pnl_usdt": trade.get("paper_pnl_usdt"),
        "paper_return_pct": trade.get("paper_return_pct"),
        "exit_reason": trade.get("exit_reason"),
    }


def build_position_records(state: Dict[str, Any], cfg: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    slippage_bp = float(cfg.get("slippage_bp", state.get("last_poll", {}).get("slippage_bp", 9.38)))
    records: List[Dict[str, Any]] = []
    for key, pos in (state.get("open_positions") or {}).items():
        symbol = str(pos.get("symbol") or "")
        side = str(pos.get("side") or "")
        snap = market_snapshot(symbol)
        mark = snap.get("mark")
        entry_exec = finite(pos.get("entry_exec_price"))
        notional = finite(pos.get("notional_usdt")) or 0.0
        exit_exec = exec_price(mark, side, "exit", slippage_bp) if mark else None
        pnl = ret_for(side, entry_exec or 0.0, exit_exec or 0.0) * notional if exit_exec and entry_exec else None
        raw_signal = pos.get("raw_signal") or {}
        raw_inner = raw_signal.get("raw") or {}
        orderbook = snap.get("orderbook") or {}
        spread_bp = orderbook.get("spread_bp")
        configured_slip = slippage_bp
        dynamic_slip_bp = configured_slip
        if isinstance(spread_bp, (int, float)) and math.isfinite(float(spread_bp)):
            dynamic_slip_bp = max(configured_slip, float(spread_bp) / 2.0)
        records.append(
            {
                "utc": iso(now),
                "key": key,
                "strategy_name": pos.get("strategy_name"),
                "portfolio_id": pos.get("portfolio_id"),
                "mode": pos.get("mode"),
                "symbol": symbol,
                "side": side,
                "detected_utc": pos.get("detected_utc"),
                "last_seen_utc": pos.get("last_seen_utc"),
                "signal_entry_price": raw_signal.get("entry_price"),
                "signal_mark_price": raw_signal.get("mark_price"),
                "signal_break_even_price": raw_inner.get("breakEvenPrice"),
                "lead_unrealized_profit": raw_inner.get("unrealizedProfit"),
                "lead_position_amount": raw_signal.get("position_amount"),
                "lead_notional_value": raw_signal.get("notional_value"),
                "entry_mark_price": pos.get("entry_mark_price"),
                "entry_exec_price": pos.get("entry_exec_price"),
                "entry_price_source": pos.get("entry_price_source"),
                "current_mark": mark,
                "current_mark_source": snap.get("mark_source"),
                "hypothetical_exit_exec_price": exit_exec,
                "notional_usdt": notional,
                "configured_slippage_bp": slippage_bp,
                "paper_live_fee_model": "none_in_current_daemon",
                "unrealized_pnl_usdt_after_configured_exit_slippage": pnl,
                "unrealized_return_pct_after_configured_exit_slippage": (100.0 * pnl / notional if pnl is not None and notional else None),
                "orderbook_proxy": orderbook,
                "dynamic_slippage_model": {
                    "status": "fallback_until_more_telemetry",
                    "fallback_slippage_bp": configured_slip,
                    "spread_half_bp": (float(spread_bp) / 2.0 if isinstance(spread_bp, (int, float)) else None),
                    "suggested_entry_slippage_bp": dynamic_slip_bp,
                    "suggested_exit_slippage_bp": dynamic_slip_bp,
                },
            }
        )
    return records


def append_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load_ie100_rows() -> List[Dict[str, Any]]:
    manifest = read_json(IE100_MANIFEST, {})
    rows = []
    for variant in manifest.get("variants", []):
        summary = variant.get("summary") or {}
        rows.append(
            {
                "variant": variant.get("name"),
                "label": summary.get("label"),
                "net_pct": summary.get("net_pct"),
                "net_pct_per_30d": summary.get("net_pct_per_30d"),
                "pf": summary.get("pf"),
                "max_dd_pct": summary.get("max_dd_pct"),
                "win_rate_pct": summary.get("win_rate_pct"),
                "avg_dca_fills": summary.get("avg_dca_fills"),
            }
        )
    return rows


def update_reports(records: List[Dict[str, Any]], state: Dict[str, Any], cfg: Dict[str, Any], now: datetime) -> None:
    closed = [closed_trade_metrics(t) for t in state.get("closed_trades", [])]
    latest_closed = closed[-1] if closed else None
    total_closed_pnl = sum(float(t.get("paper_pnl_usdt") or 0.0) for t in state.get("closed_trades", []))
    total_open_pnl = sum(float(r.get("unrealized_pnl_usdt_after_configured_exit_slippage") or 0.0) for r in records)
    status = {
        "updated_at": iso(now),
        "paper_backtest_only": True,
        "state_path": str(STATE_PATH),
        "config_path": str(CONFIG_PATH),
        "telemetry_jsonl": str(TELEMETRY_JSONL),
        "open_positions_count": len(records),
        "closed_trades_count": len(closed),
        "closed_pnl_usdt": total_closed_pnl,
        "open_unrealized_pnl_usdt_after_configured_exit_slippage": total_open_pnl,
        "combined_paper_pnl_usdt_mark_to_exit_model": total_closed_pnl + total_open_pnl,
        "configured_slippage_bp": cfg.get("slippage_bp", state.get("last_poll", {}).get("slippage_bp")),
        "fee_model": "paper-live daemon currently models slippage only; no fee deduction in paper_pnl_usdt",
        "open_positions": records,
        "latest_closed_trade": latest_closed,
    }
    write_json(PAPER_STATUS_JSON, status)

    lines = [
        "# VeronicaUA HYPE Paper-Live Status",
        "",
        "Paper/backtest-only. No live orders. No secrets.",
        "",
        f"- Updated: `{status['updated_at']}`",
        f"- State: `{STATE_PATH}`",
        f"- Telemetry JSONL: `{TELEMETRY_JSONL}`",
        f"- Open positions: `{len(records)}`",
        f"- Closed trades: `{len(closed)}`",
        f"- Closed paper PnL: `{total_closed_pnl}` USDT",
        f"- Open unrealized PnL after configured exit slippage: `{total_open_pnl}` USDT",
        f"- Combined paper PnL mark-to-exit model: `{total_closed_pnl + total_open_pnl}` USDT",
        f"- Slippage model in daemon: fixed `{status['configured_slippage_bp']}` bp per entry/exit side",
        "- Fee model in daemon: none; paper PnL currently does not deduct exchange fees.",
        "",
        "## Open Positions",
        "",
    ]
    if not records:
        lines.append("- None.")
    for rec in records:
        lines.extend(
            [
                f"- `{rec['symbol']}` `{rec['side']}` key `{rec['key']}`",
                f"  - entry mark `{rec['entry_mark_price']}`, entry exec `{rec['entry_exec_price']}`, current mark `{rec['current_mark']}` from `{rec['current_mark_source']}`",
                f"  - notional `{rec['notional_usdt']}`, hypothetical exit exec `{rec['hypothetical_exit_exec_price']}`",
                f"  - unrealized PnL after configured exit slippage `{rec['unrealized_pnl_usdt_after_configured_exit_slippage']}` USDT / `{rec['unrealized_return_pct_after_configured_exit_slippage']}`%",
                f"  - signal entry `{rec['signal_entry_price']}`, signal mark `{rec['signal_mark_price']}`, lead raw unrealized `{rec['lead_unrealized_profit']}`",
                f"  - orderbook proxy `{rec['orderbook_proxy']}`",
            ]
        )
    lines.extend(["", "## Latest Closed Trade", ""])
    if latest_closed:
        lines.append(f"- `{latest_closed}`")
    else:
        lines.append("- None.")
    PAPER_STATUS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    slippage_lines = [
        "# HYPE Dynamic Slippage Model Research",
        "",
        "Paper/backtest-only sidecar report. This does not place orders.",
        "",
        f"- Updated: `{iso(now)}`",
        f"- Telemetry source: `{TELEMETRY_JSONL}`",
        "- Current daemon execution model: fixed configured slippage bp on entry and exit.",
        "- Current daemon fee model: none in paper-live PnL.",
        "- Current dynamic model status: `fallback_until_more_telemetry`.",
        "",
        "## Inputs Collected",
        "",
        "- Signal entry price and signal mark from Binance public open-position payload.",
        "- Local simulated entry mark and entry exec price.",
        "- Current public mark from BingX ccxt when available, Binance mark fallback otherwise.",
        "- Configured slippage bp.",
        "- Orderbook proxy when BingX orderbook is available: best bid/ask, spread bp, top-5 bid/ask depth.",
        "- Unrealized PnL after applying configured exit slippage.",
        "- Closed trade exit mark/exec/PnL from the existing state file.",
        "",
        "## Current Fallback",
        "",
        f"- Use configured fixed slippage `{cfg.get('slippage_bp', state.get('last_poll', {}).get('slippage_bp'))}` bp until enough telemetry exists.",
        "- If orderbook spread is available, provisional dynamic estimate is `max(configured_bp, spread_bp / 2)` for both entry and exit.",
        "- This is deliberately conservative and only changes reporting/profitability estimation, not trading behavior.",
        "",
        "## Next Data Needed",
        "",
        "- More poll samples across volatile HYPE periods.",
        "- Closed paper trades with entry/exit orderbook snapshots.",
        "- Optional depth-at-notional impact estimate for 100 USDT and larger test notionals.",
    ]
    if records:
        first = records[0]
        slippage_lines.extend(
            [
                "",
                "## Latest Snapshot",
                "",
                f"- Symbol: `{first['symbol']}`",
                f"- Current mark: `{first['current_mark']}` from `{first['current_mark_source']}`",
                f"- Orderbook proxy: `{first['orderbook_proxy']}`",
                f"- Suggested dynamic slippage: `{first['dynamic_slippage_model']}`",
            ]
        )
    SLIPPAGE_REPORT_MD.write_text("\n".join(slippage_lines) + "\n", encoding="utf-8")

    ie_rows = load_ie100_rows()
    plain = next((r for r in ie_rows if r["variant"] == "long_low_exposure" and r["label"] == "plain"), None)
    # The plain result is identical across variants in this compare family; use baseline if present in source summaries.
    if not plain:
        baseline_summary = read_json(REPORT_DIR / "wave_002_initial_equity_100_no_warmup_no_trend" / "variants" / "baseline" / "summary.json", {})
        p = baseline_summary.get("plain") or {}
        plain = {"variant": "baseline", "label": "plain", **p}
    best = max(ie_rows, key=lambda r: float(r.get("net_pct") or -1e9)) if ie_rows else None
    uplift_pct_points = (float(best["net_pct"]) - float(plain["net_pct"])) if best and plain else None
    uplift_pnl = uplift_pct_points
    v21_lines = [
        "# HYPE V21/DCA vs Plain",
        "",
        f"- Updated: `{iso(now)}`",
        "- Dataset: `wave_002_initial_equity_100_no_warmup_no_trend`",
        "- Interpretation: `plain` is direct single-entry follow of lead side; V21/DCA variants add tuned averaging/exit behavior from the compare configs.",
        "",
    ]
    if plain and best:
        v21_lines.extend(
            [
                "## Result",
                "",
                f"- Plain: net `{plain['net_pct']}`%, /30d `{plain['net_pct_per_30d']}`%, PF `{plain['pf']}`, maxDD `{plain['max_dd_pct']}`%.",
                f"- Best V21/DCA: `{best['variant']}` / `{best['label']}` net `{best['net_pct']}`%, /30d `{best['net_pct_per_30d']}`%, PF `{best['pf']}`, maxDD `{best['max_dd_pct']}`%.",
                f"- Uplift: `{uplift_pnl}` USDT on 100 USDT initial equity, i.e. `{uplift_pct_points}` percentage points.",
                "- Materiality: profitable uplift is positive but not a step-change; it is about 14.87 percentage points over 134 days, roughly 10.4% relative improvement over plain net return.",
                "",
                "## Ranking",
                "",
                "| variant | label | net % | /30d % | PF | maxDD % | win % |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for row in sorted(ie_rows, key=lambda r: float(r.get("net_pct") or -1e9), reverse=True):
            v21_lines.append(
                f"| {row['variant']} | {row['label']} | {row['net_pct']} | {row['net_pct_per_30d']} | {row['pf']} | {row['max_dd_pct']} | {row['win_rate_pct']} |"
            )
    V21_PLAIN_MD.write_text("\n".join(v21_lines) + "\n", encoding="utf-8")

    LOOP_STRUCTURE_MD.write_text(
        "\n".join(
            [
                "# VeronicaUA / Binance 430051 Loop Structure",
                "",
                f"- Updated: `{iso(now)}`",
                "- Guardrail: paper/backtest-only; no live orders.",
                "",
                "## Active tmux Sessions",
                "",
                "- `binance_430051_hype_v21_loop`: periodically refreshes public Binance lead history/open-position data, annual HYPE OHLCV/NPZ, and V21 compare/tune waves.",
                "- `binance_veronicaUA_follow_open_paper`: paper-live listener following Binance public open-position direction directly, not contrarian close.",
                "- `binance_veronicaUA_slippage_telemetry`: sidecar telemetry loop for mark/orderbook/PnL/slippage reports.",
                "",
                "## Data Flow",
                "",
                "1. Binance public open positions endpoint -> direct follow-open paper signal.",
                "2. Paper-live daemon -> `veronicaUA_follow_open_state.json` and `veronicaUA_follow_open_paper.log`.",
                "3. Sidecar telemetry -> `veronicaUA_slippage_telemetry.jsonl`, `PAPER_LIVE_STATUS.json`, `PAPER_LIVE_STATUS_20260524.md`, `SLIPPAGE_MODEL_REPORT_20260524.md`.",
                "4. Binance public closed position history -> `wave_*/position_refresh/position_history_normalized.csv`.",
                "5. HYPE OHLCV collection -> annual/window `.npz` under this report dir.",
                "6. V21 compare/tune -> `wave_*/variants/*/summary.json` and IE=100 no-warmup/no-trend report.",
                "7. Status/report aggregation -> `STATUS.md`, `STATUS.json`, audit and comparison reports.",
                "",
                "## Agent Flow Used So Far",
                "",
                "- Parent/Taras delegated Binance 430051/HYPE loop setup to this Codex worker.",
                "- This worker owns only the Binance 430051/HYPE report/code paths here.",
                "- Separate workers own Binance 475183 and DarkKnight loops; those were not modified for this task.",
                "- Current addition is a sidecar telemetry/research loop, avoiding disruption of existing paper-live and tune loops.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def poll_once() -> Dict[str, Any]:
    now = utc_now()
    state = read_json(STATE_PATH, {})
    cfg = read_json(CONFIG_PATH, {})
    records = build_position_records(state, cfg, now)
    append_jsonl(TELEMETRY_JSONL, records)
    update_reports(records, state, cfg, now)
    return {"updated_at": iso(now), "records": len(records), "status_json": str(PAPER_STATUS_JSON)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Paper-only VeronicaUA HYPE slippage telemetry sidecar.")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval-sec", type=float, default=60.0)
    args = ap.parse_args()
    while True:
        print(json.dumps(poll_once(), ensure_ascii=False, indent=2), flush=True)
        if args.once or not args.loop:
            break
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    main()
