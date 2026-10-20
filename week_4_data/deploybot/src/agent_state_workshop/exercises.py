"""The three student exercises. This is the only file you need to edit.

Each `todo(...)` call raises ExerciseIncomplete. Replace it with working code.
The runner and `workshop check` reload this file on every run, so you never
need to restart anything (including the GUI server) after an edit.

Run `uv run workshop check --strategy student` to see which parts pass.

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
    todo,
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
            # TODO 1a: set state.head to a HeadRef for payload.commit, citing this event.
            todo("1a: record the current head (HeadRef) and the event that set it")

        elif event.type == "test.completed":
            # TODO 1b: keep one TestRecord per (commit, suite). Replace an existing
            # record only if this run_number is higher. Hint: find_test(state, commit, suite).
            todo("1b: keep the highest run per (commit, suite) as a TestRecord")

        elif event.type == "approval.granted":
            # TODO 1c: append an ApprovalRecord with its commit, environment,
            # approver, approval_id, and source event.
            todo("1c: record each approval with its commit and environment")

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

    # TODO 2a: TestEvidence for the REQUIRED_SUITE result of *this* commit.
    # If there is none, status="unknown" with no source.
    current_test: TestEvidence = todo("2a: select the required-suite result for the current commit")

    # TODO 2b: ApprovalEvidence for an approval of exactly (commit, target_environment).
    # If there is none, status="missing".
    matching_approval: ApprovalEvidence = todo("2b: select an approval for this commit and environment")

    # TODO 2c: list every blocker that applies (HEAD_UNKNOWN, TESTS_FAILED or
    # TESTS_UNKNOWN, APPROVAL_MISSING). Keep all of them, not just the first.
    blockers: list[str] = todo("2c: list every blocker for this target")

    # Provided: evidence for the decision, and the history with scope intact.
    evidence = [target.source_event_id, current_test.source_event_id, matching_approval.source_event_id]
    if matching_approval.status == "missing":
        evidence += [a.source_event_id for a in state.approvals]  # why no approval applies
    return AgentContext(
        session_id=state.session_id,
        strategy="student",
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

    # TODO 3a: workshop.test.status, workshop.test.commit, workshop.test.source_event_id
    # TODO 3b: workshop.approval.status, workshop.approval.id, workshop.approval.source_event_id
    # TODO 3c: workshop.blockers (a list of strings)
    # TODO 3d: span.add_event(EVIDENCE_EVENT, {EVIDENCE_IDS_KEY: [...context.evidence_event_ids]})
    #
    # This function does not raise when incomplete. The runner records which
    # keys are missing, and the trace viewers show a warning.
