from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_table(path: str | Path, **kwargs) -> pd.DataFrame:
    """Read CSV or Parquet by suffix."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(p, **kwargs)
    if suffix in {".csv", ".txt"}:
        return pd.read_csv(p, **kwargs)
    raise ValueError(f"Unsupported table format: {p}")


def write_table(df: pd.DataFrame, path: str | Path, *, index: bool = False, **kwargs) -> Path:
    """Write CSV or Parquet by suffix."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    suffix = p.suffix.lower()
    if suffix == ".parquet":
        df.to_parquet(p, index=index, **kwargs)
    elif suffix in {".csv", ".txt"}:
        df.to_csv(p, index=index, **kwargs)
    else:
        raise ValueError(f"Unsupported table format: {p}")
    return p


def write_csv_and_parquet(df: pd.DataFrame, base_path_without_suffix: str | Path, *, index: bool = False) -> dict:
    base = Path(base_path_without_suffix)
    base.parent.mkdir(parents=True, exist_ok=True)

    out = {}
    csv_path = base.with_suffix(".csv")
    df.to_csv(csv_path, index=index)
    out["csv"] = str(csv_path)

    try:
        pq_path = base.with_suffix(".parquet")
        df.to_parquet(pq_path, index=index)
        out["parquet"] = str(pq_path)
    except Exception as e:
        out["parquet_error"] = str(e)

    return out


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    tmp.replace(p)
    return p
