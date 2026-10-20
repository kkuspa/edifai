"""Reference solution for src/agent_state_workshop/exercises.py.

INSTRUCTOR MATERIAL. Same signatures and provided code as the student file,
with every exercise gap filled in. Run it with `--strategy reference`.

Exercise 1  project_state          retain scope, ordering, and provenance
Exercise 2  build_context          select facts that apply to the current target
Exercise 3  annotate_context_span  attach evidence to the context.build span
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from agent_state_workshop.models import (
    APPROVAL_MISSING,
    HEAD_UNKNOWN,
    REQUIRED_SUITE,
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
    UserRequest,
    WorkingState,
    parse_payload,
)
from agent_state_workshop.support import (
    EVIDENCE_EVENT,
    EVIDENCE_IDS_KEY,
    find_approval,
    find_test,
    ordered_unique,
    session_of,
    set_attr,
)

# Names imported for your convenience; keep them even if unused.
__all__ = [
    "APPROVAL_MISSING", "HEAD_UNKNOWN", "REQUIRED_SUITE", "TESTS_FAILED", "TESTS_UNKNOWN",
    "ApprovalRecord", "HeadRef", "TestRecord", "EVIDENCE_EVENT", "EVIDENCE_IDS_KEY",
]


# --------------------------------------------------------------------------
# Exercise 1: project the event history into a scoped working state.
# --------------------------------------------------------------------------


def project_state(events: Iterable[Event]) -> WorkingState:
    """Replay the full history into a WorkingState.

    Rules (see docs/workshop.md, "Data rules"):
      * A head change sets the current commit. It does not erase older facts.
      * A test result belongs to one commit and one suite. When several runs
        exist for the same (commit, suite), the highest run_number wins,
        whatever order the events arrived in.
      * An approval belongs to one commit and one environment.
      * Every record keeps the event_id it came from (source_event_id).
    """
    ordered = ordered_unique(events)  # sorted by sequence, duplicates removed
    state = WorkingState(session_id=session_of(ordered))

    for event in ordered:
        state.through_sequence = event.sequence
        payload = parse_payload(event)  # typed: HeadChanged, TestCompleted, ApprovalGranted, ...

        if event.type == "head.changed":
            state.head = HeadRef(commit=payload.commit, source_event_id=event.event_id)

        elif event.type == "test.completed":
            record = TestRecord(
                commit=payload.commit,
                suite=payload.suite,
                run_number=payload.run_number,
                result=payload.result,
                source_event_id=event.event_id,
            )
            existing = find_test(state, payload.commit, payload.suite)
            if existing is None:
                state.tests.append(record)
            elif record.run_number > existing.run_number:
                state.tests[state.tests.index(existing)] = record

        elif event.type == "approval.granted":
            state.approvals.append(
                ApprovalRecord(
                    approval_id=payload.approval_id,
                    commit=payload.commit,
                    environment=payload.environment,
                    approved_by=payload.approved_by,
                    source_event_id=event.event_id,
                )
            )

        elif event.type == "session.resume_requested":
            state.last_user_request = UserRequest(message=payload.message, source_event_id=event.event_id)

    return state


# --------------------------------------------------------------------------
# Exercise 2: build the context the planner sees for the current target.
# --------------------------------------------------------------------------


def build_context(state: WorkingState, target_environment: str) -> AgentContext:
    """Select the facts that apply to (current commit, target_environment).

    A fact about another commit or environment stays in the history lists with
    its scope intact; it must never be presented as current evidence. Missing
    facts are reported as unknown/missing, never invented.
    """
    commit = state.head.commit if state.head else None
    target = TargetRef(
        commit=commit,
        environment=target_environment,
        source_event_id=state.head.source_event_id if state.head else None,
    )

    test = find_test(state, commit, REQUIRED_SUITE)
    if test is None:
        current_test = TestEvidence(status="unknown", commit=commit, suite=REQUIRED_SUITE)
    else:
        current_test = TestEvidence(
            status=test.result,
            commit=test.commit,
            suite=test.suite,
            run_number=test.run_number,
            source_event_id=test.source_event_id,
        )

    approval = find_approval(state, commit, target_environment)
    if approval is None:
        matching_approval = ApprovalEvidence(status="missing", commit=commit, environment=target_environment)
    else:
        matching_approval = ApprovalEvidence(
            status="approved",
            approval_id=approval.approval_id,
            commit=approval.commit,
            environment=approval.environment,
            approved_by=approval.approved_by,
            source_event_id=approval.source_event_id,
        )

    blockers: list[str] = []
    if commit is None:
        blockers.append(HEAD_UNKNOWN)
    if current_test.status == "failed":
        blockers.append(TESTS_FAILED)
    elif current_test.status == "unknown":
        blockers.append(TESTS_UNKNOWN)
    if matching_approval.status == "missing":
        blockers.append(APPROVAL_MISSING)

    # Provided: evidence for the decision, and the history with scope intact.
    evidence = [target.source_event_id, current_test.source_event_id, matching_approval.source_event_id]
    if matching_approval.status == "missing":
        evidence += [a.source_event_id for a in state.approvals]  # why no approval applies
    return AgentContext(
        session_id=state.session_id,
        strategy="reference",
        through_sequence=state.through_sequence,
        user_request=state.last_user_request.message if state.last_user_request else None,
        target=target,
        current_test=current_test,
        matching_approval=matching_approval,
        blockers=blockers,
        historical_tests=[t for t in state.tests if t.commit != commit],
        historical_approvals=[a for a in state.approvals if a.commit != commit or a.environment != target_environment],
        evidence_event_ids=[e for e in evidence if e],
    )


# --------------------------------------------------------------------------
# Exercise 3: annotate the context.build span with decision evidence.
# --------------------------------------------------------------------------


def annotate_context_span(span: Any, context: AgentContext) -> None:
    """Attach the facts behind the decision to the active context.build span.

    Use set_attr(span, key, value); it skips None values. The attribute names
    are listed in docs/telemetry-schema.md. Nested objects belong in
    context.json, not in attributes.
    """
    set_attr(span, "workshop.target.commit", context.target.commit)
    set_attr(span, "workshop.target.environment", context.target.environment)
    set_attr(span, "workshop.target.source_event_id", context.target.source_event_id)

    set_attr(span, "workshop.test.status", context.current_test.status)
    set_attr(span, "workshop.test.commit", context.current_test.commit)
    set_attr(span, "workshop.test.source_event_id", context.current_test.source_event_id)
    set_attr(span, "workshop.approval.status", context.matching_approval.status)
    set_attr(span, "workshop.approval.id", context.matching_approval.approval_id)
    set_attr(span, "workshop.approval.source_event_id", context.matching_approval.source_event_id)
    set_attr(span, "workshop.blockers", context.blockers)
    span.add_event(EVIDENCE_EVENT, {EVIDENCE_IDS_KEY: list(context.evidence_event_ids)})
