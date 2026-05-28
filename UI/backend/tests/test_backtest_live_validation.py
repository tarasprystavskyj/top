import sys
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.append(str(BACKEND_DIR))

import api_main


@pytest.fixture(autouse=True)
def _isolate_live_root_env(monkeypatch):
    monkeypatch.delenv("BACKTEST_LIVE_VALIDATION_LIVE_ROOTS", raising=False)
    monkeypatch.delenv("LIVE_RESULTS_ROOTS", raising=False)


def _write_json(path: Path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def _sample_tv_csv(path: Path):
    df = pd.DataFrame(
        [
            {"Trade #": 1, "Type": "Entry short", "Date and time": "2026-04-10 00:00:00", "Signal": "Sell", "Price USDT": 100, "Size (qty)": 1, "Net P&L USDT": 0},
            {"Trade #": 2, "Type": "Exit short", "Date and time": "2026-04-10 00:05:00", "Signal": "Buy", "Price USDT": 95, "Size (qty)": 1, "Net P&L USDT": 5},
            {"Trade #": 3, "Type": "Entry short", "Date and time": "2026-04-10 00:10:00", "Signal": "Sell", "Price USDT": 98, "Size (qty)": 1, "Net P&L USDT": 0},
        ]
    )
    df.to_csv(path, index=False)


def test_files_and_inspect(monkeypatch, tmp_path):
    src = tmp_path / "TV_backtest_source"
    src.mkdir()
    tv = src / "X_BINGX_ENAUSDT.P_sample.csv"
    _sample_tv_csv(tv)

    monkeypatch.setattr(api_main, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(api_main, "TV_BACKTEST_SOURCE_DIR", str(src))

    client = TestClient(api_main.app)
    resp = client.get("/api/backtest_live_validation/files")
    assert resp.status_code == 200
    data = resp.json()["files"]
    assert data and data[0]["name"].endswith(".csv")

    inspect = client.post("/api/backtest_live_validation/inspect", json={"path": str(tv)})
    assert inspect.status_code == 200
    body = inspect.json()
    assert body["symbol"] == "ENA-USDT"
    assert body["bar_interval_seconds"] == 300


def test_run_and_details(monkeypatch, tmp_path):
    src = tmp_path / "TV_backtest_source"
    src.mkdir()
    reports = tmp_path / "reports"
    reports.mkdir()
    tv = src / "X_BINGX_ENAUSDT.P_sample.csv"
    _sample_tv_csv(tv)

    monkeypatch.setattr(api_main, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(api_main, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(api_main, "TV_BACKTEST_SOURCE_DIR", str(src))
    monkeypatch.setattr(api_main, "VALIDATION_REPORTS_DIR", str(reports))
    monkeypatch.setattr(api_main, "BT_ROOT", str(tmp_path))

    class FakeProc:
        def __init__(self):
            self.returncode = 0
            self.stdout = "ok"
            self.stderr = ""

    def fake_run(cmd, cwd=None, capture_output=False, text=False):
        run_id = cmd[cmd.index("--label") + 1]
        run_dir = reports / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        bx = pd.DataFrame(
            [
                {"Час виконання": "10/04/26 12:05 AM", "Ф’ючерси / Напрямок": "Відкрити Short", "Виконано": "1", "Ціна виконання": "100", "Закриті PnL / %": "0 USDT", "Комісія": "-0.1"},
                {"Час виконання": "10/04/26 12:10 AM", "Ф’ючерси / Напрямок": "Закрити Short", "Виконано": "1", "Ціна виконання": "95", "Закриті PnL / %": "5 USDT", "Комісія": "-0.1"},
            ]
        )
        # extractor may use dash-preserving safe stem (ENA-USDT) depending on environment
        bx.to_csv(run_dir / "ENA-USDT_trade_history_for_match.csv", index=False)
        matched = pd.DataFrame(
            [{"real_time": "2026-04-10T00:10:00Z", "real_net_pnl": 4.9, "real_qty": 1.0, "real_price": 95.0, "signed_slippage_bps": 1.2, "abs_slippage_bps": 2.0}]
        )
        matched.to_csv(run_dir / f"{run_id}_matched_orders.csv", index=False)
        pd.DataFrame([]).to_csv(run_dir / f"{run_id}_unmatched_real_orders.csv", index=False)
        pd.DataFrame([]).to_csv(run_dir / f"{run_id}_unmatched_tv_packs.csv", index=False)
        return FakeProc()

    monkeypatch.setattr(api_main.subprocess, "run", fake_run)

    client = TestClient(api_main.app)
    run_resp = client.post("/api/backtest_live_validation/run", json={"path": str(tv), "run_match": True, "debug": True})
    assert run_resp.status_code == 200
    run_data = run_resp.json()
    assert run_data["run_id"]

    details = client.get(f"/api/backtest_live_validation/run/{run_data['run_id']}")
    assert details.status_code == 200
    body = details.json()
    assert "pnl_chart" in body
    assert "margin_chart" in body
    assert "slippage_chart" in body
    assert "stats" in body

    poll = client.get(f"/api/backtest_live_validation/run/{run_data['run_id']}/poll")
    assert poll.status_code == 200
    assert "poll_interval_ms" in poll.json()


def test_run_without_auto_fetch_live(monkeypatch, tmp_path):
    src = tmp_path / "TV_backtest_source"
    src.mkdir()
    reports = tmp_path / "reports"
    reports.mkdir()
    tv = src / "X_BINGX_ENAUSDT.P_sample.csv"
    _sample_tv_csv(tv)

    monkeypatch.setattr(api_main, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(api_main, "TV_BACKTEST_SOURCE_DIR", str(src))
    monkeypatch.setattr(api_main, "VALIDATION_REPORTS_DIR", str(reports))
    monkeypatch.setattr(api_main, "BT_ROOT", str(tmp_path))
    live_root = tmp_path / "_reports" / "_live"
    live_session = live_root / "ena_bundle"
    live_session.mkdir(parents=True)
    pd.DataFrame([{"Час виконання": "10/04/26 12:05 AM", "Виконано": "1"}]).to_csv(live_session / "ENA_USDT_trade_history_for_match.csv", index=False)
    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(live_root))

    def forbid_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called when auto_fetch_live=false")

    monkeypatch.setattr(api_main.subprocess, "run", forbid_run)

    client = TestClient(api_main.app)
    run_resp = client.post(
        "/api/backtest_live_validation/run",
        json={"path": str(tv), "auto_fetch_live": False, "live_path": str(live_session), "run_match": True, "debug": True},
    )
    assert run_resp.status_code == 200
    run_data = run_resp.json()
    assert run_data["run_id"]
    assert run_data["status"]["auto_fetch_live"] is False


def _create_valid_live_session(base: Path, name: str = "session_ok") -> Path:
    session = base / name
    session.mkdir(parents=True, exist_ok=True)
    _write_json(
        session / "status.json",
        {
            "exchange": "bingx",
            "timeframe": "5m",
            "status": "running",
            "started_at": "2026-04-10T00:00:00Z",
            "updated_at": "2026-04-10T00:15:00Z",
            "open_legs": 1,
            "filled_orders": 2,
            "last_debug_event": {"level": "info", "event_type": "heartbeat", "ts": "2026-04-10T00:15:00Z"},
        },
    )
    pd.DataFrame(
        [
            {"ts": "2026-04-10T00:00:00Z", "value": 10000},
            {"ts": "2026-04-10T00:05:00Z", "value": 10025},
        ]
    ).to_csv(session / "equity.csv", index=False)
    pd.DataFrame(
        [
            {"ts": "2026-04-10T00:00:00Z", "value": 10000},
            {"ts": "2026-04-10T00:05:00Z", "value": 10020},
        ]
    ).to_csv(session / "backtest_equity.csv", index=False)
    pd.DataFrame([{"symbol": "ENA-USDT", "side": "short", "qty": 1.0}]).to_csv(session / "open_positions.csv", index=False)
    pd.DataFrame([{"id": 1, "status": "filled", "price": 99.0}]).to_csv(session / "orders.csv", index=False)
    (session / "debug_events.jsonl").write_text(
        '\n'.join(
            [
                json.dumps({"ts": "2026-04-10T00:01:00Z", "level": "info", "event_type": "start"}),
                json.dumps({"ts": "2026-04-10T00:05:00Z", "level": "warn", "event_type": "slow_loop"}),
            ]
        ),
        encoding="utf-8",
    )
    (session / "stdio.log").write_text("line a\nline b\n", encoding="utf-8")
    return session


def test_live_sessions_empty_dir(monkeypatch, tmp_path):
    live_root = tmp_path / "_reports" / "_live"
    live_root.mkdir(parents=True)
    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(live_root))
    monkeypatch.setattr(api_main, "LIVE_TOP_REPORTS_DIR", str(tmp_path / "missing_top"))
    monkeypatch.setattr(api_main, "LIVE_REPO_REPORTS_DIR", str(tmp_path / "missing_reports"))
    monkeypatch.setattr(api_main, "LIVE_VERONIKA_REPORTS_DIR", str(tmp_path / "missing_veronika"))

    client = TestClient(api_main.app)
    resp = client.get("/api/backtest_live_validation/live_sessions")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["sessions"] == []
    assert payload["root"] == str(live_root)


def test_live_session_partial_malformed(monkeypatch, tmp_path):
    live_root = tmp_path / "_reports" / "_live"
    broken = live_root / "broken_session"
    broken.mkdir(parents=True)
    (broken / "status.json").write_text("{not-json", encoding="utf-8")
    (broken / "stdio.log").write_text("oops\n", encoding="utf-8")
    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(live_root))
    monkeypatch.setattr(api_main, "LIVE_TOP_REPORTS_DIR", str(tmp_path / "missing_top"))
    monkeypatch.setattr(api_main, "LIVE_REPO_REPORTS_DIR", str(tmp_path / "missing_reports"))
    monkeypatch.setattr(api_main, "LIVE_VERONIKA_REPORTS_DIR", str(tmp_path / "missing_veronika"))

    client = TestClient(api_main.app)
    listed = client.get("/api/backtest_live_validation/live_sessions")
    assert listed.status_code == 200
    sessions = listed.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["status"] in {"unknown", "stopped", "running", "error"}

    inspect = client.post("/api/backtest_live_validation/live_session/inspect", json={"path": str(broken)})
    assert inspect.status_code == 200
    body = inspect.json()
    assert body["path"] == str(broken)
    assert "open_legs" in body
    assert "filled_orders" in body

    chart = client.get("/api/backtest_live_validation/live_session/chart", params={"path": str(broken)})
    assert chart.status_code == 200
    assert chart.json() == {}

    table = client.get("/api/backtest_live_validation/live_session/table", params={"path": str(broken), "kind": "stdio"})
    assert table.status_code == 200
    assert isinstance(table.json()["rows"], list)


def test_live_session_endpoints_with_valid_session(monkeypatch, tmp_path):
    live_root = tmp_path / "_reports" / "_live"
    session = _create_valid_live_session(live_root)
    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(live_root))
    monkeypatch.setattr(api_main, "LIVE_TOP_REPORTS_DIR", str(tmp_path / "missing_top"))
    monkeypatch.setattr(api_main, "LIVE_REPO_REPORTS_DIR", str(tmp_path / "missing_reports"))
    monkeypatch.setattr(api_main, "LIVE_VERONIKA_REPORTS_DIR", str(tmp_path / "missing_veronika"))

    client = TestClient(api_main.app)

    listed = client.get("/api/backtest_live_validation/live_sessions")
    assert listed.status_code == 200
    sessions = listed.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["name"] == "session_ok"
    assert sessions[0]["exchange"] == "bingx"
    assert sessions[0]["timeframe"] == "5m"

    inspect = client.post("/api/backtest_live_validation/live_session/inspect", json={"path": str(session)})
    assert inspect.status_code == 200
    inspect_body = inspect.json()
    assert inspect_body["status"] == "running"
    assert inspect_body["open_legs"] >= 1
    assert inspect_body["filled_orders"] >= 1

    status = client.get("/api/backtest_live_validation/live_session/status", params={"path": str(session)})
    assert status.status_code == 200
    status_body = status.json()
    assert status_body["path"] == str(session)
    assert status_body["status"] == "running"

    chart = client.get("/api/backtest_live_validation/live_session/chart", params={"path": str(session)})
    assert chart.status_code == 200
    chart_body = chart.json()
    assert isinstance(chart_body.get("live"), list)
    assert isinstance(chart_body.get("backtest"), list)
    assert isinstance(chart_body.get("distance"), list)

    for kind in ("open_positions", "orders", "debug_events", "stdio"):
        table = client.get("/api/backtest_live_validation/live_session/table", params={"path": str(session), "kind": kind})
        assert table.status_code == 200
        assert isinstance(table.json().get("rows"), list)


def test_live_chart_uses_selected_tv_and_session_sqlite(monkeypatch, tmp_path):
    src = tmp_path / "TV_backtest_source"
    src.mkdir()
    tv = src / "C_-_SHORT_-_MA_driven_BINGX_ENAUSDT.P_2026-04-16_c2859.csv"
    _sample_tv_csv(tv)

    live_root = tmp_path / "_reports" / "_live"
    session = live_root / "sqlite_only_session"
    session.mkdir(parents=True)
    con = sqlite3.connect(session / "session.sqlite")
    con.execute("CREATE TABLE config_snapshots (run_id TEXT, ts_utc TEXT, cfg_json TEXT)")
    con.execute(
        "INSERT INTO config_snapshots VALUES (?, ?, ?)",
        ("r1", "2026-04-10T00:00:00Z", json.dumps({"initial_equity": 10000})),
    )
    con.execute(
        "CREATE TABLE equity (run_id TEXT, ts_utc TEXT, equity_usdt REAL, cash_usdt REAL, position_value_usdt REAL, realized_pnl_cum REAL, unrealized_pnl REAL)"
    )
    con.executemany(
        "INSERT INTO equity VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("r1", "2026-04-10T00:00:00Z", 10000, 10000, 0, 0, 0),
            ("r1", "2026-04-10T00:05:00Z", 10005, 10005, 0, 5, 0),
            ("r1", "2026-04-10T00:10:00Z", 10008, 10008, 0, 8, 0),
        ],
    )
    con.commit()
    con.close()

    monkeypatch.setattr(api_main, "TV_BACKTEST_SOURCE_DIR", str(src))
    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(live_root))
    monkeypatch.setattr(api_main, "LIVE_TOP_REPORTS_DIR", str(tmp_path / "missing_top"))
    monkeypatch.setattr(api_main, "LIVE_REPO_REPORTS_DIR", str(tmp_path / "missing_reports"))
    monkeypatch.setattr(api_main, "LIVE_VERONIKA_REPORTS_DIR", str(tmp_path / "missing_veronika"))

    client = TestClient(api_main.app)
    chart = client.get("/api/backtest_live_validation/live_session/chart", params={"path": str(session), "tv_path": str(tv)})
    assert chart.status_code == 200
    body = chart.json()
    assert [p["value"] for p in body["live"]] == [0.0, 5.0, 8.0]
    assert len(body["backtest_price"]) == 3
    assert body["backtest_price"][0]["value"] == 100.0


