"""Exercise acceptance checks, shared by `workshop check`, the GUI, and pytest.

Each check reports one of:
  pass   the behaviour matches the specification
  todo   the code reached a todo(...) that has not been replaced yet
  fail   the code ran but produced the wrong data
  error  the code raised an unexpected exception

Exercise 2 and 3 checks use fixed inputs, not your project_state, so each
exercise can be checked on its own. The decision checks run exercises 1 and
2 together with the simulated planner and the supplied gate.
"""

from __future__ import annotations

import random
import tempfile
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from . import policy
from .models import (
    APPROVAL_MISSING,
    TESTS_FAILED,
    TESTS_UNKNOWN,
    AgentContext,
    ApprovalEvidence,
    ApprovalRecord,
    Event,
    HeadRef,
    TargetRef,
    TestEvidence,
    TestRecord,
    WorkingState,
)
from .planners import SimulatedPlanner
from .scenarios import DECISION_SCENARIOS, expected_state_40, full_history, synthetic
from .store import EventStore
from .strategies import StructuredStrategy, load_strategy
from .support import EVIDENCE_EVENT, EVIDENCE_IDS_KEY, ExerciseIncomplete, annotation_gaps


@dataclass
class CheckResult:
    exercise: str
    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _events_through(seq: int) -> list[Event]:
    return [e for e in full_history() if e.sequence <= seq]


def _state_40() -> WorkingState:
    return WorkingState.model_validate(expected_state_40())


