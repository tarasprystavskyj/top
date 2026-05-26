import sys
import json
import sqlite3
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.append(str(BACKEND_DIR))

import api_main


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
        },
    )
    _write_json(session / "active_status.json", {"open_legs": 1, "filled_orders": 2})
    (session / "ACTIVE_STATUS_PATH.txt").write_text("active_status.json\n", encoding="utf-8")
    (session / "live_stdout_20260525.log").write_text("hype canary started\n", encoding="utf-8")

    con = sqlite3.connect(session / "session.sqlite")
    try:
        con.execute(
            "CREATE TABLE open_positions (symbol TEXT, status TEXT, qty REAL, entry_fill REAL, ts_utc TEXT)"
        )
        con.execute(
            "INSERT INTO open_positions VALUES ('HYPE-USDT', 'OPEN', 1.5, 25.0, '2026-05-25T00:01:00Z')"
        )
        con.execute(
            "CREATE TABLE orders (id INTEGER, symbol TEXT, status TEXT, price REAL, ts_utc TEXT)"
        )
        con.execute(
            "INSERT INTO orders VALUES (1, 'HYPE-USDT', 'filled', 25.1, '2026-05-25T00:02:00Z')"
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
    assert status.json()["status"]["live_symbol"] == "HYPE-USDT"

    positions = client.get(
        "/api/backtest_live_validation/live_session/table",
        params={"path": str(session), "kind": "open_positions"},
    )
    assert positions.status_code == 200
    assert positions.json()["rows"][0]["source"] == "RUN_STATUS.json"


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
