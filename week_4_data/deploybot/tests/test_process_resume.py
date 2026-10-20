"""The pause is a real process boundary, and runs survive being split across processes."""

from __future__ import annotations

import json
import os
import sqlite3

from conftest import run_cli


def test_prepare_uses_a_child_process_and_sqlite(tmp_path):
    out = run_cli(tmp_path, "prepare", "--db", ".workshop/base.sqlite").stdout
    assert "child process" in out
    conn = sqlite3.connect(tmp_path / ".workshop" / "base.sqlite")
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    memory_pid, memory_through = conn.execute("SELECT writer_pid, through_sequence FROM memories").fetchone()
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    assert count == 40 and memory_through == 20
    assert int(meta["checkpoint_pid"]) == memory_pid
    assert memory_pid != int(meta["resumer_pid"]) != os.getpid()

    again = run_cli(tmp_path, "prepare", "--db", ".workshop/base.sqlite", check=False)
    assert again.returncode != 0 and "--reset" in again.stderr
    run_cli(tmp_path, "prepare", "--db", ".workshop/base.sqlite", "--reset")


def test_cli_run_in_a_separate_process_reads_persisted_state(tmp_path):
    run_cli(tmp_path, "prepare", "--db", ".workshop/b.sqlite")
    out = run_cli(tmp_path, "run", "--db", ".workshop/b.sqlite", "--strategy", "baseline", "--out", "runs/before").stdout
    assert "deploy b2 -> staging" in out and "BLOCKED" in out and "nothing (no deployment)" in out
    reuse = run_cli(tmp_path, "run", "--db", ".workshop/b.sqlite", "--strategy", "baseline", "--out", "runs/before", check=False)
    assert reuse.returncode == 1 and "immutable" in reuse.stderr


def test_steps_across_processes_form_one_trace(tmp_path):
    run_cli(tmp_path, "prepare", "--db", ".workshop/s.sqlite")
    run_cli(tmp_path, "step", "--db", ".workshop/s.sqlite", "--strategy", "reference", "--out", "runs/stepped")
    run_cli(tmp_path, "step", "--db", ".workshop/s.sqlite")
    status = run_cli(tmp_path, "status", "--db", ".workshop/s.sqlite").stdout
    assert "next stage context.build" in status
    run_cli(tmp_path, "step", "--db", ".workshop/s.sqlite", "--until-done")
    spans = [json.loads(line) for line in (tmp_path / "runs/stepped/spans.jsonl").read_text().splitlines()]
    roots = [s for s in spans if s["parent_span_id"] is None]
    assert len(roots) == 1 and len({s["trace_id"] for s in spans}) == 1
    pids = {s["resource"]["process.pid"] for s in spans}
    assert len(pids) >= 3
    manifest = json.loads((tmp_path / "runs/stepped/manifest.json").read_text())
    assert manifest["configuration"]["execution_mode"] == "stepped"


def test_unfinished_student_exits_2_with_guidance(tmp_path):
    run_cli(tmp_path, "prepare", "--db", ".workshop/f.sqlite")
    proc = run_cli(tmp_path, "run", "--db", ".workshop/f.sqlite", "--strategy", "student", "--out", "runs/after", check=False)
    if proc.returncode == 0:  # a student has already completed exercises.py
        return
    assert proc.returncode == 2
    assert "Unfinished exercise" in proc.stderr and "runtime is fine" in proc.stderr
    # A failed run directory may be reused without --replace.
    proc2 = run_cli(tmp_path, "run", "--db", ".workshop/f.sqlite", "--strategy", "student", "--out", "runs/after", check=False)
    assert proc2.returncode == 2
