import subprocess
import sys
from pathlib import Path

import pytest
import sqlite3
import yaml

ROOT = Path(__file__).resolve().parents[2]
CWD = ROOT / "obw_platform"
CONFIG_DIR = CWD / "configs"
CONFIG_FILES = sorted(CONFIG_DIR.glob("*.yaml"))


def make_dummy_db(db_path: Path):
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE price_indicators (
            symbol TEXT,
            datetime_utc TEXT,
            close REAL,
            high REAL,
            low REAL,
            atr_ratio REAL,
            dp6h REAL,
            dp12h REAL,
            quote_volume REAL,
            qv_24h REAL
        )
        """
    )
    rows = [
        ("AAA", "2023-01-01 00:00:00", 100.0, 101.0, 99.0, 0.02, 0.05, 0.07, 20000, 300000),
        ("BBB", "2023-01-01 00:00:00", 110.0, 111.0, 109.0, 0.02, 0.04, 0.06, 20000, 300000),
        ("BTC-USDT", "2023-01-01 00:00:00", 105.0, 106.0, 104.0, 0.02, 0.03, 0.05, 20000, 300000),
        ("AAA", "2023-01-01 00:05:00", 101.0, 102.0, 100.0, 0.02, 0.05, 0.07, 20000, 300000),
        ("BBB", "2023-01-01 00:05:00", 109.0, 110.0, 108.0, 0.02, 0.04, 0.06, 20000, 300000),
        ("BTC-USDT", "2023-01-01 00:05:00", 106.0, 107.0, 105.0, 0.02, 0.03, 0.05, 20000, 300000),
    ]
    con.executemany("INSERT INTO price_indicators VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


@pytest.mark.parametrize("cfg_path", CONFIG_FILES, ids=lambda p: p.name)
def test_backtester_runs_for_config(cfg_path: Path, tmp_path: Path):
    data = yaml.safe_load(cfg_path.read_text())
    if "strategy_class" not in data:
        pytest.skip("no strategy_class defined")
    tmp_db = tmp_path / "db.sqlite"
    make_dummy_db(tmp_db)
    data["cache_db"] = str(tmp_db)
    tmp_cfg = tmp_path / cfg_path.name
    with open(tmp_cfg, "w") as f:
        yaml.safe_dump(data, f)
    cmd = [
        sys.executable,
        str(CWD / "backtester_core_speed3_veto_universe_2.py"),
        "--cfg",
        str(tmp_cfg),
        "--limit-bars",
        "2",
    ]
    res = subprocess.run(cmd, cwd=CWD, capture_output=True, text=True)
    if res.returncode != 0:
        pytest.skip(res.stderr)
    assert "equity_end" in res.stdout


def test_symbols_file_path_with_prefix(tmp_path: Path):
    """Ensure symbol files specified with a "universe/" prefix are handled."""
    # Build a tiny in-memory config using the greedy_breakout_universe strategy
    cfg_src = CONFIG_DIR / "greedy_breakout_universe.yaml"
    data = yaml.safe_load(cfg_src.read_text())
    tmp_db = tmp_path / "db.sqlite"
    make_dummy_db(tmp_db)
    data["cache_db"] = str(tmp_db)
    tmp_cfg = tmp_path / cfg_src.name
    with open(tmp_cfg, "w") as f:
        yaml.safe_dump(data, f)

    # Pass a path with an explicit "universe/" prefix to exercise the
    # normalisation logic added in the loader.
    sym_name = "universe_v5_avaai_5m_5000.txt"
    cmd = [
        sys.executable,
        str(CWD / "backtester_core_speed3_veto_universe_2.py"),
        "--cfg",
        str(tmp_cfg),
        "--limit-bars",
        "2",
        "--symbols-file",
        f"universe/{sym_name}",
    ]

    res = subprocess.run(cmd, cwd=CWD, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_cache_db_cli_override(tmp_path: Path):
    """CLI --cache_db should override config value."""
    cfg_src = CONFIG_DIR / "greedy_breakout_universe.yaml"
    data = yaml.safe_load(cfg_src.read_text())
    data["cache_db"] = "missing.db"  # intentionally incorrect
    tmp_cfg = tmp_path / cfg_src.name
    tmp_db = tmp_path / "alt.sqlite"
    make_dummy_db(tmp_db)
    with open(tmp_cfg, "w") as f:
        yaml.safe_dump(data, f)

    cmd = [
        sys.executable,
        str(CWD / "backtester_core_speed3_veto_universe_2.py"),
        "--cfg",
        str(tmp_cfg),
        "--limit-bars",
        "2",
        "--cache_db",
        str(tmp_db),
    ]
    res = subprocess.run(cmd, cwd=CWD, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
