"""Trace structure, local export, and OTLP behaviour."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agent_state_workshop.runner import STAGES
from agent_state_workshop.telemetry import read_spans
from conftest import ok

REQUIRED_CHILDREN = {"state.load", "memory.project", "context.build", "planner.propose", "policy.validate", "state.save"}


def _spans(run_dir: Path) -> list[dict]:
    return read_spans(Path(run_dir) / "spans.jsonl")


def _check_structure(spans: list[dict]) -> dict:
    roots = [s for s in spans if s["parent_span_id"] is None]
    assert len(roots) == 1 and roots[0]["name"] == "agent.resume"
    root = roots[0]
    assert all(len(s["trace_id"]) == 32 and len(s["span_id"]) == 16 for s in spans)
    assert len({s["trace_id"] for s in spans}) == 1
    children = {s["name"] for s in spans if s["parent_span_id"] == root["span_id"]}
    assert REQUIRED_CHILDREN <= children
    for s in spans:
        assert s["start_time_unix_nano"] <= s["end_time_unix_nano"]
        assert s["resource"]["service.name"] == "agent-state-workshop"
    return root


def test_blocked_run_has_no_deploy_span_or_receipt(prepared):
    r = ok(prepared.execute("run", {"db": ".workshop/s.sqlite", "strategy": "baseline", "out": "runs/b"}))
    spans = _spans(prepared.root / "runs/b")
    root = _check_structure(spans)
    assert "tool.deploy" not in {s["name"] for s in spans}
    assert not (prepared.root / "runs/b/receipt.json").exists()
    gate = next(s for s in spans if s["name"] == "policy.validate")
    assert any(e["name"] == "proposal.rejected" for e in gate["events"])
    assert gate["status"]["code"] == "UNSET"  # a rejection is a domain outcome, not a span error
    assert root["attributes"]["workshop.planner_mode"] == "simulated"
    assert root["attributes"]["workshop.outcome.executed_action"] == "none"
    assert not any(s["name"] == "model.generate" for s in spans)  # no fictitious model call
    assert not any(k.startswith("workshop.llm.") and "token" in k for s in spans for k in s["attributes"])
    assert r["trace_id"] == root["trace_id"]


def test_allowed_deploy_has_span_and_receipt_and_evidence_resolves(prepared):
    db = ".workshop/s.sqlite"
    ok(prepared.execute("append", {"db": db, "fixture": "tests-pass-b2"}))
    ok(prepared.execute("append", {"db": db, "fixture": "approve-b2-staging"}))
    ok(prepared.execute("run", {"db": db, "strategy": "reference", "out": "runs/ok"}))
    run = prepared.root / "runs/ok"
    spans = _spans(run)
    _check_structure(spans)
    deploy = [s for s in spans if s["name"] == "tool.deploy"]
    assert len(deploy) == 1 and (run / "receipt.json").exists()
    assert deploy[0]["attributes"]["workshop.deploy.receipt_id"] == json.loads((run / "receipt.json").read_text())["receipt_id"]
    events = {e["event_id"] for e in json.loads((run / "events.json").read_text())}
    ctx = next(s for s in spans if s["name"] == "context.build")
    selected = next(e for e in ctx["events"] if e["name"] == "evidence.selected")
    assert set(selected["attributes"]["workshop.evidence.event_ids"]) <= events
    assert ctx["attributes"]["workshop.annotation.complete"] is True
    manifest = json.loads((run / "manifest.json").read_text())
    for name, info in manifest["artifacts"].items():
        assert (run / info["path"]).exists(), name


def test_runtime_failure_is_recorded_as_span_error(prepared, monkeypatch):
    from agent_state_workshop import runner

    def boom(run, span):
        raise RuntimeError("simulated crash")

    monkeypatch.setitem(runner.HANDLERS, "memory.project", boom)
    r = ok(prepared.execute("run", {"db": ".workshop/s.sqlite", "strategy": "reference", "out": "runs/crash"}))
    assert r["status"] == "failed"
    spans = _spans(prepared.root / "runs/crash")
    mp = next(s for s in spans if s["name"] == "memory.project")
    assert mp["status"]["code"] == "ERROR" and any(e["name"] == "exception" for e in mp["events"])
    assert next(s for s in spans if s["name"] == "agent.resume")["status"]["code"] == "ERROR"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_unreachable_otlp_keeps_local_trace_and_decision(prepared):
    start = time.monotonic()
    r = ok(prepared.execute("run", {"db": ".workshop/s.sqlite", "strategy": "baseline", "out": "runs/otlp-down",
                                    "otlp_endpoint": f"http://127.0.0.1:{_free_port()}/v1/traces"}))
    assert time.monotonic() - start < 30
    assert r["proposal"]["action"] == "deploy" and r["gate"]["outcome"] == "blocked"
    _check_structure(_spans(prepared.root / "runs/otlp-down"))
    manifest = json.loads((prepared.root / "runs/otlp-down/manifest.json").read_text())
    assert manifest["otlp"]["errors"]


def test_otlp_receives_the_same_trace_ids(prepared):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    received: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ok(prepared.execute("run", {"db": ".workshop/s.sqlite", "strategy": "reference", "out": "runs/otlp",
                                    "otlp_endpoint": f"http://127.0.0.1:{server.server_port}/v1/traces"}))
    finally:
        server.shutdown()
    remote = set()
    for body in received:
        req = ExportTraceServiceRequest.FromString(body)
        for rs in req.resource_spans:
            for ss in rs.scope_spans:
                remote |= {(sp.trace_id.hex(), sp.span_id.hex()) for sp in ss.spans}
    local = {(s["trace_id"], s["span_id"]) for s in _spans(prepared.root / "runs/otlp")}
    assert local and local == remote


def test_stage_names_match_spec():
    assert STAGES == ("state.load", "memory.project", "context.build", "planner.propose",
                      "policy.validate", "tool.deploy", "state.save")
