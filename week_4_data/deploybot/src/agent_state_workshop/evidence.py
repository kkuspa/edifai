"""Read-only views of recorded runs, run comparison, and evidence export.

Everything here reads run directories. Nothing here executes agent work.
The CLI, the GUI, and the HTML export all build on these functions, so the
three interfaces show the same facts.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .runner import ARTIFACT_FILES, display_path, json_diff, read_json
from .scenarios import repo_root
from .telemetry import read_spans

EVENT_ID_RE = re.compile(r"\bevt-\d{3,}\b")


@dataclass
class RunView:
    run_dir: Path
    manifest: dict[str, Any]
    progress: dict[str, Any]
    artifacts: dict[str, Any] = field(default_factory=dict)
    spans: list[dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        p = self.progress
        return f"{p.get('strategy')} / {p.get('planner_mode')} / {self.run_dir.name}"


def load_run(run_dir: Path) -> RunView:
    run_dir = Path(run_dir)
    progress = read_json(run_dir / "progress.json")
    if progress is None:
        raise FileNotFoundError(f"{run_dir} is not a run directory (no progress.json)")
    artifacts = {}
    for name in ARTIFACT_FILES:
        if name.endswith(".json") and name not in ("manifest.json", "progress.json"):
            data = read_json(run_dir / name)
            if data is not None:
                artifacts[name] = data
    return RunView(run_dir, read_json(run_dir / "manifest.json", {}) or {}, progress, artifacts,
                   read_spans(run_dir / "spans.jsonl"))


def span_named(view: RunView, name: str) -> dict[str, Any] | None:
    return next((s for s in view.spans if s["name"] == name), None)


def evidence_ids_in(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found = EVENT_ID_RE.findall(value)
    elif isinstance(value, dict):
        for v in value.values():
            found += evidence_ids_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            found += evidence_ids_in(v)
    return list(dict.fromkeys(found))


def key_facts(view: RunView) -> dict[str, Any]:
    """The facts the comparison focuses on, each with the span that shows it."""
    ctx = view.artifacts.get("context.json") or {}
    proposal = view.artifacts.get("proposal.json") or {}
    gate = view.artifacts.get("gate.json") or {}
    receipt = view.artifacts.get("receipt.json")
    state = view.artifacts.get("state.json") or {}
    cb, pp, pv, td = (span_named(view, n) for n in ("context.build", "planner.propose", "policy.validate", "tool.deploy"))
    root = span_named(view, "agent.resume")
    target = ctx.get("target") or {}
    test = ctx.get("current_test") or {}
    appr = ctx.get("matching_approval") or {}
    event_ids = {e["event_id"] for e in view.artifacts.get("events.json") or []}
    cited = evidence_ids_in({"context": ctx, "proposal": proposal})
    cb_attrs = (cb or {}).get("attributes", {})
    return {
        "run_dir": display_path(view.run_dir),
        "run_id": view.progress.get("run_id"),
        "status": view.progress.get("status"),
        "strategy": view.progress.get("strategy"),
        "planner_mode": view.progress.get("planner_mode"),
        "provider": view.progress.get("provider"),
        "model": view.progress.get("model"),
        "target_environment": view.progress.get("target_environment"),
        "trace_id": view.progress.get("trace_id"),
        "root_span_id": (root or {}).get("span_id"),
        "root_status": ((root or {}).get("status") or {}).get("code"),
        "source_through_sequence": (view.progress.get("source") or {}).get("through_sequence"),
        "source_events_sha256": (view.progress.get("source") or {}).get("events_sha256"),
        "state_through_sequence": state.get("through_sequence"),
        "context_through_sequence": ctx.get("through_sequence"),
        "current_commit": target.get("commit"),
        "current_commit_source": target.get("source_event_id"),
        "test_status": test.get("status"),
        "test_commit": test.get("commit"),
        "test_source": test.get("source_event_id"),
        "approval_status": appr.get("status"),
        "approval_scope": f"{appr.get('commit')}/{appr.get('environment')}" if appr.get("commit") else None,
        "approval_source": appr.get("source_event_id"),
        "historical_approvals": [f"{a['commit']}/{a['environment']} ({a['source_event_id']})"
                                 for a in ctx.get("historical_approvals") or []],
        "context_blockers": ctx.get("blockers"),
        "narrative_memory": ctx.get("narrative_memory"),
        "proposed_action": proposal.get("action"),
        "proposed_target": f"{proposal.get('commit')} -> {proposal.get('environment')}" if proposal else None,
        "proposal_blockers": proposal.get("blockers"),
        "proposal_explanation": proposal.get("explanation"),
        "gate_outcome": gate.get("outcome"),
        "gate_reasons": gate.get("reason_codes"),
        "gate_authoritative": gate.get("authoritative"),
        "executed_action": "deploy" if receipt else "none",
        "receipt_id": (receipt or {}).get("receipt_id"),
        "span_ids": {
            "context.build": (cb or {}).get("span_id"),
            "planner.propose": (pp or {}).get("span_id"),
            "policy.validate": (pv or {}).get("span_id"),
            "tool.deploy": (td or {}).get("span_id"),
        },
        "evidence_selected": next((e["attributes"].get("workshop.evidence.event_ids") for e in (cb or {}).get("events", [])
                                   if e["name"] == "evidence.selected"), None),
        "annotation_complete": cb_attrs.get("workshop.annotation.complete"),
        "annotation_missing": cb_attrs.get("workshop.annotation.missing"),
        "unsourced_facts": cb_attrs.get("workshop.context.unsourced_facts"),
        "cited_event_ids": cited,
        "unresolved_event_ids": [e for e in cited if e not in event_ids],
    }


COMPARISON_ROWS: tuple[tuple[str, str], ...] = (
    ("Strategy / planner", "strategy"),
    ("Source events through", "source_through_sequence"),
    ("Memory through", "state_through_sequence"),
    ("Current commit (source)", "current_commit"),
    ("Test evidence", "test_status"),
    ("Approval for target", "approval_status"),
    ("Context blockers", "context_blockers"),
    ("Proposed action", "proposed_action"),
    ("Gate result", "gate_outcome"),
    ("Executed action", "executed_action"),
)


def fact_cell(f: dict[str, Any], key: str) -> str:
    """One comparison cell, with its scope and source spelled out."""
    if key == "strategy":
        return f"{f['strategy']} / planner_mode={f['planner_mode']}" + (f" ({f['provider']} {f['model']})" if f.get("provider") else "")
    if key == "current_commit":
        return f"{f['current_commit']} ({f['current_commit_source'] or 'no source'})"
    if key == "test_status":
        scope = f"commit {f['test_commit']}" if f.get("test_commit") else "NO COMMIT SCOPE"
        return f"{f['test_status']} - {scope}, source {f['test_source'] or 'none'}"
    if key == "approval_status":
        scope = f["approval_scope"] or "NO COMMIT/ENVIRONMENT SCOPE"
        hist = f"; history: {', '.join(f['historical_approvals'])}" if f.get("historical_approvals") else ""
        return f"{f['approval_status']} - {scope}, source {f['approval_source'] or 'none'}{hist}"
    if key == "proposed_action":
        return f"{f['proposed_action']} {f['proposed_target'] or ''} (span {f['span_ids']['planner.propose'] or '-'})"
    if key == "gate_outcome":
        return f"{f['gate_outcome']}: {', '.join(f['gate_reasons'] or [])} (span {f['span_ids']['policy.validate'] or '-'})"
    if key == "executed_action":
        if f["executed_action"] == "deploy":
            return f"deploy - receipt {f['receipt_id']} (span {f['span_ids']['tool.deploy']})"
        return "none - no tool.deploy span, no receipt"
    value = f.get(key)
    if isinstance(value, list):
        return ", ".join(value) or "(none)"
    return "-" if value is None else str(value)


def compare(before_dir: Path, after_dir: Path) -> dict[str, Any]:
    before, after = load_run(before_dir), load_run(after_dir)
    fb, fa = key_facts(before), key_facts(after)
    notices = []
    if fb["source_events_sha256"] != fa["source_events_sha256"]:
        notices.append(
            f"The runs observed different source histories (through {fb['source_through_sequence']} vs "
            f"{fa['source_through_sequence']}). Expected for a follow-up comparison; otherwise re-run from "
            "independent copies of the same history."
        )
    for label, f in (("before", fb), ("after", fa)):
        if f["status"] != "completed":
            notices.append(f"The {label} run is {f['status']}; some artifacts may be missing.")
        if f["annotation_complete"] is False:
            notices.append(f"The {label} run's context.build annotation is incomplete: "
                           f"{', '.join(f['annotation_missing'] or [])}.")
        if f["unsourced_facts"]:
            notices.append(f"The {label} run's context presents facts with no source event: "
                           f"{', '.join(f['unsourced_facts'])}.")
        if f["unresolved_event_ids"]:
            notices.append(f"The {label} run cites event IDs missing from its events.json: "
                           f"{', '.join(f['unresolved_event_ids'])}.")
    diffs = {
        name: json_diff(before.artifacts.get(name), after.artifacts.get(name))
        for name in ("state.json", "context.json", "proposal.json", "gate.json")
    }
    return {
        "before": fb,
        "after": fa,
        "rows": [{"label": label, "before": fact_cell(fb, key), "after": fact_cell(fa, key),
                  "changed": fact_cell(fb, key) != fact_cell(fa, key)} for label, key in COMPARISON_ROWS],
        "notices": notices,
        "diffs": diffs,
    }


def compare_text(result: dict[str, Any]) -> str:
    lines = [f"BEFORE {result['before']['run_dir']}  trace {result['before']['trace_id']}",
             f"AFTER  {result['after']['run_dir']}  trace {result['after']['trace_id']}", ""]
    for row in result["rows"]:
        mark = "*" if row["changed"] else " "
        lines.append(f"{mark} {row['label']}")
        lines.append(f"    before: {row['before']}")
        lines.append(f"    after:  {row['after']}")
    for f, label in ((result["before"], "before"), (result["after"], "after")):
        lines.append(f"\n{label} span IDs: " + ", ".join(f"{k}={v or '-'}" for k, v in f["span_ids"].items()))
        selected = ", ".join(f["evidence_selected"]) if f["evidence_selected"] else "(not annotated)"
        lines.append(f"{label} evidence.selected: {selected}")
    if result["notices"]:
        lines.append("\nNotices:")
        lines += [f"  ! {n}" for n in result["notices"]]
    return "\n".join(lines)


def diff_text(changes: list[dict[str, Any]]) -> str:
    if not changes:
        return "  (no differences)"
    out = []
    for c in changes:
        b = json.dumps(c["before"], sort_keys=True) if c["before"] is not None else "-"
        a = json.dumps(c["after"], sort_keys=True) if c["after"] is not None else "-"
        out.append(f"  {c['change']:7} {c['path']}\n          before: {b}\n          after:  {a}")
    return "\n".join(out)


def export_evidence(before: Path, after: Path, followups: list[Path], output: Path,
                    root: Path | None = None) -> dict[str, Any]:
    """Write the submission bundle: comparison.html, evidence.json, evidence.md, findings.md."""
    from .traceview import render_tree_text
    from .visualize import render_comparison

    output.mkdir(parents=True, exist_ok=True)
    html_path = output / "comparison.html"
    render_comparison(before, after, html_path)
    comparison = compare(before, after)
    runs = [load_run(p) for p in (before, after, *followups)]
    bundle = {
        "evidence_version": 1,
        "comparison": {k: comparison[k] for k in ("rows", "notices")},
        "runs": [key_facts(v) for v in runs],
        "diffs": comparison["diffs"],
    }
    (output / "evidence.json").write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    md = ["# Evidence index", "", "Generated by `workshop export`. Cite these IDs in findings.md.", "",
          "## Before/after comparison", "", "```", compare_text(comparison), "```", ""]
    for view in runs:
        f = key_facts(view)
        md += [f"## {view.run_dir.name}: {f['strategy']} / {f['planner_mode']}", "",
               f"Proposal: {f['proposed_action']} {f['proposed_target']}. Gate: {f['gate_outcome']} "
               f"({', '.join(f['gate_reasons'] or [])}). Executed: {f['executed_action']}.", "",
               "```", render_tree_text(view.spans, show_attributes=True), "```", ""]
    (output / "evidence.md").write_text("\n".join(md), encoding="utf-8")

    findings = output / "findings.md"
    if not findings.exists():
        template = (root or repo_root()) / "docs" / "findings-template.md"
        if not template.exists():
            template = repo_root() / "docs" / "findings-template.md"
        if template.exists():
            shutil.copyfile(template, findings)
    return {
        "output": str(output),
        "files": [str(p) for p in (html_path, output / "evidence.json", output / "evidence.md", findings) if p.exists()],
        "notices": comparison["notices"],
    }
