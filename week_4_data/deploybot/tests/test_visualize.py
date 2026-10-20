"""Offline HTML comparison, terminal views, and evidence export."""

from __future__ import annotations

import json
import re
import shutil

from agent_state_workshop import strategies
from agent_state_workshop.evidence import compare, compare_text, load_run
from agent_state_workshop.traceview import render_tree_text
from agent_state_workshop.visualize import render_comparison
from conftest import ok


def _two_runs(ws):
    ok(ws.execute("prepare", {"db": ".workshop/base.sqlite"}))
    ok(ws.execute("prepare", {"db": ".workshop/fixed.sqlite"}))
    ok(ws.execute("run", {"db": ".workshop/base.sqlite", "strategy": "baseline", "out": "runs/before"}))
    ok(ws.execute("run", {"db": ".workshop/fixed.sqlite", "strategy": "reference", "out": "runs/after"}))
    return ws.root / "runs/before", ws.root / "runs/after"


def test_html_is_offline_and_contains_real_traces(ws):
    before, after = _two_runs(ws)
    out = render_comparison(before, after, ws.root / "submissions/comparison.html")
    html = out.read_text()
    assert not re.search(r'<(script|link)[^>]+(src|href)=["\']?https?:', html), "no network resources"
    assert "fetch(" not in html
    for run in (before, after):
        progress = json.loads((run / "progress.json").read_text())
        assert progress["trace_id"] in html
        for line in (run / "spans.jsonl").read_text().splitlines():
            assert json.loads(line)["span_id"] in html


def test_html_escapes_artifact_content(ws):
    before, after = _two_runs(ws)
    ctx_path = after / "context.json"
    ctx = json.loads(ctx_path.read_text())
    ctx["user_request"] = '</script><img src=x onerror="alert(1)">'
    ctx_path.write_text(json.dumps(ctx))
    html = render_comparison(before, after, ws.root / "c.html").read_text()
    assert "<img src=x" not in html and "</script><img" not in html


def test_comparison_focuses_on_the_changed_data(ws):
    before, after = _two_runs(ws)
    result = compare(before, after)
    rows = {r["label"]: r for r in result["rows"]}
    assert "NO COMMIT SCOPE" in rows["Test evidence"]["before"]
    assert "evt-032" in rows["Test evidence"]["after"]
    assert rows["Proposed action"]["before"].startswith("deploy b2")
    assert rows["Proposed action"]["after"].startswith("report_test_failure b2")
    assert rows["Executed action"]["before"].startswith("none")
    assert any("no source event" in n for n in result["notices"])
    text = compare_text(result)
    assert "evidence.selected: evt-021, evt-032, evt-012" in text


def test_incomplete_annotation_is_flagged(ws, monkeypatch):
    real = strategies.load_strategy

    def lazy_annotation(name):
        s = real("reference")
        return strategies.StructuredStrategy(name, s.source_path, s.project_state, s.build_context,
                                             lambda span, ctx: None)

    monkeypatch.setattr("agent_state_workshop.runner.load_strategy", lazy_annotation)
    before, _ = _two_runs(ws)
    ok(ws.execute("prepare", {"db": ".workshop/lazy.sqlite"}))
    ok(ws.execute("run", {"db": ".workshop/lazy.sqlite", "strategy": "student", "out": "runs/lazy"}))
    result = compare(before, ws.root / "runs/lazy")
    assert result["after"]["annotation_complete"] is False
    assert any("annotation is incomplete" in n for n in result["notices"])
    html = render_comparison(before, ws.root / "runs/lazy", ws.root / "lazy.html").read_text()
    assert "Evidence annotation on context.build is incomplete" in html


def test_terminal_trace_tree(ws):
    _, after = _two_runs(ws)
    text = render_tree_text(load_run(after).spans, show_attributes=True)
    assert text.splitlines()[1].startswith("agent.resume")
    assert "|- context.build" in text and "evidence IDs: evt-021, evt-032, evt-012" in text
    assert "workshop.test.source_event_id = evt-032" in text


def test_export_bundle(ws):
    shutil.copy  # keep import used on all platforms
    before, after = _two_runs(ws)
    r = ok(ws.execute("export", {"before": str(before), "after": str(after)}))
    names = {p.split("/")[-1] for p in r["files"]}
    assert {"comparison.html", "evidence.json", "evidence.md", "findings.md"} <= names
    bundle = json.loads((ws.root / "submissions/evidence.json").read_text())
    assert [run["strategy"] for run in bundle["runs"]] == ["baseline", "reference"]