def test_live_session_endpoint_validation(monkeypatch, tmp_path):
    live_root = tmp_path / "_reports" / "_live"
    session = _create_valid_live_session(live_root, "session_guardrails")
    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(live_root))

    client = TestClient(api_main.app)

    bad_kind = client.get("/api/backtest_live_validation/live_session/table", params={"path": str(session), "kind": "bad_kind"})
    assert bad_kind.status_code == 400

    outside_path = client.get("/api/backtest_live_validation/live_session/status", params={"path": str(tmp_path)})
    assert outside_path.status_code == 400

    missing_path = client.get("/api/backtest_live_validation/live_session/chart")
    assert missing_path.status_code == 400


def test_live_sessions_include_repo_reports_hype_canary(monkeypatch, tmp_path):
    legacy_root = tmp_path / "obw_platform" / "_reports" / "_live"
    top_live_reports = tmp_path / "_reports" / "_live"
    repo_reports = tmp_path / "reports"
    sibling_reports = tmp_path / "veronika" / "reports"
    legacy_root.mkdir(parents=True)
    top_live_reports.mkdir(parents=True)
    repo_reports.mkdir()
    sibling_reports.mkdir(parents=True)
    unrelated = repo_reports / "not_a_live_session"
    unrelated.mkdir()
    (unrelated / "notes.txt").write_text("ignore me\n", encoding="utf-8")

    session = repo_reports / "hype_canary_bingx_live_20260525"
    session.mkdir()
    _write_json(
        session / "RUN_STATUS.json",
        {
            "exchange": "bingx",
            "timeframe": "5m",
            "status": "running",
            "started_at": "2026-05-25T00:00:00Z",
            "updated_at": "2026-05-25T00:05:00Z",
            "candidate_params": {"fresh_tp_percent": 1.4, "normal_base_pct": 10.0},
        },
    )
    _write_json(session / "active_status.json", {"open_legs": 1, "filled_orders": 2})
    (session / "ACTIVE_STATUS_PATH.txt").write_text("active_status.json\n", encoding="utf-8")
    (session / "live_stdout_20260525.log").write_text("hype canary started\n", encoding="utf-8")
    (session / "run_telemetry_20260525T000000Z.jsonl").write_text(
        json.dumps({"ts": "2026-05-25T00:02:00Z", "input_meta": {"market": {"mark": 25.2}}}) + "\n"
        + json.dumps({"ts": "2026-05-25T00:03:00Z", "input_meta": {"market": {"mark": 25.4}}}) + "\n",
        encoding="utf-8",
    )

    con = sqlite3.connect(session / "session.sqlite")
    try:
        con.execute(
            "CREATE TABLE open_positions (symbol TEXT, status TEXT, qty REAL, entry_fill REAL, ts_utc TEXT)"
        )
        con.execute(
            "INSERT INTO open_positions VALUES ('HYPE-USDT', 'OPEN', 1.5, 25.0, '2026-05-25T00:01:00Z')"
        )
        con.execute(
            "CREATE TABLE orders (order_id TEXT, symbol TEXT, side TEXT, type TEXT, status TEXT, price REAL, qty REAL, ts_utc TEXT)"
        )
        con.execute(
            "INSERT INTO orders VALUES ('order-1', 'HYPE-USDT', 'LONG', 'OPEN', 'filled', 25.1, 1.5, '2026-05-25T00:02:00Z')"
        )
        con.commit()
    finally:
        con.close()

    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(legacy_root))
    monkeypatch.setattr(api_main, "LIVE_TOP_REPORTS_DIR", str(top_live_reports))
    monkeypatch.setattr(api_main, "LIVE_REPO_REPORTS_DIR", str(repo_reports))
    monkeypatch.setattr(api_main, "LIVE_VERONIKA_REPORTS_DIR", str(sibling_reports))

    client = TestClient(api_main.app)

    listed = client.get("/api/backtest_live_validation/live_sessions")
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["root"] == str(legacy_root)
    assert str(top_live_reports) in payload["roots"]
    assert str(repo_reports) in payload["roots"]
    assert str(sibling_reports) in payload["roots"]
    names = [item["name"] for item in payload["sessions"]]
    assert names == ["hype_canary_bingx_live_20260525"]
    summary = payload["sessions"][0]
    assert summary["exchange"] == "bingx"
    assert summary["open_legs"] == 1
    assert summary["filled_orders"] == 2

    inspect = client.post("/api/backtest_live_validation/live_session/inspect", json={"path": str(session)})
    assert inspect.status_code == 200
    assert inspect.json()["path"] == str(session)

    positions = client.get(
        "/api/backtest_live_validation/live_session/table",
        params={"path": str(session), "kind": "open_positions"},
    )
    assert positions.status_code == 200
    assert positions.json()["rows"][0]["symbol"] == "HYPE-USDT"

    orders = client.get(
        "/api/backtest_live_validation/live_session/table",
        params={"path": str(session), "kind": "orders"},
    )
    assert orders.status_code == 200
    assert orders.json()["rows"][0]["symbol"] == "HYPE-USDT"

    chart = client.get(
        "/api/backtest_live_validation/live_session/chart",
        params={"path": str(session)},
    )
    assert chart.status_code == 200
    chart_body = chart.json()
    assert chart_body["live"][0]["value"] > 0
    assert chart_body["sources"]["live"] == "session.sqlite:orders"
    assert chart_body["approximate"] is True
    assert chart_body["mark"][-1]["value"] == 25.4
    assert chart_body["markers"][0]["text"] in {"DCA buy", "Meta strategy open"}
    assert any(item["layer"] == "parameters" for item in chart_body["labels"])

    generated_csv = tmp_path / "HYPE_USDT_trade_history_for_match.csv"
    generated = api_main._write_live_orders_match_csv_from_sqlite(str(session), str(generated_csv), "HYPE-USDT")
    assert generated == str(generated_csv)
    generated_df = pd.read_csv(generated_csv)
    assert set(["Час виконання", "Ф’ючерси / Напрямок", "Виконано", "Ціна виконання", "Ордер №"]).issubset(generated_df.columns)
    assert "HYPEUSDT" in str(generated_df.iloc[0]["Ф’ючерси / Напрямок"])

    outside = client.get(
        "/api/backtest_live_validation/live_session/status",
        params={"path": str(tmp_path / "outside")},
    )
    assert outside.status_code == 400


