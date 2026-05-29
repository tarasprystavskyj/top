#!/usr/bin/env python3
"""Telegram guard monitor for a HYPE live session.

The monitor is intentionally deterministic and bounded: each check reads only
current status, recent orders, and a small persisted dedup state. It never
places orders and it never prints secrets.
"""
import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


DEFAULT_LIVE_DIR = Path("/var/www/vps2.happyuser.info/top/top_1/obw_platform/_reports/_live/hype_canary_bingx_live_20260525")
DEFAULT_ENV_FILE = Path("/var/www/vps2.happyuser.info/top/top_1/obw_platform/.env")
DEFAULT_STATE_PATH = DEFAULT_LIVE_DIR / "_monitor" / "telegram_monitor_state.json"
DEFAULT_INTERVAL_SEC = 3600.0
TELEGRAM_TOKEN_KEYS = ("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN", "TG_TOKEN")
TELEGRAM_CHAT_KEYS = ("TELEGRAM_CHAT", "TELEGRAM_CHAT_ID", "TG_CHAT_ID", "TG_CHAT")
ORDER_COLUMNS = ("order_id", "ts_utc", "symbol", "side", "type", "price", "qty", "status", "reason", "mode", "extra")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_env_file(path: Path) -> Dict[str, bool]:
    loaded: Dict[str, bool] = {}
    if not path or not path.exists():
        return loaded
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        try:
            parsed = shlex.split(value.strip(), comments=False, posix=True)[0] if value.strip() else ""
        except Exception:
            parsed = value.strip().strip("'\"")
        os.environ.setdefault(key, parsed)
        loaded[key] = bool(os.environ.get(key))
    return loaded


def first_env(keys: Iterable[str]) -> str:
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value
    return ""


def read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def load_monitor_state(path: Path) -> Dict[str, Any]:
    state = read_json(path, {})
    state.setdefault("seen_order_ids", [])
    state.setdefault("alerted_issue_keys", [])
    state.setdefault("last_daily_reexec_date_kyiv", "")
    state.setdefault("created_utc", iso(utc_now()))
    return state


def save_monitor_state(path: Path, state: Dict[str, Any]) -> None:
    state["seen_order_ids"] = list(dict.fromkeys(state.get("seen_order_ids", [])))[-5000:]
    state["alerted_issue_keys"] = list(dict.fromkeys(state.get("alerted_issue_keys", [])))[-5000:]
    state["updated_utc"] = iso(utc_now())
    write_json(path, state)


def status_path(live_dir: Path) -> Path:
    return live_dir / "RUN_STATUS.json"


def session_db_path(live_dir: Path, status: Dict[str, Any]) -> Path:
    raw = status.get("session_db") or live_dir / "session.sqlite"
    path = Path(raw)
    if not path.is_absolute():
        path = live_dir / path
    return path


def active_log_path(live_dir: Path) -> Optional[Path]:
    marker = live_dir / "ACTIVE_LOG_PATH.txt"
    try:
        if marker.exists():
            p = Path(marker.read_text(encoding="utf-8").strip())
            return p if p.exists() else None
    except Exception:
        return None
    return None


