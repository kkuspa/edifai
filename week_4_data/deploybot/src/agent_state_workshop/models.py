"""Typed data contracts: events, working state, agent context, proposals, gate results.

Every model here is plain data. Behaviour lives in store.py, exercises.py,
planners.py, and policy.py.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
PROJECTION_VERSION = 1
CONTEXT_VERSION = 1
REQUIRED_SUITE = "required"

EventType = Literal[
    "policy.established",
    "session.started",
    "head.changed",
    "test.completed",
    "approval.granted",
    "session.paused",
    "session.resume_requested",
    # Routine activity that carries no decision-relevant facts.
    "file.inspected",
    "file.edited",
    "command.ran",
    "note.recorded",
]

TestResult = Literal["passed", "failed"]
TestStatus = Literal["passed", "failed", "unknown"]
ApprovalStatus = Literal["approved", "missing"]
Action = Literal["report_test_failure", "request_test_run", "request_approval", "deploy"]
ACTIONS: tuple[str, ...] = ("report_test_failure", "request_test_run", "request_approval", "deploy")

# Blocker codes shared by context builders, planners, and the gate.
TESTS_FAILED = "tests_failed"
TESTS_UNKNOWN = "tests_unknown"
APPROVAL_MISSING = "approval_missing_for_target"
HEAD_UNKNOWN = "head_unknown"
BLOCKER_CODES: tuple[str, ...] = (TESTS_FAILED, TESTS_UNKNOWN, APPROVAL_MISSING, HEAD_UNKNOWN)


def canonical_json(data: Any) -> str:
    """Stable JSON used for hashing and equality checks."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_of(data: Any) -> str:
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- events


class Event(BaseModel):
    """One immutable fact in the session's history.

    `sequence` is issued by the repository and is the only ordering key.
    `occurred_at` is for display.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: str
    session_id: str
    sequence: int = Field(ge=1)
    occurred_at: str
    type: EventType
    actor: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _payload_matches_type(self) -> Event:
        model = PAYLOAD_MODELS.get(self.type)
        if model is not None:
            model.model_validate(self.payload)
        return self

    def canonical(self) -> str:
        return canonical_json(self.model_dump(mode="json"))


class _Payload(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


class PolicyEstablished(_Payload):
    policy_id: str
    rule: str


class HeadChanged(_Payload):
    commit: str
    previous_commit: str | None = None
    branch: str | None = None


class TestCompleted(_Payload):
    __test__ = False  # keep pytest from collecting this class

    commit: str
    suite: str
    test_run_id: str
    run_number: int = Field(ge=1)
    result: TestResult


class ApprovalGranted(_Payload):
    approval_id: str
    commit: str
    environment: str
    approved_by: str


class ResumeRequested(_Payload):
    message: str


PAYLOAD_MODELS: dict[str, type[_Payload]] = {
    "policy.established": PolicyEstablished,
    "head.changed": HeadChanged,
    "test.completed": TestCompleted,
    "approval.granted": ApprovalGranted,
    "session.resume_requested": ResumeRequested,
}


def parse_payload(event: Event) -> Any:
    """Return the typed payload for decision-relevant events, else the raw dict."""
    model = PAYLOAD_MODELS.get(event.type)
    return model.model_validate(event.payload) if model else event.payload


# ---------------------------------------------------------------------- working state


class HeadRef(BaseModel):
    commit: str
    source_event_id: str


class TestRecord(BaseModel):
    """The latest completed run of one suite for one commit."""

    __test__ = False

    commit: str
    suite: str
    run_number: int
    result: TestResult
    source_event_id: str


class ApprovalRecord(BaseModel):
    approval_id: str
    commit: str
    environment: str
    approved_by: str
    source_event_id: str


class UserRequest(BaseModel):
    message: str
    source_event_id: str


class WorkingState(BaseModel):
    """Derived, rebuildable view of the event history.

    Lists are kept in a canonical order so that the same history always
    produces the same state hash.
    """

    projection_version: int = PROJECTION_VERSION
    session_id: str
    through_sequence: int = 0
    head: HeadRef | None = None
    tests: list[TestRecord] = Field(default_factory=list)
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    last_user_request: UserRequest | None = None

    @model_validator(mode="after")
    def _canonical_order(self) -> WorkingState:
        self.tests.sort(key=lambda t: (t.commit, t.suite))
        self.approvals.sort(key=lambda a: (a.commit, a.environment, a.approval_id))
        return self

    def canonical_dump(self) -> dict[str, Any]:
        """Re-validate so lists mutated after construction are sorted too."""
        return WorkingState.model_validate(self.model_dump(mode="json")).model_dump(mode="json")

    def state_hash(self) -> str:
        return sha256_of(self.canonical_dump())


# --------------------------------------------------------------------- agent context


class TargetRef(BaseModel):
    commit: str | None
    environment: str
    source_event_id: str | None = None


class TestEvidence(BaseModel):
    """Test evidence as delivered to the planner.

    `commit` and `source_event_id` are None when the fact is unscoped or unknown.
    """

    __test__ = False

    status: TestStatus
    commit: str | None = None
    suite: str | None = None
    run_number: int | None = None
    source_event_id: str | None = None


class ApprovalEvidence(BaseModel):
    status: ApprovalStatus
    approval_id: str | None = None
    commit: str | None = None
    environment: str | None = None
    approved_by: str | None = None
    source_event_id: str | None = None


class AgentContext(BaseModel):
    """Exactly what the planner sees. Saved verbatim as context.json."""

    context_version: int = CONTEXT_VERSION
    session_id: str
    strategy: str
    through_sequence: int
    user_request: str | None = None
    target: TargetRef
    current_test: TestEvidence
    matching_approval: ApprovalEvidence
    blockers: list[str] = Field(default_factory=list)
    historical_tests: list[TestRecord] = Field(default_factory=list)
    historical_approvals: list[ApprovalRecord] = Field(default_factory=list)
    narrative_memory: str | None = None
    evidence_event_ids: list[str] = Field(default_factory=list)

    @field_validator("blockers", "evidence_event_ids")
    @classmethod
    def _dedupe(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


# ------------------------------------------------------------------ proposals & gate


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Action
    commit: str | None
    environment: str
    evidence_event_ids: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    explanation: str = Field(max_length=600)


class AuthoritativeFacts(BaseModel):
    """What the gate read directly from the event store for the proposed target."""

    checked_through_sequence: int
    head_commit: str | None
    head_source_event_id: str | None
    test_result: TestStatus
    test_run_number: int | None
    test_source_event_id: str | None
    approval_id: str | None
    approval_source_event_id: str | None


class GateResult(BaseModel):
    gate_version: int = 1
    action: Action
    commit: str | None
    environment: str
    outcome: Literal["allowed", "blocked"]
    reason_codes: list[str]
    # Blockers the gate found for the proposed target, whatever the action.
    authoritative_blockers: list[str]
    authoritative: AuthoritativeFacts

    @property
    def allowed(self) -> bool:
        return self.outcome == "allowed"


class Receipt(BaseModel):
    receipt_id: str
    session_id: str
    commit: str
    environment: str
    deployed_at: str
    run_id: str
    gate_checked_through_sequence: int
    reused_existing: bool = False