def test_live_sessions_include_sibling_veronika_reports(monkeypatch, tmp_path):
    legacy_root = tmp_path / "obw_platform" / "_reports" / "_live"
    top_live_reports = tmp_path / "top_1" / "_reports" / "_live"
    repo_reports = tmp_path / "top_1" / "reports"
    sibling_reports = tmp_path / "veronika" / "reports"
    legacy_root.mkdir(parents=True)
    top_live_reports.mkdir(parents=True)
    repo_reports.mkdir(parents=True)
    sibling_reports.mkdir(parents=True)

    session = sibling_reports / "hype_canary_bingx_live_20260525"
    session.mkdir()
    _write_json(
        session / "RUN_STATUS.json",
        {
            "live_exchange": "bingx",
            "live_symbol": "HYPE-USDT",
            "paper_only": False,
            "status": "running",
            "open_paper_trades": [{"symbol": "HYPEUSDT", "side": "LONG"}],
            "utc": "2026-05-26T05:00:00Z",
        },
    )

    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(legacy_root))
    monkeypatch.setattr(api_main, "LIVE_TOP_REPORTS_DIR", str(top_live_reports))
    monkeypatch.setattr(api_main, "LIVE_REPO_REPORTS_DIR", str(repo_reports))
    monkeypatch.setattr(api_main, "LIVE_VERONIKA_REPORTS_DIR", str(sibling_reports))

    client = TestClient(api_main.app)
    listed = client.get("/api/backtest_live_validation/live_sessions")
    assert listed.status_code == 200
    payload = listed.json()
    assert str(sibling_reports) in payload["roots"]
    assert [item["name"] for item in payload["sessions"]] == ["hype_canary_bingx_live_20260525"]

    status = client.get(
        "/api/backtest_live_validation/live_session/status",
        params={"path": str(session)},
    )
    assert status.status_code == 200
    assert status.json()["status"] == "running"

    positions = client.get(
        "/api/backtest_live_validation/live_session/table",
        params={"path": str(session), "kind": "open_positions"},
    )
    assert positions.status_code == 200
    assert positions.json()["rows"][0]["live_symbol"] == "HYPE-USDT"
    assert positions.json()["rows"][0]["source"] == "RUN_STATUS.json"


