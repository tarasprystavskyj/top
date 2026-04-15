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
    recv_window: int = 5000
    orders_limit: int = 1000
    fill_page_size: int = 1000
    fill_chunk_hours: int = 24
    tz_name: str = "Europe/Kyiv"
    timeout_sec: int = 20
    insecure: bool = False
    ca_bundle: Optional[str] = None
    verbose: bool = False
    debug_http: bool = False


class BingXError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export BingX futures trade history with debug")
    p.add_argument("--symbol", required=True, help="Trading pair, e.g. ENA-USDT")
    p.add_argument("--start", required=True, help='Start datetime, e.g. "2026-04-12 00:35:00"')
    p.add_argument("--end", required=True, help='End datetime, e.g. "2026-04-14 20:30:00"')
    p.add_argument("--tz", default="Europe/Kyiv", help="Input/output timezone name")
    p.add_argument("--out-dir", default="_reports/bingx_exports", help="Directory for exported files")
    p.add_argument("--out", default=None, help="Browser-like CSV filename or full path")
    p.add_argument("--raw-json", default=None, help="Joined raw JSON filename or full path")
    p.add_argument("--fills-csv", default=None, help="Normalized fills CSV filename or full path")
    p.add_argument("--orders-csv", default=None, help="Normalized orders CSV filename or full path")
    p.add_argument("--currency", default="USDT", choices=["USDT", "USDC"], help="Settlement currency")
    p.add_argument("--fill-chunk-hours", type=int, default=24, help="Chunk size for fill queries")
    p.add_argument("--orders-limit", type=int, default=1000, help="Limit for allOrders page size")
    p.add_argument("--fill-page-size", type=int, default=1000, help="Page size for fillHistory")
    p.add_argument("--recv-window", type=int, default=5000, help="Request recvWindow in ms")
    p.add_argument("--timeout-sec", type=int, default=20, help="HTTP timeout in seconds")
    p.add_argument("--api-key", default=os.getenv("BINGX_API_KEY"), help="BingX API key")
    p.add_argument("--api-secret", default=os.getenv("BINGX_API_SECRET"), help="BingX API secret")
    p.add_argument("--env-file", default=None, help="Optional .env path")
    p.add_argument("--ca-bundle", default=None, help="Custom CA bundle path")
    p.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    p.add_argument("--verbose", action="store_true", help="Verbose logging")
    p.add_argument("--debug-http", action="store_true", help="Print request/response diagnostics")
    return p.parse_args()


def dbg(cfg: Config, *parts: Any, force: bool = False) -> None:
    if cfg.verbose or cfg.debug_http or force:
        print("[dbg]", *parts, file=sys.stderr)


def mask(s: str, left: int = 6, right: int = 4) -> str:
    if not s:
        return ""
    if len(s) <= left + right:
        return "*" * len(s)
    return s[:left] + "*" * (len(s) - left - right) + s[-right:]


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


def sign_query(secret: str, params: Dict[str, Any]) -> str:
    qs = urlencode(sorted((k, v) for k, v in params.items() if v is not None), doseq=False)
    sig = hmac.new(secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
    return qs + "&signature=" + sig


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


def describe_data_shape(data: Any) -> str:
    if data is None:
        return "None"
    if isinstance(data, list):
        return f"list len={len(data)}"
    if isinstance(data, dict):
        return f"dict keys={sorted(data.keys())[:20]}"
    return type(data).__name__


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
            if cfg.debug_http:
                dbg(cfg, "http.response", method.upper(), url, "status=", resp.status_code, "text_head=", resp.text[:320])

            resp.raise_for_status()
            raw = resp.json()
            dbg(cfg, "api.result", path, "code=", raw.get("code"), "msg=", raw.get("msg"), "shape=", describe_data_shape(raw.get("data")))
            return raw
        except Exception as exc:
            last_err = exc
            dbg(cfg, "api.error", path, "base=", base, "error=", repr(exc), force=True)
    raise BingXError(f"All BingX endpoints failed: {last_err}")


def request_signed(session: requests.Session, method: str, path: str, params: Dict[str, Any], cfg: Config) -> Any:
    raw = request_signed_payload(session, method, path, params, cfg)
    if raw.get("code") != 0:
        raise BingXError(f'BingX error {raw.get("code")}: {raw.get("msg")}')
    return raw.get("data")


def iter_time_chunks(start_ms: int, end_ms: int, max_span_ms: int) -> Iterable[Tuple[int, int]]:
    cur = start_ms
    while cur < end_ms:
        nxt = min(cur + max_span_ms, end_ms)
        yield cur, nxt
        cur = nxt


def _safe_name(s: str) -> str:
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out).strip("_") or "export"