def sqlite_columns(con: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [str(row[1]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def recent_orders(db_path: Path, limit: int = 200) -> List[Dict[str, Any]]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        cols = sqlite_columns(con, "orders")
        if not cols:
            return []
        selected = [col for col in ORDER_COLUMNS if col in cols]
        if "order_id" not in selected:
            return []
        order_col = "ts_utc" if "ts_utc" in cols else "rowid"
        rows = con.execute(
            f"SELECT {', '.join(selected)} FROM orders ORDER BY {order_col} DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out = []
        for row in rows:
            item = {col: row[col] if col in row.keys() else None for col in ORDER_COLUMNS}
            out.append(item)
        return out
    finally:
        con.close()


def open_positions(db_path: Path) -> List[Dict[str, Any]]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        cols = sqlite_columns(con, "open_positions")
        if not cols:
            return []
        selected = [c for c in ("bot_id", "symbol", "side", "qty", "entry", "status", "exchange", "entry_fill", "exit_fill") if c in cols]
        if not selected:
            return []
        rows = con.execute(f"SELECT {', '.join(selected)} FROM open_positions").fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def has_live_process(live_dir: Path, process_pattern: str = "hype_cap100_.*live.*canary\\.py") -> bool:
    try:
        proc = subprocess.run(
            ["pgrep", "-af", process_pattern],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    text = proc.stdout or ""
    return str(live_dir) in text


def issue_key(kind: str, value: Any) -> str:
    return f"{kind}:{str(value)[:240]}"


def status_age_sec(status: Dict[str, Any], now: datetime) -> Optional[float]:
    raw = status.get("utc")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None
    return max(0.0, (now - dt).total_seconds())


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def monitor_title(kind: str, text: str) -> str:
    icons = {
        "ok": "🟢",
        "warn": "🟠",
        "bad": "🔴",
        "control": "🛑",
        "wait": "⏳",
        "info": "🔵",
    }
    return f"{icons.get(kind, '🔵')} HYPE {text}"


def summarize_order(order: Dict[str, Any]) -> str:
    reason = str(order.get("reason") or "")
    if len(reason) > 260:
        reason = reason[:260] + "..."
    status = str(order.get("status") or "")
    status_icon = {
        "FILLED": "🟢",
        "REJECTED": "🔴",
        "CANCELED": "🟠",
        "CANCELLED": "🟠",
        "EXPIRED": "🟠",
    }.get(status.upper(), "🔵")
    return (
        f"{status_icon} {status} {order.get('type')} {order.get('symbol')} {order.get('side')}\n"
        f"📅 {order.get('ts_utc')}\n"
        f"📦 qty={order.get('qty')}  💰 price={order.get('price')}\n"
        f"🧾 id={order.get('order_id')}\n"
        f"💬 {reason}"
    )


def order_attempt_kind(order: Dict[str, Any]) -> str:
    text = " ".join(
        str(order.get(k) or "").lower()
        for k in ("mode", "type", "side", "reason", "extra")
    )
    if "close" in text or "exit" in text or "reduce" in text:
        return "close"
    if "open" in text or "entry" in text or "buy" in text:
        return "open"
    return "unknown"


def active_position_notional_sum(positions: List[Dict[str, Any]], *, expected_exchange: str = "", live_dir: str = "") -> float:
    total = 0.0
    for pos in positions:
        status = str(pos.get("status") or "").upper()
        if status and status not in {"OPEN", "ACTIVE", "FILLED"}:
            continue
        if expected_exchange and str(pos.get("exchange") or "").lower() != str(expected_exchange).lower():
            continue
        if live_dir:
            bot_id = str(pos.get("bot_id") or "")
            if bot_id and str(live_dir) not in bot_id:
                continue
        qty = parse_float(pos.get("qty"))
        entry = parse_float(pos.get("entry_fill"), parse_float(pos.get("entry")))
        exit_fill = parse_float(pos.get("exit_fill"))
        if exit_fill > 0:
            continue
        total += abs(qty * entry)
    return total


def collect_alerts(args: argparse.Namespace, state: Dict[str, Any], now: datetime) -> Tuple[List[str], Dict[str, Any]]:
    live_dir = Path(args.live_dir)
    status = read_json(status_path(live_dir), {})
    alerts: List[str] = []
    meta: Dict[str, Any] = {"status": status}
    alerted_issue_keys = set(state.get("alerted_issue_keys", []))

    if not status:
        key = issue_key("missing_status", status_path(live_dir))
        if key not in alerted_issue_keys:
            alerts.append(f"{monitor_title('bad', 'RUN_STATUS missing')}\n📄 {status_path(live_dir)}")
            alerted_issue_keys.add(key)
    else:
        age = status_age_sec(status, now)
        meta["status_age_sec"] = age
        if age is None or age > float(args.max_status_age_sec):
            key = issue_key("stale_status", f"{status.get('utc')}:{int(age or -1)//60}")
            if key not in alerted_issue_keys:
                alerts.append(f"{monitor_title('warn', 'stale RUN_STATUS')}\n📅 utc={status.get('utc')}\n⏱ age_sec={age}")
                alerted_issue_keys.add(key)

        if args.expected_live_exchange and status.get("live_exchange") != args.expected_live_exchange:
            key = issue_key("exchange_mismatch", status.get("live_exchange"))
            if key not in alerted_issue_keys:
                alerts.append(f"{monitor_title('warn', 'exchange mismatch')}\n📍 live={status.get('live_exchange')}\n🎯 expected={args.expected_live_exchange}")
                alerted_issue_keys.add(key)

        control = status.get("control") if isinstance(status.get("control"), dict) else {}
        for flag in ("stop_new_orders", "hot_stop", "kill"):
            if control.get(flag):
                key = issue_key(f"control_{flag}", control.get(f"{flag}_path") or flag)
                if key not in alerted_issue_keys:
                    alerts.append(f"{monitor_title('control', 'control flag active')}\n🚦 {flag}\n📄 {control.get(f'{flag}_path')}")
                    alerted_issue_keys.add(key)

        backoff = status.get("order_error_backoff") if isinstance(status.get("order_error_backoff"), dict) else {}
        if backoff.get("last_error") or parse_float(backoff.get("consecutive")) > 0 or parse_float(backoff.get("until_ts")) > now.timestamp():
            key = issue_key("order_backoff", backoff.get("last_error") or backoff.get("until_utc") or backoff.get("consecutive"))
            if key not in alerted_issue_keys:
                alerts.append(f"{monitor_title('wait', 'order backoff active')}\n{json.dumps(backoff, ensure_ascii=False)[:900]}")
                alerted_issue_keys.add(key)

        failures = status.get("entry_failures") if isinstance(status.get("entry_failures"), dict) else {}
        for failure_key, failure in failures.items():
            key = issue_key("entry_failure", f"{failure_key}:{failure.get('last_error') if isinstance(failure, dict) else failure}")
            if key not in alerted_issue_keys:
                alerts.append(f"{monitor_title('bad', 'entry failure')}\n🔑 {failure_key}\n{json.dumps(failure, ensure_ascii=False)[:900]}")
                alerted_issue_keys.add(key)

        guards = status.get("guards") if isinstance(status.get("guards"), dict) else {}
        open_trades = status.get("open_paper_trades") if isinstance(status.get("open_paper_trades"), list) else []
        gross = parse_float(guards.get("gross_open_notional"))
        trade_sum = sum(parse_float(t.get("notional")) for t in open_trades if isinstance(t, dict))
        if open_trades and abs(gross - trade_sum) > float(args.notional_mismatch_tolerance):
            key = issue_key("notional_mismatch", f"{gross:.6f}:{trade_sum:.6f}")
            if key not in alerted_issue_keys:
                alerts.append(f"{monitor_title('warn', 'notional mismatch')}\n🛡 guards.gross={gross}\n📊 open_trades_sum={trade_sum}")
                alerted_issue_keys.add(key)

    if not has_live_process(live_dir, process_pattern=str(args.process_pattern)):
        key = issue_key("process_missing", live_dir)
        if key not in alerted_issue_keys:
            alerts.append(f"{monitor_title('bad', 'live process missing')}\n📂 {live_dir}")
            alerted_issue_keys.add(key)

    db_path = session_db_path(live_dir, status if isinstance(status, dict) else {})
    orders = recent_orders(db_path, limit=int(args.order_scan_limit))
    meta["orders_scanned"] = len(orders)
    positions = open_positions(db_path)
    meta["positions_scanned"] = len(positions)
    if status:
        guards = status.get("guards") if isinstance(status.get("guards"), dict) else {}
        gross = parse_float(guards.get("gross_open_notional"))
        db_notional = active_position_notional_sum(
            positions,
            expected_exchange=str(status.get("live_exchange") or args.expected_live_exchange or ""),
            live_dir=str(live_dir),
        )
        if db_notional > 0 and abs(gross - db_notional) > float(args.notional_mismatch_tolerance):
            key = issue_key("db_notional_mismatch", f"{gross:.6f}:{db_notional:.6f}")
            if key not in alerted_issue_keys:
                alerts.append(f"{monitor_title('warn', 'position/notional mismatch')}\n🛡 RUN_STATUS.gross={gross}\n🗄 session_db_open_positions={db_notional}")
                alerted_issue_keys.add(key)
    seen_order_ids = set(state.get("seen_order_ids", []))
    new_orders = [o for o in orders if o.get("order_id") and str(o.get("order_id")) not in seen_order_ids]
    if args.init_baseline and not state.get("baseline_initialized"):
        seen_order_ids.update(str(o.get("order_id")) for o in orders if o.get("order_id"))
        state["baseline_initialized"] = True
        state["baseline_initialized_utc"] = iso(now)
        state["seen_order_ids"] = list(seen_order_ids)
        state["alerted_issue_keys"] = list(alerted_issue_keys)
        return alerts, meta

    for order in sorted(new_orders, key=lambda row: str(row.get("ts_utc") or "")):
        seen_order_ids.add(str(order.get("order_id")))
        status_text = str(order.get("status") or "").upper()
        if status_text == "FILLED":
            alerts.append(monitor_title("ok", "order filled") + "\n" + summarize_order(order))
        elif status_text in {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED"}:
            alerts.append(monitor_title("bad", "order suspicious") + "\n" + summarize_order(order))

    # Cluster alerts are about newly observed suspicious orders. Historical
    # rejected rows are kept for forensics but should not page the operator on
    # every scan after a known incident.
    rejected_recent = [o for o in new_orders if str(o.get("status") or "").upper() == "REJECTED"]
    if len(rejected_recent) >= int(args.rejected_recent_threshold):
        latest = rejected_recent[0]
        key = issue_key("rejected_cluster", f"{len(rejected_recent)}:{latest.get('ts_utc')}:{latest.get('reason')}")
        if key not in alerted_issue_keys:
            alerts.append(f"{monitor_title('bad', 'rejected order cluster')}\n🔢 count={len(rejected_recent)}\n{summarize_order(latest)}")
            alerted_issue_keys.add(key)

    suspicious_statuses = {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED"}
    suspicious_recent = [o for o in new_orders if str(o.get("status") or "").upper() in suspicious_statuses]
    for kind in ("open", "close"):
        attempts = [o for o in suspicious_recent if order_attempt_kind(o) == kind]
        if len(attempts) >= int(args.repeated_attempt_threshold):
            latest = attempts[0]
            key = issue_key(f"{kind}_attempt_cluster", f"{len(attempts)}:{latest.get('ts_utc')}:{latest.get('reason')}")
            if key not in alerted_issue_keys:
                alerts.append(f"{monitor_title('bad', f'repeated {kind} attempts')}\n🔢 count={len(attempts)}\n{summarize_order(latest)}")
                alerted_issue_keys.add(key)

    state["seen_order_ids"] = list(seen_order_ids)
    state["alerted_issue_keys"] = list(alerted_issue_keys)
    return alerts, meta


def telegram_credentials(env_file: Path) -> Tuple[str, str]:
    load_env_file(env_file)
    return first_env(TELEGRAM_TOKEN_KEYS), first_env(TELEGRAM_CHAT_KEYS)


def send_telegram(token: str, chat_id: str, text: str, timeout_sec: float = 10.0) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}, timeout=timeout_sec)
    try:
        payload = resp.json()
    except Exception:
        payload = {"text": resp.text[:500]}
    if resp.status_code >= 400 or not payload.get("ok", False):
        raise RuntimeError(f"telegram send failed status={resp.status_code} payload={payload}")
    return payload


def run_check(args: argparse.Namespace) -> Dict[str, Any]:
    now = utc_now()
    state_path = Path(args.state_path)
    state = load_monitor_state(state_path)
    original_alerted_issue_keys = list(state.get("alerted_issue_keys", []))
    alerts, meta = collect_alerts(args, state, now)
    token, chat_id = telegram_credentials(Path(args.env_file))
    sent = 0
    if args.send_startup and not state.get("startup_sent"):
        alerts.insert(0, f"{monitor_title('info', 'monitor started')}\n📅 utc={iso(now)}\n📂 {args.live_dir}\n⏱ interval_sec={args.interval_sec}")
        state["startup_sent"] = True
    if args.dry_run:
        for alert in alerts:
            print(alert)
    else:
        if alerts and (not token or not chat_id):
            raise SystemExit("Telegram token/chat id missing; set TELEGRAM_TOKEN and TELEGRAM_CHAT or aliases")
        for alert in alerts:
            send_telegram(token, chat_id, alert)
            sent += 1
            time.sleep(float(args.telegram_send_gap_sec))
    state["last_check_utc"] = iso(now)
    state["last_alert_count"] = len(alerts)
    state["last_sent_count"] = sent
    if args.dry_run:
        if args.init_baseline and state.get("baseline_initialized"):
            state["alerted_issue_keys"] = original_alerted_issue_keys
            save_monitor_state(state_path, state)
    else:
        save_monitor_state(state_path, state)
    return {"alerts": alerts, "sent": sent, "meta": meta, "state_path": str(state_path)}


def kyiv_date_hour(now: datetime) -> Tuple[str, int]:
    # Current deployment need is Kyiv 02:00 daily reset; UTC+3 is correct for
    # the active May live period. Avoid adding a runtime dependency for this.
    kyiv = now.astimezone(timezone(timedelta(hours=3)))
    return kyiv.date().isoformat(), kyiv.hour


def maybe_daily_reexec(args: argparse.Namespace, state: Dict[str, Any]) -> None:
    date_text, hour = kyiv_date_hour(utc_now())
    if hour != int(args.daily_reexec_hour_kyiv):
        return
    if state.get("last_daily_reexec_date_kyiv") == date_text:
        return
    state["last_daily_reexec_date_kyiv"] = date_text
    save_monitor_state(Path(args.state_path), state)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Telegram monitor for HYPE live session")
    ap.add_argument("--live-dir", default=str(DEFAULT_LIVE_DIR))
    ap.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    ap.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    ap.add_argument("--interval-sec", type=float, default=DEFAULT_INTERVAL_SEC)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--init-baseline", action="store_true", help="On first run, mark existing orders as seen without order alerts.")
    ap.add_argument("--send-startup", action="store_true")
    ap.add_argument("--expected-live-exchange", default="gateio")
    ap.add_argument("--process-pattern", default="hype_cap100_.*live.*canary\\.py")
    ap.add_argument("--max-status-age-sec", type=float, default=600.0)
    ap.add_argument("--order-scan-limit", type=int, default=250)
    ap.add_argument("--rejected-recent-threshold", type=int, default=5)
    ap.add_argument("--repeated-attempt-threshold", type=int, default=4)
    ap.add_argument("--notional-mismatch-tolerance", type=float, default=0.25)
    ap.add_argument("--telegram-send-gap-sec", type=float, default=0.5)
    ap.add_argument("--daily-reexec-hour-kyiv", type=int, default=2)
    return ap


def main() -> None:
    args = build_arg_parser().parse_args()
    while True:
        result = run_check(args)
        print(json.dumps({"utc": iso(utc_now()), "sent": result["sent"], "alerts": len(result["alerts"]), "state_path": result["state_path"]}, ensure_ascii=False, sort_keys=True), flush=True)
        if not args.loop:
            break
        state = load_monitor_state(Path(args.state_path))
        maybe_daily_reexec(args, state)
        time.sleep(max(1.0, float(args.interval_sec)))


if __name__ == "__main__":
    main()
