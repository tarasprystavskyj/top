import pytest

try:  # pragma: no cover - import error branch is handled by skip
    from fastapi.testclient import TestClient
    from v.api.main import app, CONFIG_DIR
except Exception as exc:  # pragma: no cover
    pytest.skip(f"fastapi not available: {exc}", allow_module_level=True)

client = TestClient(app)


def test_list_configs_contains_example():
    resp = client.get("/api/configs")
    assert resp.status_code == 200
    data = resp.json()
    assert any(item["name"] == "cfg_avaai_t5m5000.yaml" for item in data)


def test_put_and_get_config():
    name = "tmp_test_config.yaml"
    yaml_text = (
        "strategy_params:\n  top_n: 8\nportfolio:\n  position_notional: 1\nuniverse_file: 'u.txt'\n"
    )
    resp = client.put(f"/api/configs/{name}", json={"yaml_text": yaml_text})
    assert resp.status_code == 200
    assert (CONFIG_DIR / name).exists()

    resp = client.get(f"/api/configs/{name}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["parsed"]["portfolio"]["position_notional"] == 1

    (CONFIG_DIR / name).unlink()
