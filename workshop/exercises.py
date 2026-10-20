"""Student exercise: the two functions you implement.

    apply_change(state, change)  decide what a change affects, preserve what it does not
    build_context(state, step)   decide what the model sees before its next action

Everything else is supplied. Run ``uv run workshop check`` after each edit; the GUI and
the CLI both reload this file on every run, so there is nothing to restart.

Useful helpers (see workshop/state.py and workshop/context.py):

    current_quotes(state, kind=None)        quotes whose status is CURRENT
    inactive_quotes(state)                  stale, expired, and superseded quotes
    scope_matches(quote, requirements)      was this quote issued for the current party and dates?
    mark_quote(state, quote_id, status, reason, change)
    mark_unsuitable(state, item_id, reason, change)
    set_hotel_decision_status(state, DecisionStatus.X, reason, change)
    flag_proposal(state, reason)            the stored total is no longer trusted
    add_task(state, TaskKind.X, target, reason, change)
    note(state, change, text)               record a historical fact
    proposal_uses_quote(state, quote_id)

Rules of thumb from the spec: do not clear everything on every change, and do not keep
every old value as if it were current. Decide the scope of each update.
"""

from __future__ import annotations

from workshop.context import (
    decisions_view,
    history_access_view,
    proposal_view,
    purpose_view,
    quote_view,
    requirements_view,
    section,
    task_view,
    validation_feedback_view,
)
from workshop.models import (
    AgentContext,
    Change,
    DecisionStatus,
    EarlierArrivalPreference,
    HotelDecision,
    Party,
    PlanningStep,
    QuoteStatus,
    TaskKind,
    TaskState,
    dollars,
)
from workshop.state import (
    add_task,
    current_quotes,
    flag_proposal,
    inactive_quotes,
    mark_quote,
    mark_unsuitable,
    note,
    open_tasks,
    proposal_uses_quote,
    record_proposal,
    record_question,
    record_tool_observation,
    scope_matches,
    set_hotel_decision_status,
)


def apply_change(state: TaskState, change: Change) -> TaskState:
    """Return the new task state after one normalized change. Do not mutate ``state``.

    The starter version runs, but it is naive on purpose: it only overwrites the party.
    Run the starter scenario first and look at what the agent receives and does.
    """
    s = state.model_copy(deep=True)
    ev = change.event

    if ev.type == "PartySizeChanged":
        # Starter behavior: overwrite the party and nothing else.
        s.requirements.party = Party(adults=ev.adults, children=ev.children)
        # TODO 1: decide what else this change affects.
        #  - Bump s.requirements.revision. Origin, destination, dates, budget, and the
        #    walking preference must survive untouched.
        #  - Quotes are party-scoped. Any CURRENT quote that no longer matches the new
        #    party should become QuoteStatus.STALE (keep it on record; do not delete it).
        #  - Hotels whose capacity is below the new occupancy are unsuitable: record why.
        #  - If a hotel was already accepted, its decision may need a new status.
        #  - The current proposal's total no longer applies (flag_proposal), and work
        #    remains: TaskKind.REFRESH_FLIGHT_QUOTE, FIND_HOTEL_CAPACITY, REVALIDATE_PLAN.

    elif ev.type == "HotelAccepted":
        # TODO 2: store the acceptance as its own decision (s.decisions.hotel = HotelDecision(...))
        # with the hotel, the proposal it came from, the quote, and the accepted total.
        # Acceptance is not a booking and does not cover future prices.
        pass

    elif ev.type == "EarlierFlightRequested":
        # TODO 3: record an EarlierArrivalPreference anchored to the payload's reference
        # (the flight the user was looking at) and bump requirements.revision. Nothing
        # later should move this anchor. Keep the accepted hotel. Flag the proposal and
        # open TaskKind.SEARCH_EARLIER_FLIGHT (target: the reference arrival time) and
        # TaskKind.REVALIDATE_PLAN.
        pass

    elif ev.type == "QuoteExpired":
        # TODO 4: mark the quote EXPIRED. Keep the user's hotel choice. If the current
        # proposal used this quote, flag it and open TaskKind.REVALIDATE_PLAN. If the
        # item matters (accepted hotel or in the proposal), open a refresh task
        # (TaskKind.REFRESH_HOTEL_QUOTE, target: the item ID).
        pass

    elif ev.type == "ToolObservationReceived":
        # Supplied: store the observation. Returns quotes this state has not seen before.
        new_quotes = record_tool_observation(s, change)
        # TODO 5: if a new quote prices the accepted hotel for the current party at a
        # different total than the user accepted, the acceptance needs reconfirmation:
        # set the decision status, flag the proposal, and open TaskKind.CONFIRM_WITH_USER.
        for quote in new_quotes:
            pass

    elif ev.type == "ProposalPresented":
        record_proposal(s, ev.proposal, change)  # supplied

    elif ev.type == "QuestionAsked":
        record_question(s, ev.question, change)  # supplied

    return s


def build_context(state: TaskState, next_step: PlanningStep) -> AgentContext:
    """Assemble what the model sees for its next action.

    Section names the simulator and report understand: requirements, unknowns, decisions,
    current_quotes, unsuitable_items, current_proposal, pending_work, historical.
    Mark each section "current" or "historical".

    The starter version sends the requirements and every quote ever observed, all
    labeled current.
    """
    sections = [
        section("purpose", "instructions", purpose_view(next_step)),  # supplied
        section("requirements", "current", requirements_view(state.requirements)),
        # Starter behavior: every quote, whatever its status, presented as current.
        section("current_quotes", "current", [quote_view(oq) for oq in state.quotes.values()]),
        # TODO 6: decide what the model needs and label it honestly.
        #  - unknowns: state.requirements.missing_fields()
        #  - decisions: decisions_view(state.decisions)
        #  - current_quotes: CURRENT quotes only (current_quotes(state)), and
        #    unsuitable_items with their reasons
        #  - current_proposal: proposal_view(state.proposal)
        #  - pending_work: open tasks (open_tasks + task_view)
        #  - a "historical" section for inactive quotes, previous proposals, and notes,
        #    so history stays available without posing as current
        section("validation_feedback", "current", validation_feedback_view(state)),  # supplied
        section("history_access", "instructions", history_access_view()),  # supplied
    ]
    return AgentContext(purpose=next_step, sections=sections)