def _resolve_output_path(out_dir: Path, value: Optional[str], default_name: str) -> Path:
    if value:
        p = Path(value)
        if p.is_absolute() or p.parent != Path("."):
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / p.name
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / default_name


def fetch_all_orders(session: requests.Session, cfg: Config) -> List[Dict[str, Any]]:
    rows_all: List[Dict[str, Any]] = []
    seen: set[str] = set()

    seven_days_ms = 7 * 24 * 60 * 60 * 1000
    for i, (chunk_start, chunk_end) in enumerate(iter_time_chunks(cfg.start_ms, cfg.end_ms, seven_days_ms), 1):
        cursor: Optional[int] = None
        dbg(cfg, f"orders.chunk[{i}]", ms_to_local(chunk_start, cfg.tz_name), "->", ms_to_local(chunk_end, cfg.tz_name))
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
            rows = []
            if isinstance(data, dict) and isinstance(data.get("orders"), list):
                rows = data["orders"]
            elif isinstance(data, list):
                rows = data

            if not rows:
                dbg(cfg, f"orders.empty[{i}]", "cursor=", cursor)
                break

            new_count = 0
            max_oid = cursor
            for row in rows:
                oid = str(row.get("orderId"))
                if oid not in seen:
                    seen.add(oid)
                    rows_all.append(row)
                    new_count += 1
                try:
                    oi = int(oid)
                    if max_oid is None or oi > max_oid:
                        max_oid = oi
                except Exception:
                    pass

            dbg(cfg, f"orders.page[{i}]", "got=", len(rows), "new=", new_count, "cursor_in=", cursor, "cursor_out=", max_oid)
            if len(rows) < cfg.orders_limit or new_count == 0 or max_oid is None or max_oid == cursor:
                break
            cursor = max_oid

    return rows_all


def fetch_fill_history(session: requests.Session, cfg: Config) -> List[Dict[str, Any]]:
    rows_all: List[Dict[str, Any]] = []
    seen: set[str] = set()
    chunk_ms = max(1, cfg.fill_chunk_hours) * 60 * 60 * 1000

    for i, (chunk_start, chunk_end) in enumerate(iter_time_chunks(cfg.start_ms, cfg.end_ms, chunk_ms), 1):
        page = 1
        dbg(cfg, f"fills.chunk[{i}]", ms_to_local(chunk_start, cfg.tz_name), "->", ms_to_local(chunk_end, cfg.tz_name))
        while True:
            params: Dict[str, Any] = {
                "symbol": cfg.symbol,
                "currency": cfg.currency,
                "startTs": chunk_start,
                "endTs": chunk_end,
                "pageIndex": page,
                "pageSize": cfg.fill_page_size,
            }
            data = request_signed(session, "GET", "/openApi/swap/v2/trade/fillHistory", params, cfg)

            rows = []
            total = None
            if isinstance(data, dict):
                if isinstance(data.get("fill_history_orders"), list):
                    rows = data["fill_history_orders"]
                elif isinstance(data.get("fill_orders"), list):
                    rows = data["fill_orders"]
                total = data.get("total")
            elif isinstance(data, list):
                rows = data

            if not rows:
                dbg(cfg, f"fills.empty[{i}]", "page=", page, "total=", total)
                break

            new_count = 0
            for row in rows:
                tid = str(row.get("tradeId"))
                if tid in seen:
                    continue
                seen.add(tid)
                rows_all.append(row)
                new_count += 1

            dbg(cfg, f"fills.page[{i}]", "page=", page, "got=", len(rows), "new=", new_count, "total=", total)

            if len(rows) < cfg.fill_page_size or new_count == 0:
                break
            page += 1

    return rows_all


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