def test_live_session_chart_prefers_live_equity_artifact(monkeypatch, tmp_path):
    live_root = tmp_path / "_reports" / "_live"
    session = _create_valid_live_session(live_root, "hype_canary_with_artifacts")
    pd.DataFrame(
        [
            {"ts": "2026-05-25T00:00:00Z", "value": 0.0, "equity": 30.0},
            {"ts": "2026-05-25T00:01:00Z", "value": 1.25, "equity": 31.25},
        ]
    ).to_csv(session / "live_equity.csv", index=False)
    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(live_root))
    monkeypatch.setattr(api_main, "LIVE_TOP_REPORTS_DIR", str(tmp_path / "missing_top"))
    monkeypatch.setattr(api_main, "LIVE_REPO_REPORTS_DIR", str(tmp_path / "missing_reports"))
    monkeypatch.setattr(api_main, "LIVE_VERONIKA_REPORTS_DIR", str(tmp_path / "missing_veronika"))

    client = TestClient(api_main.app)
    chart = client.get("/api/backtest_live_validation/live_session/chart", params={"path": str(session)})

    assert chart.status_code == 200
    body = chart.json()
    assert body["live"][-1]["value"] == 1.25
    assert body["sources"]["live"] == "live_equity.csv"
    assert body.get("approximate") is None
    assert body.get("backtest")


