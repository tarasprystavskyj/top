#!/usr/bin/env python3
"""
Export BingX perpetual futures fill history to a browser-like CSV format.

What it does
------------
- Fetches historical order data from:
    * GET /openApi/swap/v2/trade/allOrders
- Fetches historical fill data from:
    * GET /openApi/swap/v2/trade/allFillOrders
- Joins fills to orders by orderId to recover side / positionSide.
- Exports a browser-like CSV with Ukrainian headers similar to BingX Trade History.

Why two endpoints
-----------------
`allFillOrders` gives the true execution-level records with fee and realized PnL.
`allOrders` provides side / positionSide, which is needed to label records like
"Відкрити коротку" vs "Закрити кор.".

Notes
-----
- The official fill endpoint does not expose the PnL percentage shown by the web UI.
  This script fills the "Закриті PnL / %" column with the USDT amount only.
- Historical order queries with `startTime`/`endTime` must not span more than 7 days,
  so this script automatically chunks the range.
- `allFillOrders` has no documented pagination. To reduce the chance of truncation on
  very active accounts, the script fetches it in smaller chunks (default 24h).

Environment variables
---------------------
    BINGX_API_KEY
    BINGX_API_SECRET

Example
-------
python bingx_export_futures_trade_history.py \
  --symbol ENA-USDT \
  --start "2026-04-12 00:35:00" \
  --end   "2026-04-14 20:30:00" \
  --tz Europe/Kyiv \
  --out bingx_ena_trade_history.csv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


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
    timeout_sec: int = 15


class BingXError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export BingX perpetual futures trade history")
    p.add_argument("--symbol", required=True, help="Trading pair, e.g. ENA-USDT")
    p.add_argument("--start", required=True, help='Start datetime, e.g. "2026-04-12 00:35:00"')
    p.add_argument("--end", required=True, help='End datetime, e.g. "2026-04-14 20:30:00"')
    p.add_argument("--tz", default="Europe/Kyiv", help="Output timezone name for CSV timestamps")
    p.add_argument("--out", default="bingx_trade_history.csv", help="Output browser-like CSV")
    p.add_argument("--raw-json", default=None, help="Optional path to save raw joined JSON")
    p.add_argument("--fills-csv", default=None, help="Optional path to save normalized fills CSV")
    p.add_argument("--currency", default="USDT", choices=["USDT", "USDC"], help="Settlement currency")
    p.add_argument("--trading-unit", default="COIN", choices=["COIN", "CONT"], help="Trading unit for allFillOrders")
    p.add_argument("--fill-chunk-hours", type=int, default=24, help="Chunk size for fill queries")
    p.add_argument("--orders-limit", type=int, default=1000, help="Limit for allOrders page size")
    p.add_argument("--recv-window", type=int, default=5000, help="Request recvWindow in ms")
    p.add_argument("--api-key", default=os.getenv("BINGX_API_KEY"), help="BingX API key")
    p.add_argument("--api-secret", default=os.getenv("BINGX_API_SECRET"), help="BingX API secret")
    return p.parse_args()


def ensure_tz(name: str):
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo is not available in this Python build")
    return ZoneInfo(name)


def parse_dt_to_ms(text: str, tz_name: str) -> int:
    tz = ensure_tz(tz_name)
    dt = datetime.fromisoformat(text.replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def sign_query(secret: str, params: Dict[str, Any]) -> str:
    qs = urlencode(sorted((k, v) for k, v in params.items() if v is not None), doseq=False)
    sig = hmac.new(secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
    return qs + "&signature=" + sig


def request_signed(session: requests.Session, method: str, path: str, params: Dict[str, Any], cfg: Config) -> Any:
    payload = dict(params)
    payload["timestamp"] = int(time.time() * 1000)
    payload["recvWindow"] = cfg.recv_window
    signed_qs = sign_query(cfg.api_secret, payload)

    last_err: Optional[Exception] = None
    for base in BASE_URLS:
        url = f"{base}{path}"
        try:
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

            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise BingXError(f'BingX error {data.get("code")}: {data.get("msg")}')
            return data.get("data")
        except Exception as exc:
            last_err = exc
            continue

    raise BingXError(f"All BingX endpoints failed: {last_err}")


def iter_time_chunks(start_ms: int, end_ms: int, max_span_ms: int) -> Iterable[Tuple[int, int]]:
    cur = start_ms
    while cur < end_ms:
        nxt = min(cur + max_span_ms, end_ms)
        yield cur, nxt
        cur = nxt


def fetch_all_orders(session: requests.Session, cfg: Config) -> List[Dict[str, Any]]:
    all_rows: List[Dict[str, Any]] = []
    seen_order_ids: set[str] = set()

    seven_days_ms = 7 * 24 * 60 * 60 * 1000
    for chunk_start, chunk_end in iter_time_chunks(cfg.start_ms, cfg.end_ms, seven_days_ms):
        cursor: Optional[int] = None
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
                break

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

            if len(rows) < cfg.orders_limit or new_count == 0 or max_order_id is None or max_order_id == cursor:
                break
            cursor = max_order_id

    return all_rows


def fetch_all_fills(session: requests.Session, cfg: Config) -> List[Dict[str, Any]]:
    all_rows: List[Dict[str, Any]] = []
    seen_trade_ids: set[str] = set()

    chunk_ms = max(1, cfg.fill_chunk_hours) * 60 * 60 * 1000
    for chunk_start, chunk_end in iter_time_chunks(cfg.start_ms, cfg.end_ms, chunk_ms):
        params = {
            "tradingUnit": cfg.trading_unit,
            "startTs": chunk_start,
            "endTs": chunk_end,
            "currency": cfg.currency,
        }
        data = request_signed(session, "GET", "/openApi/swap/v2/trade/allFillOrders", params, cfg)
        rows = data if isinstance(data, list) else []
        for row in rows:
            if str(row.get("symbol")) != cfg.symbol:
                continue
            tid = str(row.get("tradeId"))
            if tid in seen_trade_ids:
                continue
            seen_trade_ids.add(tid)
            all_rows.append(row)

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
) -> List[Dict[str, Any]]:
    by_order_id: Dict[str, Dict[str, Any]] = {}
    for row in orders:
        by_order_id[str(row.get("orderId"))] = row

    joined: List[Dict[str, Any]] = []
    for fill in fills:
        order = by_order_id.get(str(fill.get("orderId")), {})
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


def main() -> int:
    args = parse_args()

    if not args.api_key or not args.api_secret:
        print("Set --api-key/--api-secret or environment variables BINGX_API_KEY/BINGX_API_SECRET", file=sys.stderr)
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
    )

    if cfg.end_ms <= cfg.start_ms:
        print("--end must be later than --start", file=sys.stderr)
        return 2

    out_path = Path(args.out)
    raw_json_path = Path(args.raw_json) if args.raw_json else None
    fills_csv_path = Path(args.fills_csv) if args.fills_csv else None

    session = requests.Session()

    orders = fetch_all_orders(session, cfg)
    fills = fetch_all_fills(session, cfg)
    joined = join_fills_with_orders(fills, orders)

    export_browser_like_csv(joined, out_path, cfg.tz_name)

    if raw_json_path:
        raw_json_path.write_text(json.dumps(joined, ensure_ascii=False, indent=2), encoding="utf-8")
    if fills_csv_path:
        export_normalized_fills_csv(joined, fills_csv_path, cfg.tz_name)

    print(f"orders={len(orders)}")
    print(f"fills={len(fills)}")
    print(f"joined={len(joined)}")
    print(f"out={out_path}")
    if raw_json_path:
        print(f"raw_json={raw_json_path}")
    if fills_csv_path:
        print(f"fills_csv={fills_csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
