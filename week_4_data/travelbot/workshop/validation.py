"""Independent arithmetic and constraint checker.

The validator never reads TaskState or calls student functions. It derives the current
requirements by folding the normalized change log with its own small, deterministic rules
(``Authority``) and reads prices from the world's quote registry. A student therefore cannot
make a proposal pass by editing state, and the checker does not depend on unfinished code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from .models import (
    REASON_CATEGORY,
    Budget,
    Change,
    Party,
    Reason,
    ReasonCode,
    ValidationResult,
    WalkPreference,
    dollars,
)
from .tools import World


@dataclass
class AcceptedHotel:
    hotel_id: str
    quote_id: str
    proposal_id: str
    accepted_total_cents: int | None


@dataclass
class Authority:
    """Requirements as the application of record understands them."""

    origin: str | None
    destination: str | None
    depart_date: date | None
    return_date: date | None
    party: Party | None
    budget: Budget | None
    walk: WalkPreference | None
    earlier_reference_arrival: str | None = None
    earlier_reference_proposal_id: str | None = None
    accepted_hotel: AcceptedHotel | None = None
    revision: int = 1
    applied: list[str] = field(default_factory=list)

    @classmethod
    def from_request(cls, requirements: dict[str, Any]) -> "Authority":
        return cls(
            origin=requirements.get("origin"),
            destination=requirements.get("destination"),
            depart_date=date.fromisoformat(requirements["depart_date"]) if requirements.get("depart_date") else None,
            return_date=date.fromisoformat(requirements["return_date"]) if requirements.get("return_date") else None,
            party=Party(**requirements["party"]) if requirements.get("party") else None,
            budget=Budget(**requirements["budget"]) if requirements.get("budget") else None,
            walk=WalkPreference(**requirements["walk_preference"]) if requirements.get("walk_preference") else None,
        )

    def fold(self, change: Change, world: World) -> None:
        if change.change_id in self.applied:
            return
        self.applied.append(change.change_id)
        ev = change.event
        if ev.type == "PartySizeChanged":
            self.party = Party(adults=ev.adults, children=ev.children)
            self.revision += 1
        elif ev.type == "EarlierFlightRequested":
            self.earlier_reference_arrival = ev.reference_arrival_local
            self.earlier_reference_proposal_id = ev.reference_proposal_id
            self.revision += 1
        elif ev.type == "HotelAccepted":
            quote = world.quotes.get(ev.quote_id)
            self.accepted_hotel = AcceptedHotel(ev.hotel_id, ev.quote_id, ev.proposal_id, quote.total_cents if quote else None)

    @classmethod
    def from_changes(cls, requirements: dict[str, Any], changes: Iterable[Change], world: World) -> "Authority":
        authority = cls.from_request(requirements)
        for change in changes:
            authority.fold(change, world)
        return authority


def _reason(code: ReasonCode, message: str) -> Reason:
    return Reason(code=code, category=REASON_CATEGORY[code], message=message)


def validate_plan(
    world: World, authority: Authority, flight_quote_id: str, hotel_quote_id: str, claimed_total_cents: int | None = None
) -> ValidationResult:
    reasons: list[Reason] = []
    fq = world.quotes.get(flight_quote_id)
    hq = world.quotes.get(hotel_quote_id)
    if fq is None:
        reasons.append(_reason(ReasonCode.QUOTE_UNKNOWN, f"No quote {flight_quote_id!r} was ever issued."))
    if hq is None:
        reasons.append(_reason(ReasonCode.QUOTE_UNKNOWN, f"No quote {hotel_quote_id!r} was ever issued."))
    if fq is not None and fq.item_kind != "flight":
        reasons.append(_reason(ReasonCode.WRONG_ITEM_KIND, f"{flight_quote_id} is a {fq.item_kind} quote, not a flight quote."))
    if hq is not None and hq.item_kind != "hotel":
        reasons.append(_reason(ReasonCode.WRONG_ITEM_KIND, f"{hotel_quote_id} is a {hq.item_kind} quote, not a hotel quote."))

    party = authority.party
    computed: int | None = None
    if fq is not None and hq is not None and fq.item_kind == "flight" and hq.item_kind == "hotel":
        # Recompute from authoritative pricing basis, never from a model claim.
        flight_total = fq.unit_amount_cents * (fq.scope.adults + fq.scope.children)
        hotel_total = hq.total_cents
        computed = flight_total + hotel_total

        for q in (fq, hq):
            if world.quote_expired(q):
                reasons.append(_reason(ReasonCode.QUOTE_EXPIRED, f"{q.quote_id} is no longer valid at {world.clock.now.isoformat()}."))
            if not q.scope.matches(party, authority.depart_date, authority.return_date):
                reasons.append(
                    _reason(
                        ReasonCode.QUOTE_SCOPE_MISMATCH,
                        f"{q.quote_id} was issued for {q.scope.fingerprint()}; the trip is now "
                        f"{party.code() if party else 'unknown party'}:{authority.depart_date}..{authority.return_date}.",
                    )
                )

        flight = world.flight(fq.item_id)
        hotel = world.hotel(hq.item_id)
        if party is not None and flight is not None and flight["seats_available"] < party.seats:
            reasons.append(_reason(ReasonCode.INSUFFICIENT_SEATS, f"{fq.item_id} has {flight['seats_available']} seats; party needs {party.seats}."))
        if party is not None and hotel is not None and hotel["capacity"] < party.occupancy:
            reasons.append(_reason(ReasonCode.INSUFFICIENT_CAPACITY, f"{hq.item_id} sleeps {hotel['capacity']}; party is {party.occupancy}."))
        if hotel is not None and not hotel["available"]:
            reasons.append(_reason(ReasonCode.ITEM_UNAVAILABLE, f"{hq.item_id} is sold out."))
        if flight is not None and (
            flight["depart_date"] != str(authority.depart_date) or flight["return_date"] != str(authority.return_date)
        ):
            reasons.append(_reason(ReasonCode.DATE_MISMATCH, f"{fq.item_id} flies {flight['depart_date']}..{flight['return_date']}."))
        if authority.budget is not None and computed > authority.budget.max_total_cents:
            reasons.append(
                _reason(ReasonCode.OVER_BUDGET, f"Total {dollars(computed)} exceeds the {dollars(authority.budget.max_total_cents)} budget.")
            )

        if authority.earlier_reference_arrival and fq.arrive_local and not fq.arrive_local < authority.earlier_reference_arrival:
            reasons.append(
                _reason(
                    ReasonCode.ARRIVAL_PREFERENCE_UNMET,
                    f"{fq.item_id} arrives {fq.arrive_local}; the user asked for earlier than "
                    f"{authority.earlier_reference_arrival} (from {authority.earlier_reference_proposal_id}).",
                )
            )
        if authority.walk and hq.walk_minutes is not None and hq.walk_minutes > authority.walk.max_minutes:
            reasons.append(
                _reason(ReasonCode.WALK_PREFERENCE_UNMET, f"{hq.item_id} is a {hq.walk_minutes}-minute walk; preference is {authority.walk.max_minutes}.")
            )

        accepted = authority.accepted_hotel
        if accepted is not None:
            if hq.item_id != accepted.hotel_id:
                reasons.append(
                    _reason(ReasonCode.CONFIRMATION_REQUIRED, f"Replaces the hotel the user accepted ({accepted.hotel_id}).")
                )
            elif accepted.accepted_total_cents is not None and hq.total_cents != accepted.accepted_total_cents:
                reasons.append(
                    _reason(
                        ReasonCode.CONFIRMATION_REQUIRED,
                        f"{hq.item_id} now costs {dollars(hq.total_cents)}; the user accepted it at "
                        f"{dollars(accepted.accepted_total_cents)}.",
                    )
                )

        if claimed_total_cents is not None and claimed_total_cents != computed:
            reasons.append(
                _reason(
                    ReasonCode.CLAIMED_TOTAL_MISMATCH,
                    f"Claimed {dollars(claimed_total_cents)}; computed {dollars(computed)}. The computed total is authoritative.",
                )
            )

    categories = {r.category for r in reasons}
    return ValidationResult(
        flight_quote_id=flight_quote_id,
        hotel_quote_id=hotel_quote_id,
        flight_id=fq.item_id if fq else None,
        hotel_id=hq.item_id if hq else None,
        computed_total_cents=computed,
        claimed_total_cents=claimed_total_cents,
        hard_constraints_ok="hard_constraint" not in categories and computed is not None,
        quotes_fresh="quote_validity" not in categories,
        preferences_met="preference" not in categories,
        confirmation_required="confirmation" in categories,
        reasons=reasons,
        checked_at=world.clock.now,
        requirement_revision=authority.revision,
    )
