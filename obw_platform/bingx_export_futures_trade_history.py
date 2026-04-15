#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import requests

try:
    import certifi  # type: ignore
except Exception:
    certifi = None  # type: ignore

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    try:
        from backports.zoneinfo import ZoneInfo  # type: ignore
    except Exception:
        ZoneInfo = None  # type: ignore

try:
    import pytz  # type: ignore
except Exception:
    pytz = None  # type: ignore

try:
    from dateutil import tz as dateutil_tz  # type: ignore
except Exception:
    dateutil_tz = None  # type: ignore


BASE_URLS = (
    "https://open-api.bingx.com",
    "https://open-api.bingx.pro",
)

UA_HEADERS = [
    "Час виконання",
    "Ф’ючерси / Напрямок",
    "Виконано",
    "Ціна виконання",
    "Закриті PnL / %",
    "Комісія",
    "Ордер №",
    "Операція",
]


@dataclass
class Config:
    api_key: str
    api_secret: str
    symbol: str
    start_ms: int
    end_ms: int
    currency: str = "USDT"
    trading_unit: str = "COIN"
    recv_window: int = 5000
    orders_limit: int = 1000
    fill_chunk_hours: int = 24
    tz_name: str = "Europe/Kyiv"
    timeout_sec: int = 20
    insecure: bool = False
    ca_bundle: Optional[str] = None
    verbose: bool = False
    debug_http: bool = False
    probe_empty: bool = False


class BingXError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export BingX perpetual futures trade history with verbose console diagnostics")
    p.add_argument("--symbol", required=True, help="Trading pair, e.g. ENA-USDT")
    p.add_argument("--start", required=True, help='Start datetime, e.g. "2026-04-12 00:35:00"')
    p.add_argument("--end", required=True, help='End datetime, e.g. "2026-04-14 20:30:00"')
    p.add_argument("--tz", default="Europe/Kyiv", help="Input/output timezone name")
    p.add_argument("--out", default="bingx_trade_history.csv", help="Output browser-like CSV")
    p.add_argument("--raw-json", default=None, help="Optional path to save raw joined JSON")
    p.add_argument("--fills-csv", default=None, help="Optional path to save normalized fills CSV")
    p.add_argument("--orders-csv", default=None, help="Optional path to save normalized orders CSV")
    p.add_argument("--currency", default="USDT", choices=["USDT", "USDC"], help="Settlement currency")
    p.add_argument("--trading-unit", default="COIN", choices=["COIN", "CONT"], help="Trading unit for allFillOrders")
    p.add_argument("--fill-chunk-hours", type=int, default=24, help="Chunk size for fill queries")
    p.add_argument("--orders-limit", type=int, default=1000, help="Limit for allOrders page size")
    p.add_argument("--recv-window", type=int, default=5000, help="Request recvWindow in ms")
    p.add_argument("--timeout-sec", type=int, default=20, help="HTTP timeout in seconds")
    p.add_argument("--api-key", default=os.getenv("BINGX_API_KEY"), help="BingX API key")
    p.add_argument("--api-secret", default=os.getenv("BINGX_API_SECRET"), help="BingX API secret")
    p.add_argument("--env-file", default=None, help="Optional .env path")
    p.add_argument("--ca-bundle", default=None, help="Custom CA bundle path")
    p.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    p.add_argument("--skip-orders", action="store_true", help="Skip /allOrders and rely only on fills")
    p.add_argument("--verbose", action="store_true", help="Verbose logging")
    p.add_argument("--debug-http", action="store_true", help="Print request/response diagnostics for every API call")
    p.add_argument("--probe-empty", action="store_true", help="When a query returns empty, also probe nearby read endpoints")
    return p.parse_args()


def mask(s: str, left: int = 6, right: int = 4) -> str:
    if not s:
        return ""
    if len(s) <= left + right:
        return "*" * len(s)
    return s[:left] + "*" * (len(s) - left - right) + s[-right:]


def dbg(cfg: Config, *parts: Any, force: bool = False) -> None:
    if cfg.verbose or cfg.debug_http or force:
        print("[dbg]", *parts, file=sys.stderr)


