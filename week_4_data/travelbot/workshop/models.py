"""Shared typed contracts.

Four kinds of data live in this workshop, and they deliberately use different types:

* Task state (``TaskState``): requirements, user decisions, observations, the current
  proposal, and pending work. Owned by the application.
* World state: inventory and quote revisions. Owned by the mock tools in ``tools.py``.
  The ``Quote`` model below is what the world hands out; it is a copy, not the world.
* Changes (``Change``): normalized, append-only facts about what happened.
* Planner I/O (``ModelInput``, ``PlannerAction``): what the model sees and returns.

Money is always integer cents. Times of day are "HH:MM" strings in the destination
time zone (America/Chicago). Instants are timezone-aware datetimes from the fixture clock.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def dollars(cents: int | None) -> str:
    if cents is None:
        return "unknown"
    return f"${cents / 100:,.0f}" if cents % 100 == 0 else f"${cents / 100:,.2f}"


# --------------------------------------------------------------------------------------
# Requirements
# --------------------------------------------------------------------------------------


class Party(Model):
    adults: int = Field(ge=1)
    children: int = Field(default=0, ge=0)

    @property
    def seats(self) -> int:
        """Every traveler, child included, needs a flight seat."""
        return self.adults + self.children

    @property
    def occupancy(self) -> int:
        """Every traveler, child included, counts toward hotel occupancy."""
        return self.adults + self.children

    def label(self) -> str:
        parts = [f"{self.adults} adult" + ("s" if self.adults != 1 else "")]
        if self.children:
            parts.append(f"{self.children} child" + ("ren" if self.children != 1 else ""))
        return ", ".join(parts)

    def code(self) -> str:
        return f"{self.adults}a{self.children}c"


class Budget(Model):
    """Hard constraint on flights + hotel only. Rule: total must not exceed max_total_cents."""

    max_total_cents: int
    currency: str = "USD"
    rule: Literal["total_at_most"] = "total_at_most"


class WalkPreference(Model):
    """Preference, not a hard constraint."""

    max_minutes: int
    venue: str


class EarlierArrivalPreference(Model):
    """Preference for an outbound arrival in Chicago strictly before a fixed reference.

    The reference is the flight the user was looking at when they asked. It does not move
    forward when the agent proposes a new flight.
    """

    reference_proposal_id: str
    reference_flight_id: str
    reference_arrival_local: str
    direction: Literal["outbound_arrival"] = "outbound_arrival"
    comparison: Literal["strictly_before"] = "strictly_before"
    time_zone: str = "America/Chicago"
    requested_by_change_id: str


class Requirements(Model):
    revision: int = 1
    origin: str | None
    destination: str | None
    depart_date: date | None
    return_date: date | None
    nights: int | None
    party: Party | None
    budget: Budget | None
    walk_preference: WalkPreference | None = None
    earlier_arrival: EarlierArrivalPreference | None = None

    def missing_fields(self) -> list[str]:
        """Required inputs that are unknown. The planner should ask rather than guess."""
        required = ["origin", "destination", "depart_date", "return_date", "nights", "party", "budget"]
        return [name for name in required if getattr(self, name) is None]


# --------------------------------------------------------------------------------------
# User decisions
# --------------------------------------------------------------------------------------


class DecisionStatus(StrEnum):
    ACCEPTED = "accepted"
    NEEDS_RECONFIRMATION = "needs_reconfirmation"
    NO_LONGER_SUITABLE = "no_longer_suitable"


class HotelDecision(Model):
    """The user's acceptance of a hotel selection in the planning workflow.

    It is not a booking instruction. Accepting a hotel at one price is not acceptance of
    any future price.
    """

    hotel_id: str
    accepted_from_proposal_id: str
    accepted_quote_id: str
    accepted_total_cents: int | None
    status: DecisionStatus = DecisionStatus.ACCEPTED
    requires_reconfirmation: bool = False
    reason: str | None = None
    accepted_by_change_id: str


class Decisions(Model):
    hotel: HotelDecision | None = None


# --------------------------------------------------------------------------------------
# Quotes and observations
# --------------------------------------------------------------------------------------


class QuoteScope(Model):
    adults: int
    children: int
    depart_date: date
    return_date: date

    def fingerprint(self) -> str:
        return f"{self.adults}a{self.children}c:{self.depart_date}..{self.return_date}"

    def matches(self, party: Party | None, depart: date | None, ret: date | None) -> bool:
        return (
            party is not None
            and self.adults == party.adults
            and self.children == party.children
            and self.depart_date == depart
            and self.return_date == ret
        )


class Quote(Model):
    """A priced, scoped observation issued by the world. Immutable once issued."""

    quote_id: str
    item_id: str
    item_kind: Literal["flight", "hotel"]
    label: str
    inventory_revision: int
    scope: QuoteScope
    pricing_basis: Literal["per_traveler", "whole_stay"]
    unit_amount_cents: int
    total_cents: int
    currency: str
    observed_at: datetime
    expires_at: datetime
    tool_call_id: str
    # Eligibility facts.
    arrive_local: str | None = None
    stops: int | None = None
    connection: str | None = None
    seats_available: int | None = None
    capacity: int | None = None
    walk_minutes: int | None = None


class QuoteStatus(StrEnum):
    CURRENT = "current"
    STALE = "stale"  # scope no longer matches current requirements
    EXPIRED = "expired"  # the world says the price no longer holds
    SUPERSEDED = "superseded"  # a newer quote for the same item and scope exists


class ObservedQuote(Model):
    quote: Quote
    status: QuoteStatus = QuoteStatus.CURRENT
    status_reason: str | None = None
    observed_seq: int
    status_changed_by: str | None = None


class ItemNote(Model):
    """Why an item is unsuitable. Kept so the reason is not erased."""

    item_id: str
    reason: str
    since_change_id: str
    requirement_revision: int


class SearchRecord(Model):
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    seq: int


class HistoryNote(Model):
    """A historical fact that is still true but may no longer apply."""

    change_id: str
    seq: int
    text: str


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


class ReasonCode(StrEnum):
    OVER_BUDGET = "over_budget"
    INSUFFICIENT_CAPACITY = "insufficient_capacity"
    INSUFFICIENT_SEATS = "insufficient_seats"
    ITEM_UNAVAILABLE = "item_unavailable"
    DATE_MISMATCH = "date_mismatch"
    QUOTE_UNKNOWN = "quote_unknown"
    QUOTE_EXPIRED = "quote_expired"
    QUOTE_SCOPE_MISMATCH = "quote_scope_mismatch"
    WRONG_ITEM_KIND = "wrong_item_kind"
    ARRIVAL_PREFERENCE_UNMET = "arrival_preference_unmet"
    WALK_PREFERENCE_UNMET = "walk_preference_unmet"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CLAIMED_TOTAL_MISMATCH = "claimed_total_mismatch"


ReasonCategory = Literal["hard_constraint", "quote_validity", "preference", "confirmation", "claim"]

REASON_CATEGORY: dict[ReasonCode, ReasonCategory] = {
    ReasonCode.OVER_BUDGET: "hard_constraint",
    ReasonCode.INSUFFICIENT_CAPACITY: "hard_constraint",
    ReasonCode.INSUFFICIENT_SEATS: "hard_constraint",
    ReasonCode.ITEM_UNAVAILABLE: "hard_constraint",
    ReasonCode.DATE_MISMATCH: "hard_constraint",
    ReasonCode.WRONG_ITEM_KIND: "hard_constraint",
    ReasonCode.QUOTE_UNKNOWN: "quote_validity",
    ReasonCode.QUOTE_EXPIRED: "quote_validity",
    ReasonCode.QUOTE_SCOPE_MISMATCH: "quote_validity",
    ReasonCode.ARRIVAL_PREFERENCE_UNMET: "preference",
    ReasonCode.WALK_PREFERENCE_UNMET: "preference",
    ReasonCode.CONFIRMATION_REQUIRED: "confirmation",
    ReasonCode.CLAIMED_TOTAL_MISMATCH: "claim",
}


class Reason(Model):
    code: ReasonCode
    category: ReasonCategory
    message: str


class ValidationResult(Model):
    """Output of the independent validator. Four separate questions, four separate fields."""

    flight_quote_id: str
    hotel_quote_id: str
    flight_id: str | None
    hotel_id: str | None
    computed_total_cents: int | None
    claimed_total_cents: int | None
    hard_constraints_ok: bool
    quotes_fresh: bool
    preferences_met: bool
    confirmation_required: bool
    reasons: list[Reason]
    checked_at: datetime
    requirement_revision: int

    @property
    def compliant(self) -> bool:
        """Safe to present as compliant: hard constraints hold and every quote is valid."""
        return self.hard_constraints_ok and self.quotes_fresh

    def codes(self) -> list[str]:
        return [r.code.value for r in self.reasons]


class ValidationRecord(Model):
    tool_call_id: str
    seq: int
    result: ValidationResult


# --------------------------------------------------------------------------------------
# Proposals
# --------------------------------------------------------------------------------------


ConfirmationStatus = Literal["pending_user_review", "reconfirmation_required"]


class Proposal(Model):
    proposal_id: str
    turn: int
    flight_id: str
    hotel_id: str
    flight_quote_id: str
    hotel_quote_id: str
    requirement_revision: int
    computed_total_cents: int | None
    claimed_total_cents: int | None
    validation: ValidationResult
    confirmation_status: ConfirmationStatus
    explanation: str
    presented_at: datetime
    status: Literal["current", "needs_recheck", "superseded"] = "current"
    status_reason: str | None = None


# --------------------------------------------------------------------------------------
# Pending work
# --------------------------------------------------------------------------------------


class TaskKind(StrEnum):
    REFRESH_FLIGHT_QUOTE = "refresh_flight_quote"
    FIND_HOTEL_CAPACITY = "find_hotel_capacity"
    SEARCH_EARLIER_FLIGHT = "search_earlier_flight"
    REFRESH_HOTEL_QUOTE = "refresh_hotel_quote"
    REVALIDATE_PLAN = "revalidate_plan"
    CONFIRM_WITH_USER = "confirm_with_user"


class PendingTask(Model):
    task_id: str
    kind: TaskKind
    target: str | None = None
    reason: str
    opened_by_change_id: str
    opened_seq: int
    status: Literal["open", "done"] = "open"
    closed_by: str | None = None


# --------------------------------------------------------------------------------------
# Task state
# --------------------------------------------------------------------------------------


class TaskState(Model):
    run_id: str
    revision: int = 0
    last_seq: int = 0
    requirements: Requirements
    decisions: Decisions = Field(default_factory=Decisions)
    quotes: dict[str, ObservedQuote] = Field(default_factory=dict)
    unsuitable_items: dict[str, ItemNote] = Field(default_factory=dict)
    searches: list[SearchRecord] = Field(default_factory=list)
    validations: list[ValidationRecord] = Field(default_factory=list)
    proposal: Proposal | None = None
    proposal_history: list[Proposal] = Field(default_factory=list)
    pending: list[PendingTask] = Field(default_factory=list)
    open_question: str | None = None
    notes: list[HistoryNote] = Field(default_factory=list)
    applied_change_ids: list[str] = Field(default_factory=list)
    last_input_seq: int = 0  # seq of the latest user or world change


# --------------------------------------------------------------------------------------
# Changes
# --------------------------------------------------------------------------------------


class PartySizeChanged(Model):
    type: Literal["PartySizeChanged"] = "PartySizeChanged"
    adults: int
    children: int


class HotelAccepted(Model):
    type: Literal["HotelAccepted"] = "HotelAccepted"
    hotel_id: str
    proposal_id: str
    quote_id: str


class EarlierFlightRequested(Model):
    type: Literal["EarlierFlightRequested"] = "EarlierFlightRequested"
    reference_proposal_id: str
    reference_flight_id: str
    reference_arrival_local: str


class QuoteExpired(Model):
    type: Literal["QuoteExpired"] = "QuoteExpired"
    quote_id: str
    item_id: str


class ToolObservationReceived(Model):
    type: Literal["ToolObservationReceived"] = "ToolObservationReceived"
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    payload: dict[str, Any]


class ProposalPresented(Model):
    type: Literal["ProposalPresented"] = "ProposalPresented"
    proposal: Proposal


class QuestionAsked(Model):
    type: Literal["QuestionAsked"] = "QuestionAsked"
    question: str
    unresolved_constraint: str


ChangeEvent = Annotated[
    Union[
        PartySizeChanged,
        HotelAccepted,
        EarlierFlightRequested,
        QuoteExpired,
        ToolObservationReceived,
        ProposalPresented,
        QuestionAsked,
    ],
    Field(discriminator="type"),
]

USER_OR_WORLD_EVENTS = ("PartySizeChanged", "HotelAccepted", "EarlierFlightRequested", "QuoteExpired")


class Change(Model):
    """Envelope for every normalized change. change_id is unique; seq increases monotonically."""

    change_id: str
    seq: int
    turn: int
    source: Literal["user", "world", "tool", "agent"]
    message_id: str | None = None
    text: str | None = None
    at: datetime
    event: ChangeEvent

    def fingerprint(self) -> str:
        body = json.dumps(self.event.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()


# --------------------------------------------------------------------------------------
# Context and planner I/O
# --------------------------------------------------------------------------------------


class PlanningStep(Model):
    """Why the runner is asking for a decision now. It is a purpose, not an answer key."""

    purpose: Literal["respond_to_change", "reconsider_after_tool_result", "reconsider_after_rejection"]
    turn: int
    step: int
    trigger: str
    tool_calls_remaining: int


class ContextSection(Model):
    name: str
    temporal: Literal["instructions", "current", "historical"]
    content: Any


class AgentContext(Model):
    purpose: PlanningStep
    sections: list[ContextSection]

    def section(self, name: str) -> ContextSection | None:
        return next((s for s in self.sections if s.name == name), None)


class ToolSpec(Model):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolCallRecord(Model):
    id: str
    name: str
    arguments: dict[str, Any]


class TranscriptEntry(Model):
    index: int = -1
    turn: int
    role: Literal["user", "assistant", "tool", "notification"]
    content: str
    tool_call: ToolCallRecord | None = None
    tool_call_id: str | None = None
    normalized: dict[str, Any] | None = None
    provider_raw: dict[str, Any] | None = None  # exact assistant output, replayed verbatim


class ModelInput(Model):
    implementation: Literal["one-shot", "transcript", "state"]
    system: str
    messages: list[TranscriptEntry]
    tools: list[ToolSpec]
    context: AgentContext | None = None


class PlanDraft(Model):
    flight_quote_id: str
    hotel_quote_id: str
    claimed_total_cents: int | None = None


class CallTool(Model):
    kind: Literal["call_tool"] = "call_tool"
    name: str
    arguments: dict[str, Any]


class PresentPlan(Model):
    kind: Literal["present_plan"] = "present_plan"
    plan: PlanDraft
    explanation: str


class AskUser(Model):
    kind: Literal["ask_user"] = "ask_user"
    question: str
    unresolved_constraint: str


PlannerAction = Annotated[Union[CallTool, PresentPlan, AskUser], Field(discriminator="kind")]


class Usage(Model):
    input_tokens: int | None = None
    output_tokens: int | None = None


class PlannerResponse(Model):
    action: PlannerAction | None
    action_call_id: str
    usage: Usage | None = None
    latency_ms: float | None = None
    attempts: int = 1
    errors: list[str] = Field(default_factory=list)
    transcript_additions: list[TranscriptEntry] = Field(default_factory=list)
    provider_request: Any = None
