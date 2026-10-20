"""CLI and GUI share one core: equivalence, progress hand-off, and duplicate safety."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from agent_state_workshop.service import Workshop
from conftest import ok


fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_state_workshop.gui.server import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as c:
        c.root = tmp_path
        yield c


_counter = iter(range(10_000))


def gui_op(client, kind, params, op_id=None):
    op_id = op_id or f"test-op-{next(_counter):05d}"
    r = client.post("/api/ops", json={"op_id": op_id, "kind": kind, "params": params})
    assert r.status_code in (200, 202), r.text
    for _ in range(600):
        rec = client.get(f"/api/ops/{op_id}").json()
        if rec["status"] not in ("queued", "running"):
            assert rec["status"] == "done", rec
            return rec["result"]
        time.sleep(0.05)
    raise AssertionError("operation did not finish")


def _domain(run_dir: Path) -> dict:
    """Domain results with generated IDs and timings removed."""
    read = lambda n: json.loads((run_dir / n).read_text()) if (run_dir / n).exists() else None  # noqa: E731
    receipt = read("receipt.json")
    state = read("state.json")
    state.pop("summary_writer_pid", None)  # a process ID, generated per checkpoint
    return {
        "state": state,
        "context": read("context.json"),
        "proposal": read("proposal.json"),
        "gate": read("gate.json"),
        "receipt": {k: receipt[k] for k in ("commit", "environment", "reused_existing")} if receipt else None,
        "span_names": sorted(json.loads(line)["name"] for line in (run_dir / "spans.jsonl").read_text().splitlines()),
        "artifacts": sorted(json.loads((run_dir / "manifest.json").read_text())["artifacts"]),
    }


def test_cli_and_gui_produce_equivalent_domain_results(tmp_path, client):
    cli = Workshop(tmp_path / "cli-ws")
    plan = [("prepare", None), ("run", "baseline"), ("append", "tests-pass-b2"), ("append", "approve-b2-staging"),
            ("run", "reference")]
    cli_runs, gui_runs = [], []
    for kind, arg in plan:
        if kind == "prepare":
            ok(cli.execute("prepare", {"db": ".workshop/x.sqlite"}))
            gui_op(client, "prepare", {"session": "x"})
        elif kind == "append":
            ok(cli.execute("append", {"db": ".workshop/x.sqlite", "fixture": arg}))
            gui_op(client, "append", {"session": "x", "fixture": arg})
        else:
            cli_runs.append(Path(ok(cli.execute("run", {"db": ".workshop/x.sqlite", "strategy": arg}))["run_dir"]))
            gui_runs.append(client.root / gui_op(client, "run", {"session": "x", "strategy": arg})["run_dir"]
                            .split(str(client.root) + "/")[-1])
    for c, g in zip(cli_runs, gui_runs, strict=True):
        assert _domain(c) == _domain(g)

    ok(cli.execute("export", {"before": str(cli_runs[0]), "after": str(cli_runs[1])}))
    gui_op(client, "export", {"before": str(gui_runs[0].relative_to(client.root)),
                              "after": str(gui_runs[1].relative_to(client.root))})
    cli_rows = json.loads((cli.root / "submissions/evidence.json").read_text())["comparison"]["rows"]
    gui_rows = json.loads((client.root / "submissions/evidence.json").read_text())["comparison"]["rows"]
    strip = lambda rows: [{k: _no_ids(v) for k, v in r.items()} for r in rows]  # noqa: E731
    assert strip(cli_rows) == strip(gui_rows)

    cli_check = ok(cli.execute("check", {"strategy": "reference"}))
    gui_check = gui_op(client, "check", {"strategy": "reference"})
    assert cli_check == gui_check


def _no_ids(value):
    import re

    return re.sub(r"(span|receipt) [0-9a-z-]+", r"\1 <id>", value) if isinstance(value, str) else value


def test_switching_interfaces_preserves_progress(client):
    cli = Workshop(client.root)
    ok(cli.execute("prepare", {"db": ".workshop/shared.sqlite"}))
    first = ok(cli.execute("step", {"db": ".workshop/shared.sqlite", "strategy": "reference", "until_done": False}))
    assert first["next_stage"] == "memory.project"
    status = client.get("/api/sessions/shared").json()
    assert status["active_run"]["next_stage"] == "memory.project"
    gui_op(client, "step", {"session": "shared"})
    done = gui_op(client, "step", {"session": "shared", "until_done": True})
    assert done["status"] == "completed" and done["run_id"] == first["run_id"]
    stages = json.loads((Path(done["run_dir"]) / "progress.json").read_text())["stages"]
    assert [s["stage"] for s in stages][:2] == ["state.load", "memory.project"]
    runs = client.get("/api/runs").json()
    assert any(r["run_id"] == first["run_id"] for r in runs)
    ops = {o["interface"] for o in cli.ops.recent(20)}
    assert ops == {"cli", "gui"}


def test_retry_and_double_click_do_not_duplicate(client):
    gui_op(client, "prepare", {"session": "dup"})
    body = {"op_id": "double-click-000001", "kind": "run", "params": {"session": "dup", "strategy": "reference"}}
    responses = []
    threads = [threading.Thread(target=lambda: responses.append(client.post("/api/ops", json=body))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.status_code in (200, 202) for r in responses)
    gui_op(client, "run", body["params"], op_id=body["op_id"])  # a later retry of the same action
    runs = [r for r in client.get("/api/runs").json() if r["session"] == "dup"]
    assert len(runs) == 1
    st = client.get("/api/sessions/dup").json()
    assert len(st["runs"]) == 1


def test_reads_are_read_only(client):
    gui_op(client, "prepare", {"session": "ro"})
    gui_op(client, "run", {"session": "ro", "strategy": "baseline"})
    db = client.root / ".workshop/ro.sqlite"
    before = (db.stat().st_mtime_ns, len(list((client.root / "runs").iterdir())), len(client.get("/api/ops").json()))
    run_dir = client.get("/api/runs").json()[0]["run_dir"]
    for url in ("/", "/api/config", "/api/sessions", "/api/sessions/ro", "/api/sessions/ro/events", "/api/runs",
                f"/api/run?path={run_dir}", f"/api/compare?before={run_dir}&after={run_dir}", "/api/ops"):
        assert client.get(url).status_code == 200, url
    after = (db.stat().st_mtime_ns, len(list((client.root / "runs").iterdir())), len(client.get("/api/ops").json()))
    assert before == after


def test_gui_security_guards(client):
    assert client.get("/api/config", headers={"Host": "evil.example"}).status_code == 400
    assert client.post("/api/ops", content="op_id=xxxxxxxx", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert client.get("/api/run?path=../../etc").status_code == 404
    assert client.post("/api/ops", json={"op_id": "bad-session-1", "kind": "prepare",
                                         "params": {"session": "../x"}}).status_code == 400
    cfg = client.get("/api/config").json()
    assert "key" not in json.dumps(cfg).lower().replace("key_configured", "")
