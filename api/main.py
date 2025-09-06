"""FastAPI application exposing basic configuration management endpoints.

This is a lightweight backend skeleton used to experiment with the
trading/backtesting tooling.  For now it only exposes endpoints for
listing, retrieving and updating YAML configuration files that are
located in ``obw_platform/configs``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import yaml
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import jsonschema

from .schemas import BREAKOUT_AVAAI_FULL_5M_CONFIG_SCHEMA

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = BASE_DIR / "obw_platform" / "configs"

app = FastAPI(title="Backtest Web API")


def _ensure_config_dir() -> Path:
    if not CONFIG_DIR.exists():
        raise HTTPException(500, f"Config directory not found: {CONFIG_DIR}")
    return CONFIG_DIR


def _resolve_config(name: str, *, missing_ok: bool = False) -> Path:
    config_dir = _ensure_config_dir()
    path = (config_dir / name).resolve()
    if CONFIG_DIR not in path.parents:
        raise HTTPException(400, "Invalid config path")
    if not path.exists():
        if missing_ok:
            return path
        raise HTTPException(404, "Config not found")
    if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
        raise HTTPException(404, "Config not found")
    return path


class ConfigText(BaseModel):
    yaml_text: str


@app.get("/api/configs")
def list_configs(dir: str = Query("configs", alias="dir")) -> List[Dict[str, Any]]:
    """Return a list of available YAML configurations.

    Only the ``configs`` directory is supported at the moment; this is
    validated via the ``dir`` query parameter to keep the endpoint
    compatible with the intended API.
    """

    if dir != "configs":
        raise HTTPException(400, "unknown config dir")
    config_dir = _ensure_config_dir()
    items: List[Dict[str, Any]] = []
    for file in sorted(config_dir.glob("*.yaml")):
        stat = file.stat()
        items.append(
            {
                "name": file.name,
                "path": str(file),
                "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
        )
    return items


@app.get("/api/configs/{name}")
def get_config(name: str) -> Dict[str, Any]:
    """Return the raw YAML text and parsed representation of a config."""

    path = _resolve_config(name)
    text = path.read_text()
    parsed = yaml.safe_load(text) if text.strip() else {}
    return {
        "name": name,
        "yaml_text": text,
        "parsed": parsed,
        "schema": BREAKOUT_AVAAI_FULL_5M_CONFIG_SCHEMA,
    }


@app.put("/api/configs/{name}")
def put_config(name: str, payload: ConfigText) -> Dict[str, Any]:
    """Validate and store a configuration file."""

    try:
        data = yaml.safe_load(payload.yaml_text)
    except yaml.YAMLError as exc:  # pragma: no cover - error branch
        raise HTTPException(400, f"Invalid YAML: {exc}")

    try:
        jsonschema.validate(data, BREAKOUT_AVAAI_FULL_5M_CONFIG_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise HTTPException(400, f"Schema validation error: {exc.message}")

    path = _resolve_config(name, missing_ok=True)
    path.write_text(payload.yaml_text)
    return {"ok": True}