def parse_dotenv_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return data

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def find_env_file(explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        return p if p.exists() else None

    candidates: List[Path] = []
    cwd = Path.cwd().resolve()
    for base in [cwd, *cwd.parents]:
        candidates.append(base / ".env")

    script_dir = Path(__file__).resolve().parent
    for base in [script_dir, *script_dir.parents]:
        candidates.append(base / ".env")

    seen = set()
    for p in candidates:
        sp = str(p)
        if sp in seen:
            continue
        seen.add(sp)
        if p.exists():
            return p
    return None


def load_api_credentials(args: argparse.Namespace) -> Tuple[str, str, Optional[Path]]:
    api_key = args.api_key or ""
    api_secret = args.api_secret or ""
    env_path = None

    if api_key and api_secret:
        return api_key, api_secret, env_path

    env_path = find_env_file(args.env_file)
    if env_path is not None:
        env_data = parse_dotenv_file(env_path)
        api_key = api_key or env_data.get("BINGX_API_KEY") or env_data.get("API_KEY") or env_data.get("BINGX_KEY") or ""
        api_secret = api_secret or env_data.get("BINGX_API_SECRET") or env_data.get("API_SECRET") or env_data.get("SECRET_KEY") or env_data.get("BINGX_SECRET") or ""

    return api_key, api_secret, env_path


def ensure_tz(name: str):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    if pytz is not None:
        try:
            return pytz.timezone(name)
        except Exception:
            pass
    if dateutil_tz is not None:
        try:
            tz = dateutil_tz.gettz(name)
            if tz is not None:
                return tz
        except Exception:
            pass
    raise RuntimeError(
        "Timezone support is unavailable. Install one of: "
        "`pip install backports.zoneinfo`, `pip install pytz`, "
        "or `pip install python-dateutil`."
    )


def parse_dt_to_ms(text: str, tz_name: str) -> int:
    tz = ensure_tz(tz_name)
    dt = datetime.fromisoformat(text.replace(" ", "T"))
    if dt.tzinfo is None:
        if pytz is not None and hasattr(tz, "localize"):
            dt = tz.localize(dt)  # type: ignore[attr-defined]
        else:
            dt = dt.replace(tzinfo=tz)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def ms_to_local(ms: int, tz_name: str) -> str:
    tz = ensure_tz(tz_name)
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def sign_query(secret: str, params: Dict[str, Any]) -> str:
    qs = urlencode(sorted((k, v) for k, v in params.items() if v is not None), doseq=False)
    sig = hmac.new(secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
    return qs + "&signature=" + sig


def describe_data_shape(data: Any) -> str:
    if data is None:
        return "None"
    if isinstance(data, list):
        if not data:
            return "list len=0"
        first = data[0]
        if isinstance(first, dict):
            return f"list len={len(data)} first_keys={sorted(first.keys())[:12]}"
        return f"list len={len(data)} first_type={type(first).__name__}"
    if isinstance(data, dict):
        return f"dict keys={sorted(data.keys())[:20]}"
    return type(data).__name__


def build_session(cfg: Config) -> requests.Session:
    s = requests.Session()
    if cfg.insecure:
        s.verify = False
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    elif cfg.ca_bundle:
        s.verify = cfg.ca_bundle
    elif certifi is not None:
        try:
            s.verify = certifi.where()
        except Exception:
            s.verify = True
    else:
        s.verify = True
    return s


def request_signed_payload(
    session: requests.Session,
    method: str,
    path: str,
    params: Dict[str, Any],
    cfg: Config,
) -> Dict[str, Any]:
    payload = dict(params)
    payload["timestamp"] = int(time.time() * 1000)
    payload["recvWindow"] = cfg.recv_window
    signed_qs = sign_query(cfg.api_secret, payload)

    last_err: Optional[Exception] = None
    for base in BASE_URLS:
        url = f"{base}{path}"
        req_start = time.time()
        try:
            if cfg.debug_http:
                dbg(cfg, "http.request", method.upper(), url, "params=", payload)

            if method.upper() == "GET":
                resp = session.get(
                    f"{url}?{signed_qs}",
                    headers={
                        "X-BX-APIKEY": cfg.api_key,
                        "X-SOURCE-KEY": "BX-AI-SKILL",
                    },
                    timeout=cfg.timeout_sec,
                )
            else:
                resp = session.request(
                    method.upper(),
                    url,
                    headers={
                        "X-BX-APIKEY": cfg.api_key,
                        "X-SOURCE-KEY": "BX-AI-SKILL",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data=signed_qs,
                    timeout=cfg.timeout_sec,
                )

            elapsed = round((time.time() - req_start) * 1000, 1)
            text = resp.text
            if cfg.debug_http:
                dbg(cfg, "http.response", method.upper(), url, "status=", resp.status_code, "elapsed_ms=", elapsed, "text_head=", text[:350])

            resp.raise_for_status()
            raw = resp.json()
            code = raw.get("code")
            msg = raw.get("msg")
            data = raw.get("data")

            dbg(cfg, "api.result", path, "code=", code, "msg=", msg, "shape=", describe_data_shape(data))
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, dict):
                    dbg(cfg, "api.first_item", path, json.dumps(first, ensure_ascii=False)[:500])

            if code != 0:
                raise BingXError(f"BingX error {code}: {msg}")

            return raw

        except Exception as exc:
            last_err = exc
            dbg(cfg, "api.error", path, "base=", base, "error=", repr(exc), force=True)
            continue

    raise BingXError(f"All BingX endpoints failed: {last_err}")


def request_signed(session: requests.Session, method: str, path: str, params: Dict[str, Any], cfg: Config) -> Any:
    return request_signed_payload(session, method, path, params, cfg).get("data")


def iter_time_chunks(start_ms: int, end_ms: int, max_span_ms: int) -> Iterable[Tuple[int, int]]:
    cur = start_ms
    while cur < end_ms:
        nxt = min(cur + max_span_ms, end_ms)
        yield cur, nxt
        cur = nxt


def print_chunks(cfg: Config, start_ms: int, end_ms: int, chunk_ms: int, label: str) -> None:
    chunks = list(iter_time_chunks(start_ms, end_ms, chunk_ms))
    dbg(cfg, f"{label}.chunks", len(chunks))
    for i, (a, b) in enumerate(chunks, 1):
        dbg(cfg, f"{label}.chunk[{i}]", a, b, ms_to_local(a, cfg.tz_name), "->", ms_to_local(b, cfg.tz_name))


def probe_empty(session: requests.Session, cfg: Config, start_ms: int, end_ms: int) -> None:
    dbg(cfg, "probe_empty.start", force=True)
    probes = [
        ("GET", "/openApi/swap/v3/user/balance", {}),
        ("GET", "/openApi/swap/v2/user/positions", {}),
        ("GET", "/openApi/swap/v2/trade/allOrders", {
            "currency": cfg.currency,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": min(cfg.orders_limit, 50),
        }),
        ("GET", "/openApi/swap/v2/trade/fillHistory", {
            "symbol": cfg.symbol,
            "currency": cfg.currency,
            "startTs": start_ms,
            "endTs": end_ms,
            "pageIndex": 1,
            "pageSize": 50,
        }),
    ]
    for method, path, params in probes:
        try:
            payload = request_signed_payload(session, method, path, params, cfg)
            data = payload.get("data")
            dbg(cfg, "probe_empty.ok", path, "shape=", describe_data_shape(data), force=True)
        except Exception as e:
            dbg(cfg, "probe_empty.fail", path, repr(e), force=True)


def fetch_all_orders(session: requests.Session, cfg: Config) -> List[Dict[str, Any]]:
    all_rows: List[Dict[str, Any]] = []
    seen_order_ids: set[str] = set()
    any_nonempty = False

    seven_days_ms = 7 * 24 * 60 * 60 * 1000
    print_chunks(cfg, cfg.start_ms, cfg.end_ms, seven_days_ms, "orders")

    for chunk_i, (chunk_start, chunk_end) in enumerate(iter_time_chunks(cfg.start_ms, cfg.end_ms, seven_days_ms), 1):
        cursor: Optional[int] = None
        dbg(cfg, f"orders.scan.begin[{chunk_i}]", "local=", ms_to_local(chunk_start, cfg.tz_name), "->", ms_to_local(chunk_end, cfg.tz_name))
        while True:
            params: Dict[str, Any] = {
                "symbol": cfg.symbol,
                "currency": cfg.currency,
                "startTime": chunk_start,
                "endTime": chunk_end,
                "limit": cfg.orders_limit,
            }
            if cursor is not None:
                params["orderId"] = cursor

            data = request_signed(session, "GET", "/openApi/swap/v2/trade/allOrders", params, cfg)
            rows = data if isinstance(data, list) else []
            if not rows:
                dbg(cfg, f"orders.empty[{chunk_i}]", "cursor=", cursor, "symbol=", cfg.symbol)
                break

            any_nonempty = True
            new_count = 0
            max_order_id: Optional[int] = cursor
            for row in rows:
                oid = str(row.get("orderId"))
                if oid not in seen_order_ids:
                    seen_order_ids.add(oid)
                    all_rows.append(row)
                    new_count += 1
                try:
                    oid_int = int(oid)
                    if max_order_id is None or oid_int > max_order_id:
                        max_order_id = oid_int
                except Exception:
                    pass

            dbg(cfg, f"orders.page[{chunk_i}]", "got=", len(rows), "new=", new_count, "cursor_in=", cursor, "cursor_out=", max_order_id)

            if len(rows) < cfg.orders_limit or new_count == 0 or max_order_id is None or max_order_id == cursor:
                break
            cursor = max_order_id

    if cfg.probe_empty and not any_nonempty:
        probe_empty(session, cfg, cfg.start_ms, cfg.end_ms)

    return all_rows


def fetch_all_fills(session: requests.Session, cfg: Config) -> List[Dict[str, Any]]:
    all_rows: List[Dict[str, Any]] = []
    seen_trade_ids: set[str] = set()
    any_nonempty = False

    chunk_ms = max(1, cfg.fill_chunk_hours) * 60 * 60 * 1000
    print_chunks(cfg, cfg.start_ms, cfg.end_ms, chunk_ms, "fills")

    for chunk_i, (chunk_start, chunk_end) in enumerate(iter_time_chunks(cfg.start_ms, cfg.end_ms, chunk_ms), 1):
        params = {
            "tradingUnit": cfg.trading_unit,
            "startTs": chunk_start,
            "endTs": chunk_end,
            "currency": cfg.currency,
        }
        data = request_signed(session, "GET", "/openApi/swap/v2/trade/allFillOrders", params, cfg)
        rows = data if isinstance(data, list) else []
        if rows:
            any_nonempty = True

        added = 0
        symbols_seen = set()
        for row in rows:
            sym = str(row.get("symbol"))
            symbols_seen.add(sym)
            if sym != cfg.symbol:
                continue
            tid = str(row.get("tradeId"))
            if tid in seen_trade_ids:
                continue
            seen_trade_ids.add(tid)
            all_rows.append(row)
            added += 1

        dbg(cfg, f"fills.chunk[{chunk_i}]", "raw=", len(rows), "kept=", added, "symbols_seen=", sorted(list(symbols_seen))[:10])

    if cfg.probe_empty and not any_nonempty:
        probe_empty(session, cfg, cfg.start_ms, cfg.end_ms)

    return all_rows


def normalize_symbol_for_ui(symbol: str) -> str:
    return symbol.replace("-", "")


def format_ts(ms: int, tz_name: str) -> str:
    tz = ensure_tz(tz_name)
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fmt_qty(value: Any, symbol: str) -> str:
    base = symbol.split("-")[0]
    try:
        x = float(value)
        return f"{x:.8f}".rstrip("0").rstrip(".") + f" {base}"
    except Exception:
        return f"{value} {base}"


def fmt_price(value: Any) -> str:
    try:
        x = float(value)
        return f"{x:.8f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value)


def fmt_usdt(value: Any) -> str:
    try:
        x = float(value)
        return f"{x:+.4f} USDT" if x != 0 else "0.0000 USDT"
    except Exception:
        return f"{value} USDT"


def build_direction_label(side: str, position_side: str) -> str:
    side = (side or "").upper()
    position_side = (position_side or "").upper()

    if position_side == "SHORT":
        if side == "SELL":
            return "Відкрити коротку"
        if side == "BUY":
            return "Закрити кор."
    if position_side == "LONG":
        if side == "BUY":
            return "Відкрити лонг"
        if side == "SELL":
            return "Закрити лонг"
    return f"{side} {position_side}".strip() or "Невідомо"


def join_fills_with_orders(
    fills: List[Dict[str, Any]],
    orders: List[Dict[str, Any]],
    cfg: Config,
) -> List[Dict[str, Any]]:
    by_order_id: Dict[str, Dict[str, Any]] = {}
    for row in orders:
        by_order_id[str(row.get("orderId"))] = row

    joined: List[Dict[str, Any]] = []
    missing_order_links = 0
    for fill in fills:
        order = by_order_id.get(str(fill.get("orderId")), {})
        if not order:
            missing_order_links += 1
        joined.append(
            {
                "tradeId": str(fill.get("tradeId")),
                "orderId": str(fill.get("orderId")),
                "symbol": str(fill.get("symbol")),
                "time": int(fill.get("time")),
                "price": fill.get("price"),
                "qty": fill.get("qty"),
                "realizedPnl": fill.get("realizedPnl", "0"),
                "fee": fill.get("fee", "0"),
                "side": str(order.get("side") or fill.get("side") or ""),
                "positionSide": str(order.get("positionSide") or ""),
                "orderStatus": str(order.get("status") or ""),
                "avgPrice": order.get("avgPrice"),
                "executedQty": order.get("executedQty"),
            }
        )

    joined.sort(key=lambda x: (x["time"], x["tradeId"]))
    dbg(cfg, "join.summary", "fills=", len(fills), "orders=", len(orders), "joined=", len(joined), "missing_order_links=", missing_order_links)
    return joined


def export_browser_like_csv(rows: List[Dict[str, Any]], out_path: Path, tz_name: str) -> None:
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=UA_HEADERS)
        writer.writeheader()
        for row in rows:
            symbol = str(row["symbol"])
            side = str(row["side"])
            position_side = str(row["positionSide"])
            direction = build_direction_label(side, position_side)
            writer.writerow(
                {
                    "Час виконання": format_ts(int(row["time"]), tz_name),
                    "Ф’ючерси / Напрямок": f"{normalize_symbol_for_ui(symbol)}\n{direction}",
                    "Виконано": fmt_qty(row["qty"], symbol),
                    "Ціна виконання": fmt_price(row["price"]),
                    "Закриті PnL / %": fmt_usdt(row["realizedPnl"]),
                    "Комісія": fmt_usdt(row["fee"]),
                    "Ордер №": str(row["orderId"]),
                    "Операція": "",
                }
            )


def export_normalized_fills_csv(rows: List[Dict[str, Any]], out_path: Path, tz_name: str) -> None:
    headers = [
        "tradeId", "orderId", "symbol", "time", "time_local", "side", "positionSide",
        "price", "qty", "realizedPnl", "fee", "orderStatus",
    ]
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            out = dict(row)
            out["time_local"] = format_ts(int(row["time"]), tz_name)
            writer.writerow({k: out.get(k, "") for k in headers})


def export_orders_csv(rows: List[Dict[str, Any]], out_path: Path, tz_name: str) -> None:
    headers = [
        "orderId", "symbol", "side", "positionSide", "type", "status",
        "price", "avgPrice", "origQty", "executedQty", "time", "updateTime",
        "time_local", "update_time_local",
    ]
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            out = {
                "orderId": row.get("orderId"),
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "positionSide": row.get("positionSide"),
                "type": row.get("type"),
                "status": row.get("status"),
                "price": row.get("price"),
                "avgPrice": row.get("avgPrice"),
                "origQty": row.get("origQty"),
                "executedQty": row.get("executedQty"),
                "time": row.get("time"),
                "updateTime": row.get("updateTime"),
                "time_local": format_ts(int(row["time"]), tz_name) if row.get("time") else "",
                "update_time_local": format_ts(int(row["updateTime"]), tz_name) if row.get("updateTime") else "",
            }
            writer.writerow(out)


def main() -> int:
    args = parse_args()
    args.api_key, args.api_secret, env_path = load_api_credentials(args)

    if not args.api_key or not args.api_secret:
        print(
            "Set --api-key/--api-secret, export BINGX_API_KEY/BINGX_API_SECRET, "
            "or place them in a .env file",
            file=sys.stderr,
        )
        return 2

    cfg = Config(
        api_key=args.api_key,
        api_secret=args.api_secret,
        symbol=args.symbol.upper(),
        start_ms=parse_dt_to_ms(args.start, args.tz),
        end_ms=parse_dt_to_ms(args.end, args.tz),
        currency=args.currency,
        trading_unit=args.trading_unit,
        recv_window=args.recv_window,
        orders_limit=args.orders_limit,
        fill_chunk_hours=args.fill_chunk_hours,
        tz_name=args.tz,
        timeout_sec=args.timeout_sec,
        insecure=bool(args.insecure),
        ca_bundle=args.ca_bundle,
        verbose=bool(args.verbose),
        debug_http=bool(args.debug_http),
        probe_empty=bool(args.probe_empty),
    )

    if cfg.end_ms <= cfg.start_ms:
        print("--end must be later than --start", file=sys.stderr)
        return 2

    session = build_session(cfg)

    dbg(cfg, "startup.env_file", env_path, force=True)
    dbg(cfg, "startup.api_key", mask(cfg.api_key), "secret=", mask(cfg.api_secret), force=True)
    dbg(cfg, "startup.session_verify", getattr(session, "verify", None), "insecure=", cfg.insecure, force=True)
    dbg(cfg, "startup.symbol", cfg.symbol, "currency=", cfg.currency, "trading_unit=", cfg.trading_unit, force=True)
    dbg(cfg, "startup.time.local", args.start, "->", args.end, force=True)
    dbg(cfg, "startup.time.ms", cfg.start_ms, cfg.end_ms, force=True)
    dbg(cfg, "startup.time.utc", ms_to_utc(cfg.start_ms), "->", ms_to_utc(cfg.end_ms), force=True)
    dbg(cfg, "startup.time.local_resolved", ms_to_local(cfg.start_ms, cfg.tz_name), "->", ms_to_local(cfg.end_ms, cfg.tz_name), force=True)

    out_path = Path(args.out)
    raw_json_path = Path(args.raw_json) if args.raw_json else None
    fills_csv_path = Path(args.fills_csv) if args.fills_csv else None
    orders_csv_path = Path(args.orders_csv) if args.orders_csv else None

    orders: List[Dict[str, Any]] = []
    if not args.skip_orders:
        orders = fetch_all_orders(session, cfg)

    fills = fetch_all_fills(session, cfg)
    joined = join_fills_with_orders(fills, orders, cfg)

    export_browser_like_csv(joined, out_path, cfg.tz_name)
    if raw_json_path:
        raw_json_path.write_text(json.dumps(joined, ensure_ascii=False, indent=2), encoding="utf-8")
    if fills_csv_path:
        export_normalized_fills_csv(joined, fills_csv_path, cfg.tz_name)
    if orders_csv_path:
        export_orders_csv(orders, orders_csv_path, cfg.tz_name)

    print(f"orders={len(orders)}")
    print(f"fills={len(fills)}")
    print(f"joined={len(joined)}")
    print(f"out={out_path}")
    if raw_json_path:
        print(f"raw_json={raw_json_path}")
    if fills_csv_path:
        print(f"fills_csv={fills_csv_path}")
    if orders_csv_path:
        print(f"orders_csv={orders_csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
