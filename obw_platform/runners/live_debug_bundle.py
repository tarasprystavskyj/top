from __future__ import annotations

import hashlib
import io
import json
import platform
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Iterable, Optional


def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL;")
    return con


def ensure_live_debug_bundle_db(db_path: str) -> None:
    con = _connect(db_path)
    cur = con.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS debug_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            ts_ms INTEGER,
            level TEXT,
            event_type TEXT,
            payload_json TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS stdio_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            ts_ms INTEGER,
            stream_name TEXT,
            message TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS run_meta(
            run_id TEXT PRIMARY KEY,
            started_ts_ms INTEGER,
            finished_ts_ms INTEGER,
            status TEXT,
            argv_json TEXT,
            env_json TEXT,
            platform_json TEXT,
            extra_json TEXT,
            error_text TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS code_snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            label TEXT,
            path TEXT,
            sha256 TEXT,
            text_content TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS attached_artifacts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            name TEXT,
            path TEXT,
            sha256 TEXT,
            blob BLOB,
            note TEXT
        )"""
    )
    con.commit()
    con.close()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def debug_event(db_path: str, run_id: str, event_type: str, payload=None, level: str = "INFO") -> None:
    try:
        ensure_live_debug_bundle_db(db_path)
        con = _connect(db_path)
        con.execute(
            "INSERT INTO debug_events(run_id, ts_ms, level, event_type, payload_json) VALUES (?,?,?,?,?)",
            (run_id, int(time.time() * 1000), level, str(event_type), json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)),
        )
        con.commit()
        con.close()
    except Exception:
        pass


def record_run_meta(db_path: str, run_id: str, argv=None, env=None, extra=None) -> None:
    ensure_live_debug_bundle_db(db_path)
    platform_json = {
        "python": sys.version,
        "platform": platform.platform(),
        "executable": sys.executable,
    }
    con = _connect(db_path)
    con.execute(
        "INSERT OR REPLACE INTO run_meta(run_id, started_ts_ms, finished_ts_ms, status, argv_json, env_json, platform_json, extra_json, error_text) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            int(time.time() * 1000),
            None,
            "running",
            json.dumps(list(argv or sys.argv), ensure_ascii=False),
            json.dumps(dict(env or {}), ensure_ascii=False, sort_keys=True),
            json.dumps(platform_json, ensure_ascii=False, sort_keys=True),
            json.dumps(extra or {}, ensure_ascii=False, sort_keys=True),
            "",
        ),
    )
    con.commit()
    con.close()


def finalize_run_meta(db_path: str, run_id: str, status: str = "ok", error_text: str = "") -> None:
    ensure_live_debug_bundle_db(db_path)
    con = _connect(db_path)
    con.execute(
        "UPDATE run_meta SET finished_ts_ms=?, status=?, error_text=? WHERE run_id=?",
        (int(time.time() * 1000), str(status), str(error_text or ""), run_id),
    )
    con.commit()
    con.close()


def snapshot_file(db_path: str, run_id: str, path: str, label: str = "") -> None:
    p = Path(path)
    if not p.exists() or not p.is_file():
        return
    text = p.read_text(encoding="utf-8", errors="replace")
    sha = _sha256_bytes(text.encode("utf-8", errors="replace"))
    ensure_live_debug_bundle_db(db_path)
    con = _connect(db_path)
    con.execute(
        "INSERT INTO code_snapshots(run_id, label, path, sha256, text_content) VALUES (?,?,?,?,?)",
        (run_id, label or p.name, str(p), sha, text),
    )
    con.commit()
    con.close()


def snapshot_files(db_path: str, run_id: str, paths: Iterable[str]) -> None:
    for p in paths:
        try:
            snapshot_file(db_path, run_id, str(p))
        except Exception:
            pass


def attach_file(db_path: str, run_id: str, path: str, name: Optional[str] = None, note: str = "", max_bytes: int = 2_000_000) -> None:
    p = Path(path)
    if not p.exists() or not p.is_file():
        return
    data = p.read_bytes()
    if len(data) > max_bytes:
        note = (note + " " if note else "") + f"truncated_to_{max_bytes}"
        data = data[:max_bytes]
    ensure_live_debug_bundle_db(db_path)
    con = _connect(db_path)
    con.execute(
        "INSERT INTO attached_artifacts(run_id, name, path, sha256, blob, note) VALUES (?,?,?,?,?,?)",
        (run_id, name or p.name, str(p), _sha256_bytes(data), sqlite3.Binary(data), note),
    )
    con.commit()
    con.close()


class _TeeSqliteWriter(io.TextIOBase):
    def __init__(self, db_path: str, run_id: str, stream_name: str, wrapped):
        self.db_path = db_path
        self.run_id = run_id
        self.stream_name = stream_name
        self.wrapped = wrapped
        self._lock = threading.Lock()

    def write(self, s):
        if not s:
            return 0
        with self._lock:
            try:
                self.wrapped.write(s)
                self.wrapped.flush()
            except Exception:
                pass
            try:
                ensure_live_debug_bundle_db(self.db_path)
                con = _connect(self.db_path)
                con.execute(
                    "INSERT INTO stdio_log(run_id, ts_ms, stream_name, message) VALUES (?,?,?,?)",
                    (self.run_id, int(time.time() * 1000), self.stream_name, str(s)),
                )
                con.commit()
                con.close()
            except Exception:
                pass
        return len(s)

    def flush(self):
        try:
            self.wrapped.flush()
        except Exception:
            pass


def install_stdio_capture(db_path: str, run_id: str):
    ensure_live_debug_bundle_db(db_path)
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = _TeeSqliteWriter(db_path, run_id, "stdout", old_out)
    sys.stderr = _TeeSqliteWriter(db_path, run_id, "stderr", old_err)

    def restore():
        sys.stdout = old_out
        sys.stderr = old_err

    return restore


def finalize_bundle(db_path: str, run_id: str, status: str = "ok", error_text: str = "", results_dir: str = "", extra_files: Optional[Iterable[str]] = None):
    if results_dir:
        for name in ("live_positions.json", "session.sqlite", "combined_cache_session.db"):
            p = Path(results_dir) / name
            if p.exists() and p.is_file():
                try:
                    attach_file(db_path, run_id, str(p), name=name)
                except Exception:
                    pass
    for p in (extra_files or []):
        try:
            attach_file(db_path, run_id, str(p))
        except Exception:
            pass
    finalize_run_meta(db_path, run_id, status=status, error_text=error_text)


def exception_text() -> str:
    return traceback.format_exc()