def test_live_session_chart_prefers_broader_sqlite_equity(monkeypatch, tmp_path):
    live_root = tmp_path / "_reports" / "_live"
    session = _create_valid_live_session(live_root, "hype_canary_with_broader_sqlite")
    pd.DataFrame(
        [
            {"ts": "2026-05-28T08:49:00Z", "value": 4.0, "equity": 104.0},
            {"ts": "2026-05-28T08:50:00Z", "value": 5.0, "equity": 105.0},
        ]
    ).to_csv(session / "live_equity.csv", index=False)
    con = sqlite3.connect(session / "session.sqlite")
    try:
        con.execute("CREATE TABLE equity (run_id TEXT, ts_utc TEXT, equity_usdt REAL)")
        con.executemany(
            "INSERT INTO equity VALUES ('run-1', ?, ?)",
            [
                ("2026-05-26T07:49:10Z", 100.0),
                ("2026-05-26T07:49:40Z", 101.0),
                ("2026-05-28T08:50:00Z", 106.0),
            ],
        )
        con.commit()
    finally:
        con.close()
    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(live_root))
    monkeypatch.setattr(api_main, "LIVE_TOP_REPORTS_DIR", str(tmp_path / "missing_top"))
    monkeypatch.setattr(api_main, "LIVE_REPO_REPORTS_DIR", str(tmp_path / "missing_reports"))
    monkeypatch.setattr(api_main, "LIVE_VERONIKA_REPORTS_DIR", str(tmp_path / "missing_veronika"))

    client = TestClient(api_main.app)
    chart = client.get("/api/backtest_live_validation/live_session/chart", params={"path": str(session)})

    assert chart.status_code == 200
    body = chart.json()
    assert body["sources"]["live"] == "session.sqlite:equity"
    assert body["live"][0]["ts"] == "2026-05-26T07:49:00+00:00"
    assert body["live"][-1]["value"] == 6.0


