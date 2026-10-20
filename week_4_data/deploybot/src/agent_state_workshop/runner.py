"""The resume pipeline as a sequence of documented, individually executable stages.

    state.load       read the event history (and any saved snapshot or summary)
    memory.project   derive working state with the selected memory strategy
    context.build    select the facts the planner will see; annotate the span
    planner.propose  ask the planner for one typed next action
    policy.validate  the supplied gate re-checks the proposal against the event store
    tool.deploy      mock deployment; runs only if the gate allowed a deploy
    state.save       save the derived snapshot (structured strategies only)

A run can execute all stages in one call (`workshop run`) or one stage per
call (`workshop step`, the GUI's Step button), in the same or different
processes. Each stage reads its inputs from the run directory's artifacts and
writes its outputs there, so the run directory is both the evidence and the
hand-off between stages.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__, baseline, policy, tools
from .models import AgentContext, GateResult, Proposal, Receipt, WorkingState, sha256_of
from .planners import make_planner
from .scenarios import SESSION_ID
from .store import EventStore, utc_now
from .strategies import Strategy, SummaryStrategy, load_strategy, strategy_kind, strategy_source_path
from .support import ExerciseIncomplete, annotation_gaps, set_attr
from .telemetry import RunTelemetry, new_span_id, new_trace_id

STAGES: tuple[str, ...] = (
    "state.load",
    "memory.project",
    "context.build",
    "planner.propose",
    "policy.validate",
    "tool.deploy",
    "state.save",
)
STAGE_DESCRIPTIONS = {
    "state.load": "Read the event history and any saved snapshot or summary memory.",
    "memory.project": "Derive working state with the selected memory strategy.",
    "context.build": "Select the facts the planner will see and annotate the span.",
    "planner.propose": "Ask the planner for one typed next action.",
    "policy.validate": "The supplied gate re-checks the proposal against the event store.",
    "tool.deploy": "Mock deployment. Runs only if the gate allowed a deploy proposal.",
    "state.save": "Save the derived snapshot (structured strategies only).",
}

ARTIFACT_FILES = (
    "manifest.json",
    "progress.json",
    "events.json",
    "memory-loaded.json",
    "snapshot-before.json",
    "state.json",
    "state-diff.json",
    "context.json",
    "planner-input.json",
    "llm-request.json",
    "llm-response.json",
    "proposal.json",
    "gate.json",
    "tools.json",
    "receipt.json",
    "spans.jsonl",
)


class RunError(RuntimeError):
    """A run could not start or continue (bad arguments or session state)."""


class StageFailed(RuntimeError):
    def __init__(self, stage: str, error: BaseException):
        self.stage = stage
        self.error = error
        self.exercise_incomplete = isinstance(error, ExerciseIncomplete)
        super().__init__(f"{stage} failed: {type(error).__name__}: {error}")


@dataclass
class RunSpec:
    db_path: Path
    run_dir: Path
    strategy: str
    planner_mode: str = "simulated"
    provider: str | None = None
    model: str | None = None
    target_environment: str = "staging"
    otlp_endpoint: str | None = None
    replace: bool = False


@dataclass
class StageResult:
    stage: str
    status: str  # done | skipped | failed
    span_id: str | None
    duration_ms: float | None
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# ----------------------------------------------------------------- artifacts


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def display_path(path: Path | str) -> str:
    """A path for recorded artifacts: relative to the working directory, never a home directory."""
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return resolved.name


def json_diff(before: Any, after: Any, path: str = "$") -> list[dict[str, Any]]:
    """Structural differences as a flat list of {path, change, before, after}."""
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            sub = f"{path}.{key}"
            if key not in before:
                out.append({"path": sub, "change": "added", "before": None, "after": after[key]})
            elif key not in after:
                out.append({"path": sub, "change": "removed", "before": before[key], "after": None})
            else:
                out += json_diff(before[key], after[key], sub)
        return out
    if isinstance(before, list) and isinstance(after, list):
        out = []
        for i in range(max(len(before), len(after))):
            sub = f"{path}[{i}]"
            if i >= len(before):
                out.append({"path": sub, "change": "added", "before": None, "after": after[i]})
            elif i >= len(after):
                out.append({"path": sub, "change": "removed", "before": before[i], "after": None})
            else:
                out += json_diff(before[i], after[i], sub)
        return out
    if before != after:
        return [{"path": path, "change": "changed", "before": before, "after": after}]
    return []


# --------------------------------------------------------------- run context


@dataclass
class _Run:
    store: EventStore
    run_dir: Path
    progress: dict[str, Any]
    telemetry: RunTelemetry
    _strategy: Strategy | None = None

    @property
    def session_id(self) -> str:
        return self.progress["session_id"]

    @property
    def strategy(self) -> Strategy:
        if self._strategy is None:
            self._strategy = load_strategy(self.progress["strategy"])
        return self._strategy

    def art(self, name: str) -> Path:
        return self.run_dir / name

    def read(self, name: str) -> Any:
        return read_json(self.art(name))

    def write(self, name: str, data: Any) -> str:
        write_json(self.art(name), data)
        return name

    def record_tool_call(self, stage: str, tool: str, read_only: bool, inputs: dict, output: Any) -> None:
        calls = self.read("tools.json") or []
        calls.append({"stage": stage, "tool": tool, "read_only": read_only, "input": inputs,
                      "output": output, "at": utc_now()})
        self.write("tools.json", calls)

    def save_progress(self) -> None:
        write_json(self.art("progress.json"), self.progress)


# ------------------------------------------------------------------- stages


def _state_load(run: _Run, span: Any) -> dict[str, Any]:
    events = run.store.events(run.session_id)
    run.write("events.json", [e.model_dump(mode="json") for e in events])
    source = {
        "first_sequence": events[0].sequence if events else 0,
        "through_sequence": events[-1].sequence if events else 0,
        "event_count": len(events),
        "events_sha256": sha256_of([e.model_dump(mode="json") for e in events]),
    }
    run.progress["source"] = source
    set_attr(span, "workshop.session_id", run.session_id)
    set_attr(span, "workshop.source.first_sequence", source["first_sequence"])
    set_attr(span, "workshop.source.through_sequence", source["through_sequence"])
    set_attr(span, "workshop.source.event_count", source["event_count"])
    set_attr(span, "workshop.source.events_sha256", source["events_sha256"])
    set_attr(span, "workshop.artifact.events", "events.json")

    summary: dict[str, Any] = {"source": source}
    if isinstance(run.strategy, SummaryStrategy):
        memory = run.store.load_memory(run.session_id, baseline.SUMMARY_KIND)
        if memory is None:
            raise RunError("no saved narrative summary; was this database created by `workshop prepare`?")
        run.write("memory-loaded.json", {"kind": memory.kind, "through_sequence": memory.through_sequence,
                                         "writer_pid": memory.writer_pid, "saved_at": memory.saved_at,
                                         "body": memory.body})
        set_attr(span, "workshop.snapshot.status", "summary_memory")
        set_attr(span, "workshop.memory.summary_through_sequence", memory.through_sequence)
        set_attr(span, "workshop.memory.summary_writer_pid", memory.writer_pid)
        summary["memory_through_sequence"] = memory.through_sequence
        return summary

    snap = run.store.load_snapshot(run.session_id, run.progress["strategy"])
    if snap is None:
        status = "missing"
    elif snap.projection_version != WorkingState.model_fields["projection_version"].default:
        status = "version_mismatch"
    elif snap.through_sequence != source["through_sequence"]:
        status = "stale_position"
    else:
        status = "current_position"
    set_attr(span, "workshop.snapshot.status", status)
    if snap is not None:
        run.write("snapshot-before.json", {"projection_version": snap.projection_version,
                                           "through_sequence": snap.through_sequence,
                                           "state_hash": snap.state_hash, "saved_at": snap.saved_at,
                                           "state": snap.state})
        set_attr(span, "workshop.snapshot.through_sequence", snap.through_sequence)
        set_attr(span, "workshop.snapshot.projection_version", snap.projection_version)
        set_attr(span, "workshop.snapshot.state_hash", snap.state_hash)
    summary["snapshot_status"] = status
    return summary


def _memory_project(run: _Run, span: Any) -> dict[str, Any]:
    events_raw = run.read("events.json") or []
    set_attr(span, "workshop.memory_strategy", run.progress["strategy"])
    if isinstance(run.strategy, SummaryStrategy):
        memory = run.read("memory-loaded.json")
        state = baseline.load_state(run.session_id, memory["body"], memory["writer_pid"])
        data = state.model_dump(mode="json")
        through = state.through_sequence
        ignored = sum(1 for e in events_raw if e["sequence"] > through)
        set_attr(span, "workshop.projection.kind", "narrative-summary")
        set_attr(span, "workshop.projection.version", memory["body"].get("summary_version"))
        set_attr(span, "workshop.projection.input_first_sequence", 1)
        set_attr(span, "workshop.projection.input_through_sequence", through)
        set_attr(span, "workshop.projection.events_not_incorporated", ignored)
        summary = {"through_sequence": through, "events_not_incorporated": ignored}
    else:
        from .models import Event

        events = [Event.model_validate(e) for e in events_raw]
        state = run.strategy.project_state(events)
        if not isinstance(state, WorkingState):
            raise TypeError(f"project_state must return WorkingState, got {type(state).__name__}")
        data = state.canonical_dump()
        through = state.through_sequence
        set_attr(span, "workshop.projection.kind", "event-replay")
        set_attr(span, "workshop.projection.version", state.projection_version)
        set_attr(span, "workshop.projection.input_first_sequence", events[0].sequence if events else 0)
        set_attr(span, "workshop.projection.input_through_sequence", events[-1].sequence if events else 0)
        summary = {"through_sequence": through, "tests": len(state.tests), "approvals": len(state.approvals)}
        before = run.read("snapshot-before.json")
        if before is not None:
            diff = json_diff(before["state"], data)
            run.write("state-diff.json", {"from": "snapshot-before.json", "to": "state.json", "changes": diff})
            set_attr(span, "workshop.snapshot.matches_replay", before["state_hash"] == state.state_hash())
            set_attr(span, "workshop.artifact.state_diff", "state-diff.json")
            summary["changes_since_snapshot"] = len(diff)
    state_hash = sha256_of(data)
    run.write("state.json", data)
    set_attr(span, "workshop.projection.through_sequence", through)
    set_attr(span, "workshop.state.hash", state_hash)
    set_attr(span, "workshop.artifact.state", "state.json")
    return summary


def _context_build(run: _Run, span: Any) -> dict[str, Any]:
    state = run.read("state.json")
    target_env = run.progress["target_environment"]
    if isinstance(run.strategy, SummaryStrategy):
        with run.telemetry.tracer.start_as_current_span("tool.inspect_head") as tool_span:
            head = tools.inspect_head(run.store, run.session_id)
            set_attr(tool_span, "workshop.tool.name", "inspect_head")
            set_attr(tool_span, "workshop.tool.read_only", True)
            set_attr(tool_span, "workshop.head.commit", head.commit if head else None)
            set_attr(tool_span, "workshop.head.source_event_id", head.source_event_id if head else None)
        run.record_tool_call("context.build", "inspect_head", True, {"session_id": run.session_id},
                             head.__dict__ if head else None)
        request = tools.latest_user_request(run.store, run.session_id)
        run.record_tool_call("context.build", "latest_user_request", True, {"session_id": run.session_id},
                             request.__dict__ if request else None)
        context = baseline.build_context(baseline.BaselineState.model_validate(state), head, request, target_env)
    else:
        context = run.strategy.build_context(WorkingState.model_validate(state), target_env)
        if not isinstance(context, AgentContext):
            raise TypeError(f"build_context must return AgentContext, got {type(context).__name__}")
    data = context.model_dump(mode="json")
    run.write("context.json", data)
    set_attr(span, "workshop.memory_strategy", run.progress["strategy"])
    set_attr(span, "workshop.context.through_sequence", context.through_sequence)
    set_attr(span, "workshop.context.hash", sha256_of(data))
    set_attr(span, "workshop.artifact.context", "context.json")

    run.strategy.annotate_context_span(span, context)

    gaps = annotation_gaps(span.attributes or {}, span.events, context)
    unsourced = []
    if context.current_test.status != "unknown" and not context.current_test.source_event_id:
        unsourced.append("current_test")
    if context.matching_approval.status == "approved" and not context.matching_approval.source_event_id:
        unsourced.append("matching_approval")
    if context.target.commit and not context.target.source_event_id:
        unsourced.append("target")
    set_attr(span, "workshop.annotation.complete", not gaps)
    set_attr(span, "workshop.annotation.missing", gaps)
    set_attr(span, "workshop.context.unsourced_facts", unsourced)
    run.progress["annotation"] = {"complete": not gaps, "missing": gaps}
    return {"commit": context.target.commit, "test_status": context.current_test.status,
            "approval_status": context.matching_approval.status, "blockers": context.blockers,
            "annotation_complete": not gaps, "unsourced_facts": unsourced}


def _planner_propose(run: _Run, span: Any) -> dict[str, Any]:
    context = AgentContext.model_validate(run.read("context.json"))
    p = run.progress
    planner = make_planner(p["planner_mode"], p.get("provider"), p.get("model"))
    started = time.perf_counter()
    outcome = planner.propose(context, tracer=run.telemetry.tracer)
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    proposal = outcome.proposal
    run.write("planner-input.json", outcome.planner_input)
    run.write("proposal.json", proposal.model_dump(mode="json"))
    if outcome.exchanges:
        run.write("llm-request.json", outcome.planner_input)
        run.write("llm-response.json", outcome.exchanges)
    p["planner"] = outcome.metadata
    set_attr(span, "workshop.planner_mode", p["planner_mode"])
    set_attr(span, "workshop.planner.name", outcome.metadata.get("planner"))
    set_attr(span, "workshop.llm.provider", outcome.metadata.get("provider"))
    set_attr(span, "workshop.llm.request_model", outcome.metadata.get("model"))
    set_attr(span, "workshop.llm.attempts", outcome.metadata.get("attempts"))
    set_attr(span, "workshop.llm.input_tokens", outcome.metadata.get("input_tokens"))
    set_attr(span, "workshop.llm.output_tokens", outcome.metadata.get("output_tokens"))
    set_attr(span, "workshop.planner.elapsed_ms", elapsed)
    set_attr(span, "workshop.proposal.action", proposal.action)
    set_attr(span, "workshop.proposal.commit", proposal.commit)
    set_attr(span, "workshop.proposal.environment", proposal.environment)
    set_attr(span, "workshop.proposal.blockers", proposal.blockers)
    set_attr(span, "workshop.proposal.evidence_event_ids", proposal.evidence_event_ids)
    set_attr(span, "workshop.proposal.hash", sha256_of(proposal))
    set_attr(span, "workshop.artifact.proposal", "proposal.json")
    set_attr(span, "workshop.artifact.planner_input", "planner-input.json")
    return {"action": proposal.action, "commit": proposal.commit, "environment": proposal.environment,
            "blockers": proposal.blockers, "explanation": proposal.explanation}


def _policy_validate(run: _Run, span: Any) -> dict[str, Any]:
    proposal = Proposal.model_validate(run.read("proposal.json"))
    gate = policy.validate(run.store, run.session_id, proposal, run.progress["target_environment"])
    run.write("gate.json", gate.model_dump(mode="json"))
    a = gate.authoritative
    set_attr(span, "workshop.gate.outcome", gate.outcome)
    set_attr(span, "workshop.gate.reason_codes", gate.reason_codes)
    set_attr(span, "workshop.gate.authoritative_blockers", gate.authoritative_blockers)
    set_attr(span, "workshop.gate.checked_through_sequence", a.checked_through_sequence)
    set_attr(span, "workshop.gate.authoritative.head_commit", a.head_commit)
    set_attr(span, "workshop.gate.authoritative.head_source_event_id", a.head_source_event_id)
    set_attr(span, "workshop.gate.authoritative.test_result", a.test_result)
    set_attr(span, "workshop.gate.authoritative.test_source_event_id", a.test_source_event_id)
    set_attr(span, "workshop.gate.authoritative.approval_id", a.approval_id)
    set_attr(span, "workshop.gate.authoritative.approval_source_event_id", a.approval_source_event_id)
    set_attr(span, "workshop.artifact.gate", "gate.json")
    if not gate.allowed:
        span.add_event("proposal.rejected", {"workshop.proposal.action": proposal.action,
                                             "workshop.gate.reason_codes": list(gate.reason_codes)})
    return {"outcome": gate.outcome, "reason_codes": gate.reason_codes,
            "authoritative_blockers": gate.authoritative_blockers}


def _deploy_skip_reason(run: _Run) -> str | None:
    gate = GateResult.model_validate(run.read("gate.json"))
    if gate.action != "deploy":
        return f"no tool call: proposal action is {gate.action}"
    if not gate.allowed:
        return "no tool call: the gate blocked the deploy proposal"
    return None


def _tool_deploy(run: _Run, span: Any) -> dict[str, Any]:
    proposal = Proposal.model_validate(run.read("proposal.json"))
    gate = GateResult.model_validate(run.read("gate.json"))
    receipt: Receipt = tools.mock_deploy(run.store, run.session_id, proposal, gate, run.progress["run_id"])
    run.write("receipt.json", receipt.model_dump(mode="json"))
    run.record_tool_call("tool.deploy", "mock_deploy", False,
                         {"commit": proposal.commit, "environment": proposal.environment},
                         receipt.model_dump(mode="json"))
    set_attr(span, "workshop.tool.name", "mock_deploy")
    set_attr(span, "workshop.deploy.commit", receipt.commit)
    set_attr(span, "workshop.deploy.environment", receipt.environment)
    set_attr(span, "workshop.deploy.receipt_id", receipt.receipt_id)
    set_attr(span, "workshop.deploy.reused_existing", receipt.reused_existing)
    set_attr(span, "workshop.artifact.receipt", "receipt.json")
    return {"receipt_id": receipt.receipt_id, "reused_existing": receipt.reused_existing}


def _state_save(run: _Run, span: Any) -> dict[str, Any]:
    receipt = run.read("receipt.json")
    if receipt:
        set_attr(span, "workshop.deploy.receipt_id", receipt["receipt_id"])
    if isinstance(run.strategy, SummaryStrategy):
        set_attr(span, "workshop.snapshot.saved", False)
        set_attr(span, "workshop.snapshot.skip_reason", "baseline keeps its pause-time summary unchanged")
        return {"saved": False}
    state = WorkingState.model_validate(run.read("state.json"))
    row = run.store.save_snapshot(run.progress["strategy"], state)
    set_attr(span, "workshop.snapshot.saved", True)
    set_attr(span, "workshop.snapshot.projection_version", row.projection_version)
    set_attr(span, "workshop.snapshot.through_sequence", row.through_sequence)
    set_attr(span, "workshop.state.hash", row.state_hash)
    return {"saved": True, "through_sequence": row.through_sequence, "state_hash": row.state_hash}


HANDLERS = {
    "state.load": _state_load,
    "memory.project": _memory_project,
    "context.build": _context_build,
    "planner.propose": _planner_propose,
    "policy.validate": _policy_validate,
    "tool.deploy": _tool_deploy,
    "state.save": _state_save,
}


# ---------------------------------------------------------------- lifecycle


def _session_id(store: EventStore) -> str:
    return store.get_meta("session_id") or SESSION_ID


def start_run(spec: RunSpec) -> dict[str, Any]:
    """Create the run record and directory. Executes no stage."""
    db_path = Path(spec.db_path)
    if not db_path.exists():
        raise RunError(f"{db_path} does not exist. Create it with `workshop prepare --db {db_path}`.")
    load_strategy(spec.strategy)  # fail fast on an unknown strategy
    planner = make_planner(spec.planner_mode, spec.provider, spec.model)
    with EventStore(db_path) as store:
        if store.get_meta("prepared") != "1":
            raise RunError(f"{db_path} was not fully prepared. Re-run `workshop prepare --reset`.")
        session_id = _session_id(store)
        active = store.active_run(session_id)
        if active:
            raise RunError(
                f"run {active['run_id']} is still in progress (next stage: {active['next_stage']}). "
                "Continue it with `workshop step`, or abandon it with `workshop abandon`."
            )
        run_dir = Path(spec.run_dir)
        if run_dir.exists() and any(run_dir.iterdir()):
            previous = read_json(run_dir / "progress.json", {}) or {}
            if spec.replace or previous.get("status") == "failed":
                shutil.rmtree(run_dir)
            else:
                raise RunError(
                    f"{run_dir} already holds a run. Run directories are immutable; "
                    "choose a new --out, or pass --replace to discard it."
                )
        run_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        run_id = f"run-{now:%Y%m%dT%H%M%S}-{secrets.token_hex(2)}"
        desc = planner.describe()
        progress = {
            "progress_version": 1,
            "run_id": run_id,
            "session_id": session_id,
            "scenario": store.get_meta("scenario"),
            "db_path": os.path.relpath(db_path.resolve(), run_dir.resolve()),  # relative to the run directory
            "run_dir": display_path(run_dir),
            "strategy": spec.strategy,
            "planner_mode": spec.planner_mode,
            "provider": desc.get("provider"),
            "model": desc.get("model"),
            "target_environment": spec.target_environment,
            "otlp_endpoint": spec.otlp_endpoint,
            "trace_id": new_trace_id(),
            "root_span_id": new_span_id(),
            "started_at": utc_now(),
            "started_at_ns": time.time_ns(),
            "status": "active",
            "next_stage": STAGES[0],
            "stages": [],
        }
        write_json(run_dir / "progress.json", progress)
        store.insert_run(
            run_id=run_id, session_id=session_id, run_dir=str(run_dir), strategy=spec.strategy,
            planner_mode=spec.planner_mode, provider=desc.get("provider"),
            target_environment=spec.target_environment, trace_id=progress["trace_id"],
            root_span_id=progress["root_span_id"], status="active", next_stage=STAGES[0],
            started_at=progress["started_at"],
        )
    return progress


def advance(db_path: Path, run_id: str | None = None, until_done: bool = False,
            otlp_endpoint: str | None = None) -> list[StageResult]:
    """Execute the next stage of a run (or all remaining stages)."""
    with EventStore(db_path) as store:
        session_id = _session_id(store)
        row = store.get_run(run_id) if run_id else store.active_run(session_id)
        if row is None:
            raise RunError("no run in progress for this database. Start one with `workshop run` or `workshop step`.")
        if row["status"] != "active":
            raise RunError(f"run {row['run_id']} is already {row['status']}.")
        run_dir = Path(row["run_dir"])
        progress = read_json(run_dir / "progress.json")
        endpoint = otlp_endpoint or progress.get("otlp_endpoint")
        telemetry = RunTelemetry(run_dir, progress["trace_id"], progress["root_span_id"], endpoint)
        run = _Run(store, run_dir, progress, telemetry)
        results: list[StageResult] = []
        finalized = False
        try:
            while progress["status"] == "active":
                result = _execute_stage(run, progress["next_stage"])
                results.append(result)
                finalized = progress["status"] != "active"
                if not until_done:
                    break
        finally:
            telemetry.shutdown()
            if finalized:
                manifest = read_json(run_dir / "manifest.json", {})
                manifest["otlp"] = {"endpoint": endpoint, "errors": telemetry.otlp_errors}
                write_json(run_dir / "manifest.json", manifest)
        return results


def _execute_stage(run: _Run, stage: str) -> StageResult:
    progress = run.progress
    record: dict[str, Any] = {"stage": stage, "pid": os.getpid(), "started_at": utc_now()}
    if stage == "tool.deploy":
        reason = _deploy_skip_reason(run)
        if reason:
            record.update(status="skipped", span_id=None, duration_ms=None, summary={"reason": reason})
            return _complete_stage(run, stage, record)

    started = time.perf_counter()
    span_ref: dict[str, Any] = {}
    try:
        source = strategy_source_path(progress["strategy"])
        record["strategy_source_sha256"] = file_sha256(source) if source.exists() else None
        with run.telemetry.stage_span(stage) as span:
            span_ref["span"] = span
            set_attr(span, "workshop.run_id", progress["run_id"])
            set_attr(span, "workshop.stage", stage)
            summary = HANDLERS[stage](run, span)
    except Exception as exc:
        span = span_ref.get("span")
        record.update(
            status="failed",
            span_id=f"{span.get_span_context().span_id:016x}" if span else None,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            error=f"{type(exc).__name__}: {exc}",
            exercise_incomplete=isinstance(exc, ExerciseIncomplete),
        )
        progress["stages"].append(record)
        _finalize(run, failed=StageFailed(stage, exc))
        return StageResult(stage, "failed", record["span_id"], record["duration_ms"], {}, record["error"])

    record.update(
        status="done",
        span_id=f"{span_ref['span'].get_span_context().span_id:016x}",
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
        summary=summary,
    )
    return _complete_stage(run, stage, record)


def _complete_stage(run: _Run, stage: str, record: dict[str, Any]) -> StageResult:
    progress = run.progress
    progress["stages"].append(record)
    index = STAGES.index(stage)
    progress["next_stage"] = STAGES[index + 1] if index + 1 < len(STAGES) else None
    if progress["next_stage"] is None:
        _finalize(run)
    else:
        run.save_progress()
        run.store.update_run(progress["run_id"], next_stage=progress["next_stage"])
    return StageResult(stage, record["status"], record.get("span_id"), record.get("duration_ms"),
                       record.get("summary", {}), record.get("error"))


def _finalize(run: _Run, failed: StageFailed | None = None) -> None:
    progress = run.progress
    proposal = run.read("proposal.json") or {}
    gate = run.read("gate.json") or {}
    receipt = run.read("receipt.json")
    state = run.read("state.json") or {}
    pids = sorted({s["pid"] for s in progress["stages"]})
    status = "failed" if failed else "completed"
    executed = "deploy" if receipt else "none"

    root_attrs = {
        "workshop.session_id": run.session_id,
        "workshop.run_id": progress["run_id"],
        "workshop.scenario_id": progress.get("scenario"),
        "workshop.planner_mode": progress["planner_mode"],
        "workshop.memory_strategy": progress["strategy"],
        "workshop.target.environment": progress["target_environment"],
        "workshop.run.status": status,
        "workshop.execution.mode": "continuous" if len(pids) <= 1 else "stepped",
        "workshop.execution.process_count": len(pids),
        "workshop.projection.through_sequence": state.get("through_sequence"),
        "workshop.source.through_sequence": (progress.get("source") or {}).get("through_sequence"),
        "workshop.outcome.proposed_action": proposal.get("action"),
        "workshop.outcome.proposed_commit": proposal.get("commit"),
        "workshop.outcome.gate": gate.get("outcome"),
        "workshop.outcome.executed_action": executed,
        "workshop.llm.provider": progress.get("provider"),
        "workshop.llm.request_model": progress.get("model"),
    }
    links = []
    prev = run.store.previous_completed_run(run.session_id)
    if prev:
        links.append((prev["trace_id"], prev["root_span_id"], {"workshop.link.reason": "previous_resume_run",
                                                                "workshop.link.run_id": prev["run_id"]}))
    run.telemetry.emit_root(progress["started_at_ns"], root_attrs, links,
                            error=str(failed) if failed else None)

    progress["status"] = status
    progress["next_stage"] = None
    progress["finished_at"] = utc_now()
    if failed:
        progress["error"] = str(failed)
        progress["failed_stage"] = failed.stage
        progress["exercise_incomplete"] = failed.exercise_incomplete
    run.save_progress()

    kind = strategy_kind(progress["strategy"])
    source_path = strategy_source_path(progress["strategy"])
    manifest = {
        "manifest_version": 1,
        "run_id": progress["run_id"],
        "session_id": run.session_id,
        "scenario": progress.get("scenario"),
        "status": status,
        "error": progress.get("error"),
        "failed_stage": progress.get("failed_stage"),
        "exercise_incomplete": progress.get("exercise_incomplete", False),
        "trace_id": progress["trace_id"],
        "root_span_id": progress["root_span_id"],
        "code_version": __version__,
        "strategy": progress["strategy"],
        "strategy_kind": kind,
        "strategy_source": source_path.name,
        "strategy_source_sha256": file_sha256(source_path) if source_path.exists() else None,
        "projection_version": WorkingState.model_fields["projection_version"].default
        if kind == "structured" else f"{baseline.SUMMARY_KIND}/v{baseline.SUMMARY_VERSION}",
        "planner_mode": progress["planner_mode"],
        "planner": progress.get("planner") or {"planner_mode": progress["planner_mode"]},
        "configuration": {
            "target_environment": progress["target_environment"],
            "db_path": progress["db_path"],
            "otlp_endpoint": progress.get("otlp_endpoint"),
            "execution_mode": root_attrs["workshop.execution.mode"],
            "process_ids": pids,
        },
        "source": progress.get("source"),
        "checkpoint": {
            "writer_pid": run.store.get_meta("checkpoint_pid"),
            "prepared_at": run.store.get_meta("prepared_at"),
        },
        "outcome": {
            "proposed_action": proposal.get("action"),
            "proposed_commit": proposal.get("commit"),
            "proposed_environment": proposal.get("environment"),
            "gate": gate.get("outcome"),
            "gate_reasons": gate.get("reason_codes"),
            "executed_action": executed,
            "receipt_id": receipt.get("receipt_id") if receipt else None,
        },
        "annotation": progress.get("annotation"),
        "started_at": progress["started_at"],
        "finished_at": progress["finished_at"],
        "artifacts": {
            name: {"path": name, "sha256": file_sha256(run.art(name))}
            for name in ARTIFACT_FILES
            if name not in ("manifest.json",) and run.art(name).exists()
        },
    }
    write_json(run.art("manifest.json"), manifest)
    run.store.update_run(progress["run_id"], status=status, next_stage=None, finished_at=progress["finished_at"])


def abandon(db_path: Path) -> dict[str, Any] | None:
    """Mark the active run abandoned. No root span is emitted for it."""
    with EventStore(db_path) as store:
        row = store.active_run(_session_id(store))
        if row is None:
            return None
        run_dir = Path(row["run_dir"])
        progress = read_json(run_dir / "progress.json", {}) or {}
        progress.update(status="abandoned", next_stage=None, finished_at=utc_now())
        write_json(run_dir / "progress.json", progress)
        store.update_run(row["run_id"], status="abandoned", next_stage=None, finished_at=progress["finished_at"])
        return {"run_id": row["run_id"], "run_dir": str(run_dir)}
