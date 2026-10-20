"""Supplied update helpers for TaskState.

Students call these from ``exercises.apply_change``. They do the bookkeeping (IDs, sequence
numbers, de-duplication) so the exercise can focus on deciding *what* each change affects.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from .models import (
    USER_OR_WORLD_EVENTS,
    Budget,
    Change,
    DecisionStatus,
    HistoryNote,
    ItemNote,
    ObservedQuote,
    Party,
    PendingTask,
    Proposal,
    Quote,
    QuoteStatus,
    Requirements,
    SearchRecord,
    TaskKind,
    TaskState,
    ValidationRecord,
    ValidationResult,
    WalkPreference,
)


def initial_state(run_id: str, requirements: dict[str, Any]) -> TaskState:
    req = Requirements(
        origin=requirements.get("origin"),
        destination=requirements.get("destination"),
        depart_date=date.fromisoformat(requirements["depart_date"]) if requirements.get("depart_date") else None,
        return_date=date.fromisoformat(requirements["return_date"]) if requirements.get("return_date") else None,
        nights=requirements.get("nights"),
        party=Party(**requirements["party"]) if requirements.get("party") else None,
        budget=Budget(**requirements["budget"]) if requirements.get("budget") else None,
        walk_preference=WalkPreference(**requirements["walk_preference"]) if requirements.get("walk_preference") else None,
    )
    return TaskState(run_id=run_id, requirements=req)


# -- queries ---------------------------------------------------------------------------


def scope_matches(quote: Quote, requirements: Requirements) -> bool:
    """True when the quote was issued for the current party and dates."""
    return quote.scope.matches(requirements.party, requirements.depart_date, requirements.return_date)


def current_quotes(state: TaskState, kind: str | None = None) -> list[ObservedQuote]:
    return [
        oq
        for oq in state.quotes.values()
        if oq.status == QuoteStatus.CURRENT and (kind is None or oq.quote.item_kind == kind)
    ]


def inactive_quotes(state: TaskState) -> list[ObservedQuote]:
    return [oq for oq in state.quotes.values() if oq.status != QuoteStatus.CURRENT]


def open_tasks(state: TaskState) -> list[PendingTask]:
    return [t for t in state.pending if t.status == "open"]


def proposal_uses_quote(state: TaskState, quote_id: str) -> bool:
    p = state.proposal
    return p is not None and quote_id in (p.flight_quote_id, p.hotel_quote_id)


def validations_since_last_input(state: TaskState) -> list[ValidationRecord]:
    return [v for v in state.validations if v.seq > state.last_input_seq]


# -- updates ---------------------------------------------------------------------------


def note(state: TaskState, change: Change, text: str) -> None:
    """Record a historical fact. Historical facts stay true even when they stop applying."""
    state.notes.append(HistoryNote(change_id=change.change_id, seq=change.seq, text=text))


def mark_quote(state: TaskState, quote_id: str, status: QuoteStatus, reason: str, change: Change) -> None:
    """Change a quote's status without deleting it. Old quotes stay available for comparison."""
    oq = state.quotes.get(quote_id)
    if oq is None or oq.status == status:
        return
    oq.status = status
    oq.status_reason = reason
    oq.status_changed_by = change.change_id


def mark_unsuitable(state: TaskState, item_id: str, reason: str, change: Change) -> None:
    state.unsuitable_items[item_id] = ItemNote(
        item_id=item_id, reason=reason, since_change_id=change.change_id, requirement_revision=state.requirements.revision
    )


def set_hotel_decision_status(state: TaskState, status: DecisionStatus, reason: str, change: Change) -> None:
    decision = state.decisions.hotel
    if decision is None:
        return
    decision.status = status
    decision.requires_reconfirmation = status == DecisionStatus.NEEDS_RECONFIRMATION
    decision.reason = reason
    note(state, change, f"Hotel decision for {decision.hotel_id} is now {status.value}: {reason}")


def flag_proposal(state: TaskState, reason: str) -> None:
    """Mark the current proposal as needing a recheck. Its stored total is no longer trusted."""
    if state.proposal is not None and state.proposal.status == "current":
        state.proposal.status = "needs_recheck"
        state.proposal.status_reason = reason


def add_task(state: TaskState, kind: TaskKind, target: str | None, reason: str, change: Change) -> None:
    """Open a pending task unless the same kind/target is already open."""
    if any(t.kind == kind and t.target == target for t in open_tasks(state)):
        return
    state.pending.append(
        PendingTask(
            task_id=f"task-{len(state.pending) + 1:03d}",
            kind=kind,
            target=target,
            reason=reason,
            opened_by_change_id=change.change_id,
            opened_seq=change.seq,
        )
    )


