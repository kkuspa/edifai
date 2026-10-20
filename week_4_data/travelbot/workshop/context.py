"""Supplied formatting and context helpers.

``exercises.build_context`` chooses which sections go into the model input. These helpers
turn typed state into compact, model-readable views and render the final text. The section
names below are the vocabulary the simulator and the report understand; a missing section
is treated as "nothing known".
"""

from __future__ import annotations

import json
from typing import Any

from .models import (
    AgentContext,
    ContextSection,
    Decisions,
    ObservedQuote,
    PendingTask,
    PlanningStep,
    Proposal,
    Requirements,
    TaskState,
    ValidationRecord,
    dollars,
)

SECTION_NAMES = [
    "purpose",
    "requirements",
    "unknowns",
    "decisions",
    "current_quotes",
    "unsuitable_items",
    "current_proposal",
    "pending_work",
    "validation_feedback",
    "historical",
    "history_access",
]


def section(name: str, temporal: str, content: Any) -> ContextSection:
    return ContextSection(name=name, temporal=temporal, content=content)  # type: ignore[arg-type]


def purpose_view(step: PlanningStep) -> dict[str, Any]:
    return {
        "purpose": step.purpose,
        "turn": step.turn,
        "step": step.step,
        "trigger": step.trigger,
        "tool_calls_remaining_this_turn": step.tool_calls_remaining,
    }


def requirements_view(req: Requirements) -> dict[str, Any]:
    return {
        "revision": req.revision,
        "hard_constraints": {
            "origin": req.origin,
            "destination": req.destination,
            "depart_date": str(req.depart_date) if req.depart_date else None,
            "return_date": str(req.return_date) if req.return_date else None,
            "nights": req.nights,
            "party": req.party.model_dump() if req.party else None,
            "seats_needed": req.party.seats if req.party else None,
            "hotel_occupancy": req.party.occupancy if req.party else None,
            "budget": (
                {"max_total_cents": req.budget.max_total_cents, "display": dollars(req.budget.max_total_cents),
                 "rule": "flights + hotel total must not exceed max"}
                if req.budget else None
            ),
        },
        "preferences": {
            "walk": req.walk_preference.model_dump() if req.walk_preference else None,
            "earlier_arrival": (
                {
                    "arrive_before_local": req.earlier_arrival.reference_arrival_local,
                    "reference_proposal_id": req.earlier_arrival.reference_proposal_id,
                    "reference_flight_id": req.earlier_arrival.reference_flight_id,
                    "meaning": "outbound arrival in Chicago strictly before this time; the reference does not move",
                }
                if req.earlier_arrival else None
            ),
        },
    }


def decisions_view(decisions: Decisions) -> dict[str, Any]:
    h = decisions.hotel
    if h is None:
        return {"hotel": None}
    return {
        "hotel": {
            "hotel_id": h.hotel_id,
            "status": h.status.value,
            "requires_reconfirmation": h.requires_reconfirmation,
            "accepted_from_proposal_id": h.accepted_from_proposal_id,
            "accepted_quote_id": h.accepted_quote_id,
            "accepted_total_cents": h.accepted_total_cents,
            "reason": h.reason,
            "scope": "accepted as the hotel selection for planning; not a booking",
        }
    }


def quote_view(oq: ObservedQuote) -> dict[str, Any]:
    q = oq.quote
    view: dict[str, Any] = {
        "quote_id": q.quote_id,
        "item_id": q.item_id,
        "kind": q.item_kind,
        "label": q.label,
        "status": oq.status.value,
        "party": f"{q.scope.adults} adults, {q.scope.children} children",
        "adults": q.scope.adults,
        "children": q.scope.children,
        "dates": f"{q.scope.depart_date}..{q.scope.return_date}",
        "pricing_basis": q.pricing_basis,
        "unit_amount_cents": q.unit_amount_cents,
        "total_cents": q.total_cents,
        "inventory_revision": q.inventory_revision,
        "expires_at": q.expires_at.isoformat(),
    }
    if q.item_kind == "flight":
        view.update(arrive_local=q.arrive_local, stops=q.stops, connection=q.connection, seats_available=q.seats_available)
    else:
        view.update(capacity=q.capacity, walk_minutes=q.walk_minutes)
    if oq.status_reason:
        view["status_reason"] = oq.status_reason
    return view


def proposal_view(p: Proposal | None) -> dict[str, Any] | None:
    if p is None:
        return None
    return {
        "proposal_id": p.proposal_id,
        "status": p.status,
        "status_reason": p.status_reason,
        "flight_id": p.flight_id,
        "hotel_id": p.hotel_id,
        "flight_quote_id": p.flight_quote_id,
        "hotel_quote_id": p.hotel_quote_id,
        "total_cents_when_presented": p.computed_total_cents,
        "requirement_revision": p.requirement_revision,
        "confirmation_status": p.confirmation_status,
    }


def task_view(t: PendingTask) -> dict[str, Any]:
    return {"task_id": t.task_id, "kind": t.kind.value, "target": t.target, "reason": t.reason, "status": t.status}


def validation_view(v: ValidationRecord) -> dict[str, Any]:
    r = v.result
    return {
        "tool_call_id": v.tool_call_id,
        "flight_quote_id": r.flight_quote_id,
        "hotel_quote_id": r.hotel_quote_id,
        "computed_total_cents": r.computed_total_cents,
        "compliant": r.compliant,
        "hard_constraints_ok": r.hard_constraints_ok,
        "quotes_fresh": r.quotes_fresh,
        "preferences_met": r.preferences_met,
        "confirmation_required": r.confirmation_required,
        "reasons": [{"code": x.code.value, "category": x.category, "message": x.message} for x in r.reasons],
    }


def validation_feedback_view(state: TaskState) -> list[dict[str, Any]]:
    """Validator results since the latest user or world change (this turn's feedback)."""
    return [validation_view(v) for v in state.validations if v.seq > state.last_input_seq]


def history_access_view() -> dict[str, Any]:
    return {
        "note": "The full conversation is retained. Call read_history(turn, role, contains, limit) for exact wording.",
    }


def render_context(ctx: AgentContext) -> str:
    """Render the context as the single user message the state-driven agent receives."""
    lines = [
        "You are choosing the next action for an ongoing travel-planning task.",
        "Sections marked CURRENT describe what is true now. Sections marked HISTORICAL are kept for reference only.",
        "",
    ]
    for s in ctx.sections:
        lines.append(f"## {s.name} [{s.temporal.upper()}]")
        lines.append(json.dumps(s.content, indent=2, sort_keys=False, default=str))
        lines.append("")
    lines.append("Choose exactly one action by calling one tool.")
    return "\n".join(lines)