def build_browser_rows(fills: List[Dict[str, Any]], orders: List[Dict[str, Any]], cfg: Config) -> List[Dict[str, Any]]:
    by_order = {str(o.get("orderId")): o for o in orders}
    out = []

    for f in fills:
        oid = str(f.get("orderId"))
        o = by_order.get(oid, {})
        side = str(f.get("side") or o.get("side") or "").upper()
        position_side = str(f.get("positionSide") or o.get("positionSide") or "").upper()

        if position_side == "SHORT":
            direction = "Відкрити коротку" if side == "SELL" else "Закрити кор."
        elif position_side == "LONG":
            direction = "Відкрити лонг" if side == "BUY" else "Закрити лонг"
        else:
            direction = f"{side} {position_side}".strip() or "Невідомо"

        filled_time = f.get("filledTime")
        t_ms = None
        if filled_time:
            try:
                t_ms = int(datetime.fromisoformat(str(filled_time).replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                t_ms = None
        if t_ms is None:
            try:
                t_ms = int(f.get("time"))
            except Exception:
                t_ms = cfg.start_ms

        out.append({
            "tradeId": str(f.get("tradeId")),
            "orderId": oid,
            "symbol": str(f.get("symbol") or cfg.symbol),
            "time": t_ms,
            "side": side,
            "positionSide": position_side,
            "price": f.get("price"),
            "qty": f.get("qty"),
            "realizedPnl": f.get("realisedPNL") if f.get("realisedPNL") is not None else f.get("realizedPnl", "0"),
            "fee": f.get("commission") if f.get("commission") is not None else f.get("fee", "0"),
            "ua_time": ms_to_local(t_ms, cfg.tz_name),
            "ua_direction": direction,
        })

    out.sort(key=lambda r: (r["time"], r["tradeId"]))
    dbg(cfg, "build_browser_rows", "fills=", len(fills), "orders=", len(orders), "out=", len(out))
    return out


def export_browser_like_csv(rows: List[Dict[str, Any]], out_path: Path) -> None:
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=UA_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "Час виконання": row["ua_time"],
                "Ф’ючерси / Напрямок": f'{row["symbol"].replace("-", "")}\n{row["ua_direction"]}',
                "Виконано": fmt_qty(row["qty"], row["symbol"]),
                "Ціна виконання": fmt_price(row["price"]),
                "Закриті PnL / %": fmt_usdt(row["realizedPnl"]),
                "Комісія": fmt_usdt(row["fee"]),
                "Ордер №": row["orderId"],
                "Операція": "",
            })


def export_dict_rows(rows: List[Dict[str, Any]], out_path: Path) -> None:
    if not rows:
        out_path.write_text("", encoding="utf-8")
        return
    keys: List[str] = []
    seen = set()
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    args = parse_args()
    args.api_key, args.api_secret, env_path = load_api_credentials(args)

    if not args.api_key or not args.api_secret:
        print("Missing API credentials", file=sys.stderr)
        return 2

    cfg = Config(
        api_key=args.api_key,
        api_secret=args.api_secret,
        symbol=args.symbol.upper(),
        start_ms=parse_dt_to_ms(args.start, args.tz),
        end_ms=parse_dt_to_ms(args.end, args.tz),
        currency=args.currency,
        recv_window=args.recv_window,
        orders_limit=args.orders_limit,
        fill_page_size=args.fill_page_size,
        fill_chunk_hours=args.fill_chunk_hours,
        tz_name=args.tz,
        timeout_sec=args.timeout_sec,
        insecure=bool(args.insecure),
        ca_bundle=args.ca_bundle,
        verbose=bool(args.verbose),
        debug_http=bool(args.debug_http),
    )
    session = build_session(cfg)

    dbg(cfg, "startup.env_file", env_path, force=True)
    dbg(cfg, "startup.api_key", mask(cfg.api_key), "secret=", mask(cfg.api_secret), force=True)
    dbg(cfg, "startup.verify", getattr(session, "verify", None), force=True)
    dbg(cfg, "startup.symbol", cfg.symbol, force=True)
    dbg(cfg, "startup.range.local", ms_to_local(cfg.start_ms, cfg.tz_name), "->", ms_to_local(cfg.end_ms, cfg.tz_name), force=True)

    base_name = f"{_safe_name(cfg.symbol)}_{cfg.start_ms}_{cfg.end_ms}"
    out_dir = Path(args.out_dir)
    out_path = _resolve_output_path(out_dir, args.out, f"{base_name}_trade_history.csv")
    raw_json_path = _resolve_output_path(out_dir, args.raw_json, f"{base_name}_joined.json") if args.raw_json is not None else None
    fills_csv_path = _resolve_output_path(out_dir, args.fills_csv, f"{base_name}_fills_normalized.csv") if args.fills_csv is not None else None
    orders_csv_path = _resolve_output_path(out_dir, args.orders_csv, f"{base_name}_orders_normalized.csv") if args.orders_csv is not None else None

    orders = fetch_all_orders(session, cfg)
    fills = fetch_fill_history(session, cfg)
    rows = build_browser_rows(fills, orders, cfg)

    export_browser_like_csv(rows, out_path)
    if fills_csv_path:
        export_dict_rows(fills, fills_csv_path)
    if orders_csv_path:
        export_dict_rows(orders, orders_csv_path)
    if raw_json_path:
        raw_json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"orders={len(orders)}")
    print(f"fills={len(fills)}")
    print(f"joined={len(rows)}")
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
