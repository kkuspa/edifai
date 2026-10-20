"""Reference implementations of the two student functions.

Instructors: run with ``--strategy reference``. Students: try the exercise first.
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
    s = state.model_copy(deep=True)
    ev = change.event

    if ev.type == "PartySizeChanged":
        old, new = s.requirements.party, Party(adults=ev.adults, children=ev.children)
        if old == new:
            return s
        # Only the party changes. Destination, dates, budget, and walking preference carry over.
        s.requirements = s.requirements.model_copy(update={"party": new, "revision": s.requirements.revision + 1})
        note(s, change, f"Party changed from {old.label() if old else 'unknown'} to {new.label()}.")
        # Quotes are party-scoped: keep them on record, but they no longer price this trip.
        for oq in current_quotes(s):
            if not scope_matches(oq.quote, s.requirements):
                mark_quote(s, oq.quote.quote_id, QuoteStatus.STALE, f"Issued for {oq.quote.scope.fingerprint()}; party is now {new.code()}.", change)
        for oq in s.quotes.values():
            if oq.quote.item_kind == "hotel" and (oq.quote.capacity or 0) < new.occupancy:
                mark_unsuitable(s, oq.quote.item_id, f"Capacity {oq.quote.capacity} < occupancy {new.occupancy}.", change)
        if s.decisions.hotel is not None:
            if s.decisions.hotel.hotel_id in s.unsuitable_items:
                set_hotel_decision_status(s, DecisionStatus.NO_LONGER_SUITABLE, "Room is too small for the new party.", change)
            else:
                set_hotel_decision_status(s, DecisionStatus.NEEDS_RECONFIRMATION, "Price must be requoted for the new party.", change)
        if s.proposal is not None:
            flag_proposal(s, "Party changed; the stored total priced a different party.")
            add_task(s, TaskKind.REFRESH_FLIGHT_QUOTE, s.proposal.flight_id, "Requote flights for the new party.", change)
        add_task(s, TaskKind.FIND_HOTEL_CAPACITY, None, f"Find a room for {new.occupancy}.", change)
        add_task(s, TaskKind.REVALIDATE_PLAN, None, "Recalculate and validate the combination.", change)

    elif ev.type == "HotelAccepted":
        accepted = s.quotes.get(ev.quote_id)
        s.decisions.hotel = HotelDecision(
            hotel_id=ev.hotel_id,
            accepted_from_proposal_id=ev.proposal_id,
            accepted_quote_id=ev.quote_id,
            accepted_total_cents=accepted.quote.total_cents if accepted else None,
            accepted_by_change_id=change.change_id,
        )
        note(s, change, f"User accepted hotel {ev.hotel_id} from {ev.proposal_id} at {dollars(s.decisions.hotel.accepted_total_cents)}.")

    elif ev.type == "EarlierFlightRequested":
        # Anchor "earlier" to the flight the user was looking at. Later proposals never move it.
        pref = EarlierArrivalPreference(
            reference_proposal_id=ev.reference_proposal_id,
            reference_flight_id=ev.reference_flight_id,
            reference_arrival_local=ev.reference_arrival_local,
            requested_by_change_id=change.change_id,
        )
        s.requirements = s.requirements.model_copy(update={"earlier_arrival": pref, "revision": s.requirements.revision + 1})
        flag_proposal(s, "User asked for an earlier outbound arrival.")
        add_task(s, TaskKind.SEARCH_EARLIER_FLIGHT, ev.reference_arrival_local, f"Find an outbound arrival before {ev.reference_arrival_local}.", change)
        add_task(s, TaskKind.REVALIDATE_PLAN, None, "Validate the revised combination.", change)

    elif ev.type == "QuoteExpired":
        oq = s.quotes.get(ev.quote_id)
        mark_quote(s, ev.quote_id, QuoteStatus.EXPIRED, "Inventory notice: quote expired.", change)
        if proposal_uses_quote(s, ev.quote_id):
            flag_proposal(s, f"{ev.quote_id} expired; the total must be recomputed.")
            add_task(s, TaskKind.REVALIDATE_PLAN, None, "Recalculate with a fresh quote.", change)
        hotel = s.decisions.hotel
        relevant = (hotel is not None and hotel.hotel_id == ev.item_id) or proposal_uses_quote(s, ev.quote_id)
        if relevant:
            # The user's choice of hotel survives; only the price evidence is gone.
            kind = TaskKind.REFRESH_HOTEL_QUOTE if (oq is None or oq.quote.item_kind == "hotel") else TaskKind.REFRESH_FLIGHT_QUOTE
            add_task(s, kind, ev.item_id, f"{ev.quote_id} expired.", change)
            note(s, change, f"Quote {ev.quote_id} expired; selection of {ev.item_id} is unchanged.")

    elif ev.type == "ToolObservationReceived":
        for quote in record_tool_observation(s, change):
            hotel = s.decisions.hotel
            if (
                hotel is not None
                and quote.item_id == hotel.hotel_id
                and scope_matches(quote, s.requirements)
                and hotel.accepted_total_cents is not None
                and quote.total_cents != hotel.accepted_total_cents
            ):
                set_hotel_decision_status(
                    s,
                    DecisionStatus.NEEDS_RECONFIRMATION,
                    f"Price changed from {dollars(hotel.accepted_total_cents)} to {dollars(quote.total_cents)} since acceptance.",
                    change,
                )
                flag_proposal(s, "Accepted hotel repriced.")
                add_task(s, TaskKind.CONFIRM_WITH_USER, hotel.hotel_id, "Accepted hotel price changed materially.", change)

    elif ev.type == "ProposalPresented":
        record_proposal(s, ev.proposal, change)

    elif ev.type == "QuestionAsked":
        record_question(s, ev.question, change)

    return s


def build_context(state: TaskState, next_step: PlanningStep) -> AgentContext:
    req = state.requirements
    current = current_quotes(state)
    sections = [
        section("purpose", "instructions", purpose_view(next_step)),
        section("requirements", "current", requirements_view(req)),
        section("unknowns", "current", req.missing_fields()),
        section("decisions", "current", decisions_view(state.decisions)),
        section("current_quotes", "current", [quote_view(oq) for oq in current]),
        section("unsuitable_items", "current", [n.model_dump() for n in state.unsuitable_items.values()]),
        section("current_proposal", "current", proposal_view(state.proposal)),
        section("pending_work", "current", [task_view(t) for t in open_tasks(state)]),
        section("validation_feedback", "current", validation_feedback_view(state)),
        section(
            "historical",
            "historical",
            {
                "inactive_quotes": [
                    {"quote_id": oq.quote.quote_id, "status": oq.status.value, "reason": oq.status_reason, "total_cents": oq.quote.total_cents}
                    for oq in inactive_quotes(state)
                ],
                "previous_proposals": [
                    {"proposal_id": p.proposal_id, "flight_id": p.flight_id, "hotel_id": p.hotel_id, "total_cents": p.computed_total_cents}
                    for p in state.proposal_history
                ],
                "notes": [n.text for n in state.notes],
            },
        ),
        section("history_access", "instructions", history_access_view()),
    ]
    return AgentContext(purpose=next_step, sections=sections)