def _expect(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


# ------------------------------------------------------------- exercise 1


def _e1_expected_state(s: StructuredStrategy) -> str:
    got = s.project_state(_events_through(40))
    _expect(isinstance(got, WorkingState), "project_state must return a WorkingState")
    expected = expected_state_40()
    actual = got.canonical_dump()
    for key in ("through_sequence", "head", "tests", "approvals"):
        _expect(actual.get(key) == expected[key],
                f"{key} differs.\n    expected: {expected[key]}\n    actual:   {actual.get(key)}")
    return "state at sequence 40 matches fixtures/stale-resume/expected-state-40.json"


def _e1_history_kept(s: StructuredStrategy) -> str:
    state = s.project_state(_events_through(40))
    a1 = [t for t in state.tests if t.commit == "a1" and t.suite == "required"]
    _expect(len(a1) == 1 and a1[0].result == "passed" and a1[0].source_event_id == "evt-010",
            "the a1 test result (evt-010) must stay attached to a1 after the head moves to b2")
    _expect(any(a.commit == "a1" and a.environment == "staging" and a.source_event_id == "evt-012"
                for a in state.approvals), "the a1/staging approval (evt-012) must be kept with its scope")
    _expect(not any(t.commit == "b2" and t.source_event_id == "evt-010" for t in state.tests),
            "evt-010 describes a1; it must never be relabeled as a b2 fact")
    return "a1 facts keep their commit, environment, and source after the head change"


def _e1_deterministic(s: StructuredStrategy) -> str:
    events = _events_through(40)
    first = s.project_state(events).state_hash()
    noisy = events + events[5:15]
    random.Random(7).shuffle(noisy)
    second = s.project_state(noisy).state_hash()
    _expect(first == second, "duplicate or reordered delivery changed the projected state")
    _expect(first == s.project_state(events).state_hash(), "replaying the same events gave a different hash")
    return "same events (with duplicates, any delivery order) give the same canonical hash"


def _e1_highest_run(s: StructuredStrategy) -> str:
    extra = [
        synthetic(41, "test.completed", "ci", {"commit": "b2", "suite": "required", "test_run_id": "ci-b2-3",
                                               "run_number": 3, "result": "passed"}),
        synthetic(42, "test.completed", "ci", {"commit": "b2", "suite": "required", "test_run_id": "ci-b2-2",
                                               "run_number": 2, "result": "failed"}),
    ]
    state = s.project_state(_events_through(40) + extra)
    b2 = [t for t in state.tests if t.commit == "b2" and t.suite == "required"]
    _expect(len(b2) == 1, f"expected one required-suite record for b2, found {len(b2)}")
    _expect(b2[0].run_number == 3 and b2[0].source_event_id == "evt-041",
            "run 2 arrived after run 3; the highest run number (3, evt-041) must win")
    return "the highest completed run number wins, regardless of arrival order"


def _e1_old_commit(s: StructuredStrategy) -> str:
    extra = [synthetic(41, "test.completed", "ci", {"commit": "a1", "suite": "required", "test_run_id": "ci-a1-2",
                                                    "run_number": 2, "result": "failed"})]
    state = s.project_state(_events_through(40) + extra)
    b2 = next((t for t in state.tests if t.commit == "b2" and t.suite == "required"), None)
    _expect(b2 is not None and b2.source_event_id == "evt-032",
            "a later result for a1 must not replace b2's result (evt-032)")
    a1 = next((t for t in state.tests if t.commit == "a1" and t.suite == "required"), None)
    _expect(a1 is not None and a1.run_number == 2, "the newer a1 run should update a1's own record")
    return "a newer result for an older commit never replaces the current commit's result"


# ------------------------------------------------------------- exercise 2


def _e2_test_scoped(s: StructuredStrategy) -> str:
    ctx = s.build_context(_state_40(), "staging")
    _expect(isinstance(ctx, AgentContext), "build_context must return an AgentContext")
    _expect(ctx.target.commit == "b2" and ctx.target.source_event_id == "evt-021", "target must be b2 from evt-021")
    t = ctx.current_test
    _expect(t.status == "failed" and t.commit == "b2" and t.source_event_id == "evt-032",
            f"current_test must be b2's required result from evt-032, got {t.model_dump()}")
    return "current_test is b2's required-suite failure (evt-032)"


def _e2_approval_scoped(s: StructuredStrategy) -> str:
    ctx = s.build_context(_state_40(), "staging")
    _expect(ctx.matching_approval.status == "missing",
            "the only approval is for a1; it must not count as approval for b2")
    _expect(any(a.commit == "a1" and a.source_event_id == "evt-012" for a in ctx.historical_approvals),
            "the a1 approval should remain visible in historical_approvals with its scope")
    return "the a1/staging approval stays historical; b2/staging approval is missing"


def _e2_all_blockers(s: StructuredStrategy) -> str:
    ctx = s.build_context(_state_40(), "staging")
    _expect({TESTS_FAILED, APPROVAL_MISSING} <= set(ctx.blockers),
            f"expected both tests_failed and approval_missing_for_target, got {ctx.blockers}")
    return "both blockers are preserved: tests_failed and approval_missing_for_target"


def _e2_unknown(s: StructuredStrategy) -> str:
    state = _state_40()
    state.head = HeadRef(commit="c3", source_event_id="evt-041")
    ctx = s.build_context(state, "staging")
    _expect(ctx.current_test.status == "unknown" and ctx.current_test.source_event_id is None,
            "with no result for c3 the status must be unknown, with no source")
    _expect(TESTS_UNKNOWN in ctx.blockers, "tests_unknown must be listed as a blocker")
    return "a commit with no test result is reported as unknown, not invented"


def _e2_environment(s: StructuredStrategy) -> str:
    state = _state_40()
    state.tests = [t for t in state.tests if not (t.commit == "b2" and t.suite == "required")]
    state.tests.append(TestRecord(commit="b2", suite="required", run_number=2, result="passed", source_event_id="evt-041"))
    state.approvals.append(ApprovalRecord(approval_id="approval-b2-staging", commit="b2", environment="staging",
                                          approved_by="user:release-lead", source_event_id="evt-042"))
    ctx = s.build_context(state, "production")
    _expect(ctx.matching_approval.status == "missing", "a staging approval must not authorize production")
    ctx2 = s.build_context(state, "staging")
    _expect(ctx2.matching_approval.status == "approved" and ctx2.matching_approval.source_event_id == "evt-042",
            "b2/staging approval (evt-042) should match a staging target")
    return "approval matches only the exact commit and environment"


def _e2_evidence(s: StructuredStrategy) -> str:
    ctx = s.build_context(_state_40(), "staging")
    known = {e.event_id for e in full_history()}
    unknown = [e for e in ctx.evidence_event_ids if e not in known]
    _expect(not unknown, f"evidence IDs that resolve to no event: {unknown}")
    _expect({"evt-021", "evt-032"} <= set(ctx.evidence_event_ids), "evidence must cite evt-021 and evt-032")
    return "evidence IDs resolve to source events and cite the head and current test"


# ------------------------------------------------------- decisions (1 + 2)


def _decision(scenario: Any) -> Callable[[StructuredStrategy], str]:
    def check(s: StructuredStrategy) -> str:
        events = scenario.events()
        state = s.project_state(events)
        ctx = s.build_context(state, scenario.target_environment)
        proposal = SimulatedPlanner().propose(ctx).proposal
        with tempfile.TemporaryDirectory() as tmp, EventStore(Path(tmp) / "check.sqlite") as store:
            store.append_many(events)
            gate = policy.validate(store, events[0].session_id, proposal, scenario.target_environment)
        _expect(proposal.action == scenario.expected_action and proposal.commit == scenario.expected_commit,
                f"expected {scenario.expected_action} {scenario.expected_commit}, "
                f"got {proposal.action} {proposal.commit}")
        missing = set(scenario.required_blockers) - set(proposal.blockers)
        _expect(not missing, f"blockers missing from the proposal: {sorted(missing)}")
        _expect(gate.outcome == scenario.expected_gate, f"gate outcome {gate.outcome}, expected {scenario.expected_gate}")
        return f"{scenario.description}: {proposal.action} {proposal.commit} -> {proposal.environment}"
    return check


# ------------------------------------------------------------- exercise 3


def _resume_context() -> AgentContext:
    """The correct context for the resume scenario, built by hand."""
    return AgentContext(
        session_id="release-demo",
        strategy="check",
        through_sequence=40,
        user_request="Can you finish the deployment?",
        target=TargetRef(commit="b2", environment="staging", source_event_id="evt-021"),
        current_test=TestEvidence(status="failed", commit="b2", suite="required", run_number=1,
                                  source_event_id="evt-032"),
        matching_approval=ApprovalEvidence(status="missing", commit="b2", environment="staging"),
        blockers=[TESTS_FAILED, APPROVAL_MISSING],
        historical_tests=[TestRecord(commit="a1", suite="required", run_number=1, result="passed",
                                     source_event_id="evt-010")],
        historical_approvals=[ApprovalRecord(approval_id="approval-a1-staging", commit="a1",
                                             environment="staging", approved_by="user:release-lead",
                                             source_event_id="evt-012")],
        evidence_event_ids=["evt-021", "evt-032", "evt-012"],
    )


def _annotate(s: StructuredStrategy, ctx: AgentContext) -> Any:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("check").start_as_current_span("context.build") as span:
        s.annotate_context_span(span, ctx)
    provider.shutdown()
    return exporter.get_finished_spans()[0]


def _e3_attributes(s: StructuredStrategy) -> str:
    ctx = _resume_context()
    span = _annotate(s, ctx)
    gaps = annotation_gaps(span.attributes, span.events, ctx)
    if gaps and len(gaps) >= 4:
        raise ExerciseIncomplete(f"3: context.build annotation is missing {', '.join(gaps)}")
    _expect(not gaps, f"context.build annotation is missing: {', '.join(gaps)}")
    a = span.attributes
    _expect(a["workshop.test.status"] == "failed" and a["workshop.test.source_event_id"] == "evt-032",
            "workshop.test.* must describe b2's failed run from evt-032")
    _expect(a["workshop.approval.status"] == "missing", "workshop.approval.status must be 'missing'")
    _expect(set(a["workshop.blockers"]) == {TESTS_FAILED, APPROVAL_MISSING}, "workshop.blockers must list both blockers")
    return "context.build carries target, test, approval, and blocker attributes"


def _e3_evidence_event(s: StructuredStrategy) -> str:
    ctx = _resume_context()
    span = _annotate(s, ctx)
    events = [e for e in span.events if e.name == EVIDENCE_EVENT]
    if not events:
        raise ExerciseIncomplete(f"3d: add a span event named {EVIDENCE_EVENT!r}")
    ids = list(events[0].attributes.get(EVIDENCE_IDS_KEY, ()))
    _expect(ids == ctx.evidence_event_ids, f"{EVIDENCE_IDS_KEY} should be {ctx.evidence_event_ids}, got {ids}")
    return f"{EVIDENCE_EVENT} lists the context's evidence event IDs"


CHECKS: tuple[tuple[str, str, Callable[[StructuredStrategy], str]], ...] = (
    ("1", "expected working state", _e1_expected_state),
    ("1", "history keeps its scope", _e1_history_kept),
    ("1", "deterministic and duplicate-safe", _e1_deterministic),
    ("1", "highest run number wins", _e1_highest_run),
    ("1", "old commit cannot overwrite current", _e1_old_commit),
    ("2", "current test is scoped", _e2_test_scoped),
    ("2", "approval is scoped", _e2_approval_scoped),
    ("2", "all blockers preserved", _e2_all_blockers),
    ("2", "missing result stays unknown", _e2_unknown),
    ("2", "environment must match", _e2_environment),
    ("2", "evidence IDs resolve", _e2_evidence),
    *(("1+2", f"decision: {sc.name}", _decision(sc)) for sc in DECISION_SCENARIOS),
    ("3", "span attributes", _e3_attributes),
    ("3", "evidence.selected event", _e3_evidence_event),
)


def run_checks(strategy_name: str) -> list[CheckResult]:
    strategy = load_strategy(strategy_name)
    if not isinstance(strategy, StructuredStrategy):
        return [CheckResult("-", "strategy", "error",
                            "The baseline is intentionally flawed and is not graded. Check student or reference.")]
    results = []
    for exercise, name, fn in CHECKS:
        try:
            results.append(CheckResult(exercise, name, "pass", fn(strategy)))
        except ExerciseIncomplete as exc:
            results.append(CheckResult(exercise, name, "todo", f"unfinished: {exc}"))
        except AssertionError as exc:
            results.append(CheckResult(exercise, name, "fail", str(exc)))
        except Exception as exc:
            tb = traceback.extract_tb(exc.__traceback__)
            where = next((f"{Path(f.filename).name}:{f.lineno}" for f in reversed(tb)
                          if f.filename.endswith(("exercises.py", "reference.py"))), "")
            results.append(CheckResult(exercise, name, "error", f"{type(exc).__name__}: {exc} {where}".strip()))
    return results


def summarize(results: list[CheckResult]) -> dict[str, int]:
    out: dict[str, int] = {"pass": 0, "todo": 0, "fail": 0, "error": 0}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out
