from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_state_workshop.service import Workshop

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def ws(tmp_path: Path) -> Workshop:
    """A fresh workspace (its own .workshop/, runs/, submissions/)."""
    return Workshop(tmp_path, interface="cli")


def ok(record: dict) -> dict:
    assert record["status"] == "done", record.get("error")
    return record["result"]


@pytest.fixture
def prepared(ws: Workshop) -> Workshop:
    ok(ws.execute("prepare", {"db": ".workshop/s.sqlite"}))
    return ws


def run_cli(cwd: Path, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    full_env = {k: v for k, v in os.environ.items() if k != "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"}
    full_env.update(env or {})
    proc = subprocess.run([sys.executable, "-m", "agent_state_workshop.cli", *args], cwd=cwd,
                          capture_output=True, text=True, env=full_env, timeout=120)
    if check and proc.returncode != 0:
        raise AssertionError(f"workshop {' '.join(args)} exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc
