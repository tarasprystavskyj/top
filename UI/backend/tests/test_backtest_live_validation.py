import sys
import json
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.append(str(BACKEND_DIR))

import api_main


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
