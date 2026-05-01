from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def load_dotenv_file(path: str | Path = ".env", *, override: bool = False) -> int:
    """Tiny .env loader.

    Supports lines like:
      KEY=value
      KEY="value"
      export KEY=value

    Returns number of loaded variables.
    """
    p = Path(path)
    if not p.exists():
        return 0

    count = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue

        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = val
            count += 1
    return count


def require_env(name: str, *, min_len: int = 1) -> str:
    val = os.getenv(name, "")
    if len(val) < int(min_len):
        raise RuntimeError(f"Required env var is missing or too short: {name}")
    return val


def optional_env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def redact_secret(value: str, *, keep_start: int = 4, keep_end: int = 4) -> str:
    s = str(value or "")
    if not s:
        return ""
    if len(s) <= keep_start + keep_end:
        return "*" * len(s)
    return s[:keep_start] + "***" + s[-keep_end:]


def redact_mapping(data: dict, secret_keys=("key", "secret", "token", "password", "private")) -> dict:
    out = {}
    for k, v in data.items():
        kl = str(k).lower()
        if any(x in kl for x in secret_keys):
            out[k] = redact_secret(str(v))
        else:
            out[k] = v
    return out
