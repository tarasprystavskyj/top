import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parents[1]
for item in (REPO_ROOT, BACKEND_DIR):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

import api_main
from scripts.build_hype_consolidated_session import build_consolidated_session


def _write_json(path: Path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_source_session(root: Path, name: str, start: str, order_id: str) -> Path:
    session = root / name
    session.mkdir(parents=True)
    base = pd.Timestamp(start, tz="UTC")
    pd.DataFrame(
        [
            {"ts": (base + pd.Timedelta(minutes=idx)).isoformat(), "value": float(idx)}
            for idx in range(3)
        ]
    ).to_csv(session / "live_equity.csv", index=False)
    pd.DataFrame(
        [
            {
                "ts": (base + pd.Timedelta(minutes=idx)).isoformat(),
                "open": 50.0 + idx,
                "high": 50.5 + idx,
                "low": 49.5 + idx,
                "close": 50.2 + idx,
            }
            for idx in range(3)
        ]
    ).to_csv(session / "live_candles.csv", index=False)
    (session / "telemetry.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "poll",
                        "status": {
                            "utc": (base + pd.Timedelta(hours=1)).isoformat(),
                            "input_meta": {"market": {"mark": 55.5}},
                        },
                    }
                )
            ]
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "ts": (base + pd.Timedelta(minutes=1)).isoformat(),
                "type": "open",
                "side": "LONG",
                "symbol": "HYPE-USDT",
                "price": 51.0,
                "qty": 0.1,
                "order_id": order_id,
                "position_id": "p1",
                "pnl": 0.0,
            }
        ]
    ).to_csv(session / "live_chart_events.csv", index=False)
    _write_json(session / "RUN_STATUS.json", {"status": "stopped", "updated_at": base.isoformat()})
    con = sqlite3.connect(session / "session.sqlite")
    con.execute(
        "create table orders (order_id text, ts_utc text, bar_time_utc text, mode text, symbol text, side text, type text, price real, qty real, status text, reason text, run_id text, extra text)"
    )
    con.execute(
        "insert into orders values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            order_id,
            (base + pd.Timedelta(minutes=1)).isoformat(),
            (base + pd.Timedelta(minutes=1)).isoformat(),
            "hype",
            "HYPE-USDT",
            "LONG",
            "OPEN",
            51.0,
            0.1,
            "FILLED",
            "lead_open_position_detected",
            "r1",
            json.dumps({"fill": {"fill_type": "base_entry", "live_fill_price": 51.0, "secret_like_noise": "drop-me"}}),
        ),
    )
    con.execute(
        "create table open_positions (bot_id text, symbol text, side text, qty real, entry real, tp_price real, sl_price real, ts_open text, run_id text, exchange text, timeframe text, status text, ts_close text, entry_fill real, entry_fill_ts text, exit_fill real, exit_fill_ts text, close_reason text)"
    )
    con.commit()
    con.close()
    return session


def test_build_hype_consolidated_session_is_compact_and_api_readable(monkeypatch, tmp_path):
    live_root = tmp_path / "_live"
    source_a = _make_source_session(live_root, "hype_a", "2026-05-25T21:30:00Z", "order-a")
    source_b = _make_source_session(live_root, "hype_b", "2026-05-26T21:30:00Z", "order-b")
    output = live_root / "hype_consolidated"

    manifest = build_consolidated_session(
        live_root=live_root,
        output=output,
        sources=[source_a, source_b],
        max_points=10,
        force=True,
    )

    assert manifest["counts_full"]["price_bars_raw"] == 8
    assert manifest["counts_full"]["synthetic_gap_fills"] > 0
    assert manifest["counts_full"]["skipped_large_telemetry_files"] == 0
    assert (output / "chart.json").exists()
    assert (output / "telemetry.jsonl").exists()
    assert not list(output.glob("run_telemetry_*.jsonl"))
    assert not list(output.glob("live_stdout_*.log"))
    status = json.loads((output / "RUN_STATUS.json").read_text(encoding="utf-8"))
    chart_json = json.loads((output / "chart.json").read_text(encoding="utf-8"))
    body_last_ts = max(row["ts"] for row in chart_json["price_bars"])
    assert status["updated_at"] == manifest["generated_at"]
    assert status["data_updated_at"] == body_last_ts

    telemetry_line = (output / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()[0]
    assert "hype_live_poll_compact_v1" in telemetry_line
    assert "secret_like_noise" not in (output / "orders.csv").read_text(encoding="utf-8")

    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(live_root))
    monkeypatch.setattr(api_main, "LIVE_TOP_REPORTS_DIR", str(tmp_path / "missing_top"))
    monkeypatch.setattr(api_main, "LIVE_REPO_REPORTS_DIR", str(tmp_path / "missing_reports"))
    monkeypatch.setattr(api_main, "LIVE_VERONIKA_REPORTS_DIR", str(tmp_path / "missing_veronika"))
    client = TestClient(api_main.app)

    listed = client.get("/api/backtest_live_validation/live_sessions")
    assert listed.status_code == 200
    assert "hype_consolidated" in {row["name"] for row in listed.json()["sessions"]}

    chart = client.get("/api/backtest_live_validation/live_session/chart", params={"path": str(output)})
    assert chart.status_code == 200
    body = chart.json()
    assert body["schema"] == "hype_consolidated_chart_v1"
    assert len(body["price_bars"]) > 8
    assert body["price_bars"][-1]["close"] == 55.5
    assert any("flat price-bar gap fill" in warning for warning in body["warnings"])
    assert len(body["markers"]) == 2
