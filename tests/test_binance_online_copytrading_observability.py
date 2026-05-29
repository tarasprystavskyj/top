from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from datetime import datetime, timezone

from obw_platform.meta_strategies.binance_online_copytrading.binance_online_copytrading import (
    write_observability,
)


def test_write_observability_creates_production_artifacts(tmp_path):
    session_db = tmp_path / "session.sqlite"
    shadow_orders = tmp_path / "shadow_orders.jsonl"
    state = {
        "open_positions": {
            "lead:test:BTCUSDT:LONG": {
                "key": "lead:test:BTCUSDT:LONG",
                "strategy_name": "lead",
                "portfolio_id": "p1",
                "mode": "follow_open",
                "signal_id": "test",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "detected_utc": "2026-05-19T19:00:00Z",
                "last_seen_utc": "2026-05-19T19:01:00Z",
                "entry_mark_price": 100.0,
                "entry_exec_price": 100.1,
                "notional_usdt": 50.0,
                "lead_trader_name": "Test Trader",
            }
        },
        "closed_trades": [],
        "last_poll": {
            "utc": "2026-05-19T19:01:00Z",
            "paper_exchange": "bingx",
            "slippage_bp": 9.38,
            "ttl_hours": 72.0,
            "leads": [{"name": "lead", "events": 1}],
            "events": [
                {
                    "type": "paper_entry",
                    "key": "lead:test:BTCUSDT:LONG",
                    "strategy": "lead",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                }
            ],
        },
    }
    args = Namespace(
        dry_run=False,
        session_db=str(session_db),
        shadow_orders_path=str(shadow_orders),
        run_id="TEST_RUN",
    )

    write_observability(
        state=state,
        result={"open_paper_positions": 1, "closed_paper_trades": 0},
        cfg={"paper_notional_usdt": 50.0},
        args=args,
        run_id="TEST_RUN",
        now=datetime(2026, 5, 19, 19, 1, tzinfo=timezone.utc),
    )
    write_observability(
        state=state,
        result={"open_paper_positions": 1, "closed_paper_trades": 0},
        cfg={"paper_notional_usdt": 50.0},
        args=args,
        run_id="TEST_RUN",
        now=datetime(2026, 5, 19, 19, 1, tzinfo=timezone.utc),
    )

    con = sqlite3.connect(session_db)
    cur = con.cursor()
    assert cur.execute("select count(*) from signal_polls").fetchone()[0] == 1
    assert cur.execute("select count(*) from paper_events").fetchone()[0] == 1
    assert cur.execute("select count(*) from paper_positions where status='OPEN'").fetchone()[0] == 1
    assert cur.execute("select count(*) from shadow_orders").fetchone()[0] == 1
    assert cur.execute("select count(*) from orders").fetchone()[0] == 1
    assert cur.execute("select position_value_usdt from equity").fetchone()[0] == 50.0
    con.close()

    rows = [json.loads(line) for line in shadow_orders.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {
            "client_order_id": rows[0]["client_order_id"],
            "created_at_utc": "2026-05-19T19:01:00Z",
            "event": "entry",
            "lead_trader_name": "Test Trader",
            "mode": "follow_open",
            "price_hint": 100.1,
            "reason": "paper_open_position",
            "side": "LONG",
            "source": "binance_copy",
            "source_id": "lead:test:BTCUSDT:LONG",
            "symbol": "BTC/USDT:USDT",
            "target_notional": 50.0,
        }
    ]
