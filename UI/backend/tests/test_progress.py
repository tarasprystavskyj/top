import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.append(str(BACKEND_DIR))

import api_main


def test_estimate_symbol_count_from_override_file(monkeypatch, tmp_path):
    universe_dir = tmp_path / "universe"
    universe_dir.mkdir()
    sample = universe_dir / "sample.txt"
    sample.write_text("BTCUSDT\n# comment\nETHUSDT\n\n")
    monkeypatch.setattr(api_main, "UNIVERSE_DIR", str(universe_dir))
    monkeypatch.setattr(api_main, "BT_ROOT", str(tmp_path))
    monkeypatch.setattr(api_main, "REPO_ROOT", str(tmp_path))
    meta = {"override": {"symbols_file": "universe/sample.txt"}}
    assert api_main._estimate_symbol_count(meta) == 2


def test_estimate_symbol_count_from_config(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "sample.yaml"
    cfg_path.write_text("symbols:\n  - BTCUSDT\n  - ETHUSDT\n  - SOLUSDT\n")
    monkeypatch.setattr(api_main, "CONFIG_DIRS", [str(cfg_dir)])
    meta = {"cfg_name": "sample.yaml"}
    assert api_main._estimate_symbol_count(meta) == 3


def test_expected_duration_and_profile_update(monkeypatch):
    monkeypatch.setattr(api_main, "perf_stats", {"per_100_per_symbol": 0.5, "samples": 2})
    monkeypatch.setattr(api_main, "_save_perf_stats", lambda stats: None)
    expected = api_main._estimate_expected_duration(500, 2)
    assert abs(expected - 5.0) < 1e-6
    api_main._update_perf_profile(4.0, 200, 1)
    assert api_main.perf_stats["samples"] == 3
    assert abs(api_main.perf_stats["per_100_per_symbol"] - 1.0) < 1e-6

