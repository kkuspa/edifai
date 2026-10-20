"""Fixtures shared by the whole suite.

Rules every test follows:

* The working directory is a temporary directory, so a relative ``runs/`` or ``submissions/``
  path can never reach the repository.
* No test reads live credentials or the user's ``ant auth login`` profile, and no test opens a
  network connection.
* Behavior tests use the reference strategy so they do not depend on the student's file.
"""

from __future__ import annotations

import os
import socket
from types import SimpleNamespace

import pytest

from tests.helpers import REPO, run_session
from workshop.runner import load_strategy
from workshop.service import WorkshopService

CREDENTIAL_VARS = [
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_MODEL", "ANTHROPIC_EFFORT",
    "ANTHROPIC_SERVER_FALLBACKS", "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_REASONING_EFFORT", "OPENAI_TEMPERATURE",
    "LLM_PROVIDER", "LLM_MAX_TOKENS", "LLM_MAX_RETRIES",
]


def _repo_listing() -> dict[str, tuple[int, int]]:
    """Name, size, and mtime of everything under the repo's runs/, submissions/, and reference_runs/."""
    out: dict[str, tuple[int, int]] = {}
    for top in ("runs", "submissions", "reference_runs"):
        base = REPO / top
        if not base.exists():
            continue
        out[top] = (0, 0)
        for p in sorted(base.rglob("*")):
            st = p.stat()
            out[str(p.relative_to(REPO))] = (st.st_size, st.st_mtime_ns)
    return out


@pytest.fixture(scope="session", autouse=True)
def repo_stays_clean():
    """Fail the run if any test wrote into the repository's output directories."""
    before = _repo_listing()
    yield
    after = _repo_listing()
    assert after == before, f"tests changed the repository's output directories: {sorted(set(after) ^ set(before)) or 'file contents changed'}"


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture(autouse=True)
def no_live_credentials(monkeypatch):
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("workshop.planners.load_env", lambda: None)
    monkeypatch.setattr("workshop.planners._ant_profile_present", lambda: False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to open a connection fails the test. Child processes are not affected."""

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture
def service(tmp_path) -> WorkshopService:
    return WorkshopService(root=tmp_path / "runs")


@pytest.fixture
def reference():
    return load_strategy("reference")


@pytest.fixture
def subprocess_env():
    """Environment for child Python processes: the repo importable, no credentials, no user site."""
    env = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_VARS}
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


@pytest.fixture(scope="session")
def runs(tmp_path_factory) -> SimpleNamespace:
    """Finished simulated sessions, built once. Tests must treat them as read-only."""
    root = tmp_path_factory.mktemp("shared-runs")
    svc = WorkshopService(root=root)
    names = {
        "one_shot": run_session(svc, "one-shot", "one-shot", scenario="initial"),
        "one_shot_restated": run_session(svc, "one-shot-restated", "one-shot", restated_brief=True),
        "one_shot_unrestated": run_session(svc, "one-shot-unrestated", "one-shot"),
        "transcript": run_session(svc, "transcript", "transcript"),
        "state": run_session(svc, "state", "state"),
        "transcript_nc": run_session(svc, "transcript-nc", "transcript", scenario="no-compliant"),
        "state_nc": run_session(svc, "state-nc", "state", scenario="no-compliant"),
    }
    return SimpleNamespace(root=root, service=svc, **names)