def test_live_session_chart_exposes_mark_events_and_param_labels(monkeypatch, tmp_path):
    tv_src = tmp_path / "TV_backtest_source"
    tv_src.mkdir()
    tv = tv_src / "X_BINGX_HYPEUSDT.P_sample.csv"
    _sample_tv_csv(tv)
    live_root = tmp_path / "_reports" / "_live"
    session = live_root / "hype_chart_contract"
    session.mkdir(parents=True)
    _write_json(
        session / "RUN_STATUS.json",
        {
            "live_exchange": "bingx",
            "live_symbol": "HYPE-USDT",
            "utc": "2026-05-26T08:59:59Z",
            "candidate_params": {
                "fresh_base_pct": 28.0,
                "fresh_tp_percent": 1.4,
                "normal_base_pct": 10.0,
            },
        },
    )
    pd.DataFrame(
        [
            {"ts": "2026-05-26T08:58:00Z", "value": -0.1, "equity": 29.9},
            {"ts": "2026-05-26T08:59:00Z", "value": 0.2, "equity": 30.2},
        ]
    ).to_csv(session / "live_equity.csv", index=False)
    pd.DataFrame(
        [
            {"ts": "2026-05-26T08:58:00Z", "value": 0.0, "equity": 30.0, "realized_equity": 30.0},
            {"ts": "2026-05-26T08:59:00Z", "value": 0.3, "equity": 30.3, "realized_equity": 30.2},
        ]
    ).to_csv(session / "backtest_equity.csv", index=False)
    np.savez(
        session / "live_hype_1m_ohlcv_full_session.npz",
        symbols=np.array(["HYPE/USDT:USDT"], dtype=object),
        offsets=np.array([0, 2]),
        timestamp_s=np.array([1779785880, 1779785940]),
        open=np.array([59.5, 59.7]),
        high=np.array([59.8, 59.9]),
        low=np.array([59.4, 59.6]),
        close=np.array([59.7, 59.8]),
        volume=np.array([1.0, 1.2]),
    )
    (session / "run_telemetry_20260526T045829Z_restart.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"utc": "2026-05-26T08:58:00Z", "input_meta": {"market": {"mark": 59.5}}}),
                json.dumps({"event": "poll", "status": {"utc": "2026-05-26T08:59:00Z", "input_meta": {"market": {"mark": 59.8}}}}),
            ]
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {"ts": "2026-05-26T08:58:30Z", "type": "meta_open", "price": 59.6, "pnl": ""},
            {"ts": "2026-05-26T08:59:30Z", "type": "meta_full_close", "price": 59.8, "pnl": 0.25},
        ]
    ).to_csv(session / "live_chart_events.csv", index=False)
    con = sqlite3.connect(session / "session.sqlite")
    try:
        con.execute(
            "CREATE TABLE orders (order_id TEXT, ts_utc TEXT, bar_time_utc TEXT, mode TEXT, symbol TEXT, side TEXT, type TEXT, price REAL, qty REAL, status TEXT, reason TEXT, run_id TEXT, extra TEXT)"
        )
        con.execute(
            "INSERT INTO orders VALUES ('order-open', '2026-05-26T08:58:30Z', '2026-05-26T08:58:00Z', 'LIVE', 'HYPE-USDT', 'LONG', 'OPEN', 59.6, 0.1, 'FILLED', 'lead_open_position_detected', 'run-1', ?)",
            (json.dumps({"fill": {"fill_type": "base_entry"}}),),
        )
        con.execute(
            "INSERT INTO orders VALUES ('order-dca', '2026-05-26T08:59:30Z', '2026-05-26T08:59:00Z', 'LIVE', 'HYPE-USDT', 'LONG', 'OPEN', 59.4, 0.1, 'FILLED', 'mark_crossed_dca_level', 'run-1', ?)",
            (json.dumps({"fill": {"fill_type": "dca_add_1"}}),),
        )
        con.execute(
            "INSERT INTO orders VALUES ('order-close', '2026-05-26T09:00:30Z', '2026-05-26T09:00:00Z', 'LIVE', 'HYPE-USDT', 'LONG', 'CLOSE', 60.1, 0.2, 'FILLED', 'position_history_closed', 'run-1', ?)",
            (
                json.dumps(
                    {
                        "closed": {
                            "avg_entry": 59.5,
                            "levels": [59.4, 58.9],
                            "next_level_idx": 1,
                            "fills": [
                                {"fill_type": "base_entry", "live_fill_price": 59.6},
                                {"fill_type": "dca_add_1", "live_fill_price": 59.4},
                            ],
                        }
                    }
                ),
            ),
        )
        con.commit()
    finally:
        con.close()

    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(live_root))
    monkeypatch.setattr(api_main, "TV_BACKTEST_SOURCE_DIR", str(tv_src))
    monkeypatch.setattr(api_main, "LIVE_TOP_REPORTS_DIR", str(tmp_path / "missing_top"))
    monkeypatch.setattr(api_main, "LIVE_REPO_REPORTS_DIR", str(tmp_path / "missing_reports"))
    monkeypatch.setattr(api_main, "LIVE_VERONIKA_REPORTS_DIR", str(tmp_path / "missing_veronika"))

    client = TestClient(api_main.app)
    chart = client.get(
        "/api/backtest_live_validation/live_session/chart",
        params={"path": str(session), "backtest_path": str(tv)},
    )

    assert chart.status_code == 200
    body = chart.json()
    assert body["sources"]["mark"] == "run_telemetry_*.jsonl:1m"
    assert [p["value"] for p in body["mark"]] == [59.5, 59.8]
    assert len(body["price_bars"]) == 2
    assert body["price_bars"][0]["open"] == 59.5
    assert body["sources"]["price_bars"] == "live_hype_1m_ohlcv_full_session.npz"
    assert body["live_realized"][-1]["value"] == 0.25
    assert body["backtest_realized"][0]["value"] == 0.0
    assert {m["text"] for m in body["markers"]} >= {"Meta strategy open", "DCA buy"}
    assert any(label["text"].startswith("fresh_tp_percent") for label in body["labels"])
    assert {line["kind"] for line in body["price_lines"]} >= {"next_dca_buy", "dca_sell_target", "full_sell_tp"}
    assert any(line["text"] == "Next DCA buy" and line["price"] == 58.9 for line in body["price_lines"])
    assert [p["value"] for p in body["backtest_price"]] == [100.0, 95.0, 98.0]
    assert body["sources"]["backtest_price"] == "TradingView CSV:Price USDT"


def test_live_match_ready_prefers_runner_safe_symbol_csv(monkeypatch, tmp_path):
    live_root = tmp_path / "_reports" / "_live"
    session = live_root / "hype_canary_match_csv"
    session.mkdir(parents=True)
    safe_csv = session / "HYPE_USDT_trade_history_for_match.csv"
    dash_csv = session / "HYPE-USDT_trade_history_for_match.csv"
    safe_csv.write_text("safe\n", encoding="utf-8")
    dash_csv.write_text("dash\n", encoding="utf-8")
    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(live_root))
    monkeypatch.setattr(api_main, "LIVE_TOP_REPORTS_DIR", str(tmp_path / "missing_top"))
    monkeypatch.setattr(api_main, "LIVE_REPO_REPORTS_DIR", str(tmp_path / "missing_reports"))
    monkeypatch.setattr(api_main, "LIVE_VERONIKA_REPORTS_DIR", str(tmp_path / "missing_veronika"))

    assert api_main._find_live_match_ready_csv(str(session), "HYPE-USDT") == str(safe_csv)


def test_live_sessions_sort_newest_and_label_duplicate_names(monkeypatch, tmp_path):
    monkeypatch.setattr(api_main.time, "time", lambda: pd.Timestamp("2026-05-26T08:00:00Z").timestamp())
    production_root = tmp_path / "top_1" / "obw_platform" / "_reports" / "_live"
    legacy_root = tmp_path / "veronika" / "reports"
    other_root = tmp_path / "repo_reports"
    production_root.mkdir(parents=True)
    legacy_root.mkdir(parents=True)
    other_root.mkdir(parents=True)

    old = legacy_root / "hype_canary_bingx_live_20260525"
    old.mkdir()
    _write_json(
        old / "RUN_STATUS.json",
        {
            "live_exchange": "bingx",
            "live_symbol": "HYPE-USDT",
            "utc": "2026-05-26T06:22:32Z",
            "control": {"kill": True},
        },
    )

    older_other = other_root / "some_old_session"
    older_other.mkdir()
    _write_json(older_other / "RUN_STATUS.json", {"status": "running", "utc": "2026-05-26T05:00:00Z"})

    current = production_root / "hype_canary_bingx_live_20260525"
    current.mkdir()
    _write_json(
        current / "RUN_STATUS.json",
        {
            "live_exchange": "bingx",
            "live_symbol": "HYPE-USDT",
            "status": "running",
            "utc": "2026-05-26T07:55:00Z",
            "control": {"kill": False, "stop_new_orders": False},
        },
    )

    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(production_root))
    monkeypatch.setattr(api_main, "LIVE_TOP_REPORTS_DIR", str(tmp_path / "missing_top"))
    monkeypatch.setattr(api_main, "LIVE_REPO_REPORTS_DIR", str(other_root))
    monkeypatch.setattr(api_main, "LIVE_VERONIKA_REPORTS_DIR", str(legacy_root))

    client = TestClient(api_main.app)
    resp = client.get("/api/backtest_live_validation/live_sessions")

    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    assert sessions[0]["path"] == str(current)
    assert sessions[0]["status"] == "running"
    assert sessions[0]["duplicate_name"] is True
    assert "production" in sessions[0]["display_name"]
    stale = next(item for item in sessions if item["path"] == str(old))
    assert stale["status"] == "stopped"
    assert stale["stale_duplicate"] is True
    assert "legacy" in stale["display_name"]


