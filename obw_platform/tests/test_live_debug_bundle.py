from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from live_debug_bundle import (
    ensure_live_debug_bundle_db,
    debug_event,
    record_run_meta,
    finalize_run_meta,
    snapshot_file,
    attach_file,
    install_stdio_capture,
    finalize_bundle,
)


def _count_rows(db_path: str, table: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0] or 0)
    finally:
        con.close()


def test_ensure_live_debug_bundle_db_creates_tables(tmp_path: Path):
    db_path = str(tmp_path / "session.sqlite")
    ensure_live_debug_bundle_db(db_path)

    con = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        con.close()

    assert "debug_events" in tables
    assert "stdio_log" in tables
    assert "run_meta" in tables
    assert "code_snapshots" in tables
    assert "attached_artifacts" in tables



def test_debug_event_inserts_row(tmp_path: Path):
    db_path = str(tmp_path / "session.sqlite")
    ensure_live_debug_bundle_db(db_path)

    debug_event(db_path, "run-1", "bar", {"i": 7}, level="DEBUG")

    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT run_id, level, event_type, payload_json FROM debug_events"
        ).fetchone()
    finally:
        con.close()

    assert row[0] == "run-1"
    assert row[1] == "DEBUG"
    assert row[2] == "bar"
    assert '"i": 7' in row[3]



def test_record_and_finalize_run_meta(tmp_path: Path):
    db_path = str(tmp_path / "session.sqlite")
    ensure_live_debug_bundle_db(db_path)

    record_run_meta(db_path, "run-2", argv=["python", "x.py"], env={"A": "1"}, extra={"mode": "live"})
    finalize_run_meta(db_path, "run-2", status="error", error_text="boom")

    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT status, argv_json, env_json, extra_json, error_text, finished_ts_ms FROM run_meta WHERE run_id=?",
            ("run-2",),
        ).fetchone()
    finally:
        con.close()

    assert row[0] == "error"
    assert 'x.py' in row[1]
    assert '"A": "1"' in row[2]
    assert '"mode": "live"' in row[3]
    assert row[4] == "boom"
    assert row[5] is not None



def test_snapshot_file_stores_source_text(tmp_path: Path):
    db_path = str(tmp_path / "session.sqlite")
    src = tmp_path / "sample.py"
    src.write_text("print('ok')\n", encoding="utf-8")

    snapshot_file(db_path, "run-3", str(src), label="sample")

    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT label, path, sha256, text_content FROM code_snapshots WHERE run_id=?",
            ("run-3",),
        ).fetchone()
    finally:
        con.close()

    assert row[0] == "sample"
    assert row[1].endswith("sample.py")
    assert isinstance(row[2], str) and len(row[2]) == 64
    assert "print('ok')" in row[3]



def test_attach_file_stores_blob(tmp_path: Path):
    db_path = str(tmp_path / "session.sqlite")
    art = tmp_path / "artifact.txt"
    art.write_text("hello bundle", encoding="utf-8")

    attach_file(db_path, "run-4", str(art), name="artifact.txt", note="unit")

    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT name, note, sha256, length(blob) FROM attached_artifacts WHERE run_id=?",
            ("run-4",),
        ).fetchone()
    finally:
        con.close()

    assert row[0] == "artifact.txt"
    assert row[1] == "unit"
    assert isinstance(row[2], str) and len(row[2]) == 64
    assert row[3] > 0



def test_install_stdio_capture_writes_stdout_and_stderr(tmp_path: Path, capsys):
    db_path = str(tmp_path / "session.sqlite")
    restore = install_stdio_capture(db_path, "run-5")
    try:
        print("hello stdout")
        print("hello stderr", file=sys.stderr)
    finally:
        restore()

    captured = capsys.readouterr()
    assert "hello stdout" in captured.out
    assert "hello stderr" in captured.err

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT stream_name, message FROM stdio_log WHERE run_id=? ORDER BY id ASC",
            ("run-5",),
        ).fetchall()
    finally:
        con.close()

    joined = "".join(r[1] for r in rows)
    assert any(r[0] == "stdout" for r in rows)
    assert any(r[0] == "stderr" for r in rows)
    assert "hello stdout" in joined
    assert "hello stderr" in joined



def test_finalize_bundle_attaches_extra_files(tmp_path: Path):
    db_path = str(tmp_path / "session.sqlite")
    record_run_meta(db_path, "run-6")
    extra = tmp_path / "extra.log"
    extra.write_text("bundle extra", encoding="utf-8")

    finalize_bundle(db_path, "run-6", status="ok", results_dir="", extra_files=[str(extra)])

    assert _count_rows(db_path, "attached_artifacts") == 1

    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT status, error_text FROM run_meta WHERE run_id=?",
            ("run-6",),
        ).fetchone()
    finally:
        con.close()

    assert row[0] == "ok"
    assert row[1] == ""