def record_tool_observation(state: TaskState, change: Change) -> list[Quote]:
    """Store a tool result in state. Returns quotes that are new to this state.

    New quotes supersede older current quotes for the same item and scope.
    """
    ev = change.event
    assert ev.type == "ToolObservationReceived"
    payload = ev.payload
    if ev.tool_name in ("search_flights", "search_hotels", "refresh_quote"):
        state.searches.append(SearchRecord(tool_call_id=ev.tool_call_id, tool_name=ev.tool_name, arguments=ev.arguments, seq=change.seq))
    if "validation" in payload:
        state.validations.append(
            ValidationRecord(tool_call_id=ev.tool_call_id, seq=change.seq, result=ValidationResult.model_validate(payload["validation"]))
        )
    new_quotes: list[Quote] = []
    for raw in payload.get("quotes", []):
        quote = Quote.model_validate(raw)
        existing = state.quotes.get(quote.quote_id)
        if existing is not None:
            if existing.status == QuoteStatus.CURRENT:
                existing.observed_seq = change.seq
            continue
        for oq in current_quotes(state):
            if oq.quote.item_id == quote.item_id and oq.quote.scope == quote.scope:
                mark_quote(state, oq.quote.quote_id, QuoteStatus.SUPERSEDED, f"Superseded by {quote.quote_id}", change)
        state.quotes[quote.quote_id] = ObservedQuote(quote=quote, observed_seq=change.seq)
        new_quotes.append(quote)
    return new_quotes


def record_proposal(state: TaskState, proposal: Proposal, change: Change) -> None:
    if state.proposal is not None:
        old = state.proposal.model_copy(update={"status": "superseded", "status_reason": f"Replaced by {proposal.proposal_id}"})
        state.proposal_history.append(old)
    state.proposal = proposal
    state.open_question = None
    for task in open_tasks(state):
        if task.kind == TaskKind.REVALIDATE_PLAN:
            _close(task, change.change_id)
        if task.kind == TaskKind.CONFIRM_WITH_USER and task.target == proposal.hotel_id and proposal.confirmation_status == "reconfirmation_required":
            _close(task, change.change_id)


def record_question(state: TaskState, question: str, change: Change) -> None:
    state.open_question = question
    for task in open_tasks(state):
        if task.kind == TaskKind.CONFIRM_WITH_USER:
            _close(task, change.change_id)


def _close(task: PendingTask, by: str) -> None:
    task.status = "done"
    task.closed_by = by


def reconcile_pending(state: TaskState) -> None:
    """Close open tasks when the state holds evidence that they are done.

    Tasks only close on evidence observed *after* they were opened, so they cannot
    disappear prematurely, and a closed task never reopens on its own.
    """
    party = state.requirements.party
    for task in open_tasks(state):
        after = [oq for oq in current_quotes(state) if oq.observed_seq > task.opened_seq]
        done_by: str | None = None
        if task.kind in (TaskKind.REFRESH_FLIGHT_QUOTE, TaskKind.REFRESH_HOTEL_QUOTE):
            hit = next((oq for oq in after if oq.quote.item_id == task.target), None)
            done_by = hit.quote.tool_call_id if hit else None
        elif task.kind == TaskKind.FIND_HOTEL_CAPACITY:
            hit = next(
                (oq for oq in after if oq.quote.item_kind == "hotel" and party and (oq.quote.capacity or 0) >= party.occupancy),
                None,
            )
            done_by = hit.quote.tool_call_id if hit else None
        elif task.kind == TaskKind.SEARCH_EARLIER_FLIGHT:
            hit = next(
                (s for s in state.searches if s.tool_name == "search_flights" and s.seq > task.opened_seq and s.arguments.get("arrive_before") == task.target),
                None,
            )
            done_by = hit.tool_call_id if hit else None
        elif task.kind == TaskKind.REVALIDATE_PLAN:
            hit = next((v for v in state.validations if v.seq > task.opened_seq), None)
            done_by = hit.tool_call_id if hit else None
        if done_by:
            _close(task, done_by)


def apply_logged(
    state: TaskState, change: Change, apply_change: Callable[[TaskState, Change], TaskState]
) -> tuple[TaskState, bool]:
    """Apply a change exactly once. Identical repeats are no-ops."""
    if change.change_id in state.applied_change_ids:
        return state, False
    new = apply_change(state, change)
    if not isinstance(new, TaskState):
        raise TypeError(f"apply_change must return a TaskState, got {type(new).__name__}")
    new = new.model_copy(deep=True)
    new.applied_change_ids.append(change.change_id)
    new.revision = state.revision + 1
    new.last_seq = change.seq
    if change.event.type in USER_OR_WORLD_EVENTS:
        new.last_input_seq = change.seq
    reconcile_pending(new)
    return new, True