def test_live_status_fresh_run_status_with_kill_control_is_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(api_main.time, "time", lambda: pd.Timestamp("2026-05-26T08:00:00Z").timestamp())
    session = tmp_path / "session"
    session.mkdir()
    _write_json(
        session / "RUN_STATUS.json",
        {
            "utc": "2026-05-26T07:55:00Z",
            "control": {"kill": True},
        },
    )

    assert api_main._infer_live_status(None, str(session), "2026-05-26T07:55:00Z", None) == "stopped"
    assert api_main._infer_live_status("running", str(session), "2026-05-26T07:55:00Z", None) == "stopped"


def test_live_sessions_include_top_reports_live_root(monkeypatch, tmp_path):
    legacy_root = tmp_path / "obw_platform" / "_reports" / "_live"
    top_live_reports = tmp_path / "top_1" / "_reports" / "_live"
    repo_reports = tmp_path / "top_1" / "reports"
    sibling_reports = tmp_path / "veronika" / "reports"
    legacy_root.mkdir(parents=True)
    top_live_reports.mkdir(parents=True)
    repo_reports.mkdir(parents=True)
    sibling_reports.mkdir(parents=True)

    session = top_live_reports / "hype_canary_bingx_live"
    session.mkdir()
    _write_json(
        session / "RUN_STATUS.json",
        {
            "live_exchange": "bingx",
            "live_symbol": "HYPE-USDT",
            "status": "running",
            "open_paper_trades": [{"symbol": "HYPEUSDT", "side": "LONG", "fills": 1}],
            "utc": "2026-05-26T05:10:00Z",
        },
    )

    monkeypatch.setattr(api_main, "LIVE_RESULTS_DIR", str(legacy_root))
    monkeypatch.setattr(api_main, "LIVE_TOP_REPORTS_DIR", str(top_live_reports))
    monkeypatch.setattr(api_main, "LIVE_REPO_REPORTS_DIR", str(repo_reports))
    monkeypatch.setattr(api_main, "LIVE_VERONIKA_REPORTS_DIR", str(sibling_reports))

    client = TestClient(api_main.app)
    listed = client.get("/api/backtest_live_validation/live_sessions")
    assert listed.status_code == 200
    payload = listed.json()
    assert str(top_live_reports) in payload["roots"]
    assert payload["sessions"][0]["path"] == str(session)
    assert payload["sessions"][0]["exchange"] == "bingx"
