"""World state and the read-only tools that observe it.

The world owns inventory, quote revisions, and the fixture clock. Each run gets its own
World built from the fixture file, so runs never share inventory. Tools only read inventory
and issue quotes; nothing here books, charges, or reserves anything.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from .models import Party, Quote, QuoteScope, ToolSpec

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load_inventory() -> dict[str, Any]:
    return json.loads((FIXTURES / "inventory.json").read_text())


def load_clock_start() -> datetime:
    return datetime.fromisoformat(json.loads((FIXTURES / "clock.json").read_text())["start"])


class FixtureClock:
    """A clock that only moves when the scenario tells it to."""

    def __init__(self, start: datetime, now: datetime | None = None):
        self.start = start
        self.now = now or start

    def set_offset(self, minutes: int) -> None:
        self.now = self.start + timedelta(minutes=minutes)

    def advance(self, minutes: int) -> None:
        self.now = self.now + timedelta(minutes=minutes)


class World:
    def __init__(self, inventory: dict[str, Any] | None = None, clock: FixtureClock | None = None):
        self.inventory = copy.deepcopy(inventory or load_inventory())
        self.clock = clock or FixtureClock(load_clock_start())
        self.revisions: dict[str, int] = {item["flight_id"]: 1 for item in self.inventory["flights"]}
        self.revisions.update({item["hotel_id"]: 1 for item in self.inventory["hotels"]})
        self.quotes: dict[str, Quote] = {}
        self.expired_quote_ids: set[str] = set()
        self.events: list[dict[str, Any]] = []

    # -- persistence ------------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "inventory": self.inventory,
            "clock_start": self.clock.start.isoformat(),
            "clock_now": self.clock.now.isoformat(),
            "revisions": self.revisions,
            "quotes": {k: q.model_dump(mode="json") for k, q in self.quotes.items()},
            "expired_quote_ids": sorted(self.expired_quote_ids),
            "events": self.events,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "World":
        clock = FixtureClock(datetime.fromisoformat(data["clock_start"]), datetime.fromisoformat(data["clock_now"]))
        world = cls(data["inventory"], clock)
        world.revisions = dict(data["revisions"])
        world.quotes = {k: Quote.model_validate(v) for k, v in data["quotes"].items()}
        world.expired_quote_ids = set(data["expired_quote_ids"])
        world.events = list(data["events"])
        return world

    # -- inventory --------------------------------------------------------------------

    @property
    def fixture_revision(self) -> str:
        return self.inventory["fixture_revision"]

    def flight(self, flight_id: str) -> dict[str, Any] | None:
        return next((f for f in self.inventory["flights"] if f["flight_id"] == flight_id), None)

    def hotel(self, hotel_id: str) -> dict[str, Any] | None:
        return next((h for h in self.inventory["hotels"] if h["hotel_id"] == hotel_id), None)

    def item(self, item_id: str) -> tuple[str, dict[str, Any]] | None:
        if (f := self.flight(item_id)) is not None:
            return "flight", f
        if (h := self.hotel(item_id)) is not None:
            return "hotel", h
        return None

    def price(self, kind: str, item: dict[str, Any], party: Party) -> tuple[int, int]:
        """Return (unit_amount_cents, total_cents) for the party at the item's current revision."""
        item_id = item["flight_id"] if kind == "flight" else item["hotel_id"]
        rule = item["pricing"]["revisions"][str(self.revisions[item_id])]
        if kind == "flight":
            return rule["amount_cents"], rule["amount_cents"] * party.seats
        extra = max(0, party.occupancy - rule["base_occupancy"]) * rule["extra_guest_cents"]
        total = rule["base_cents"] + extra
        return total, total

    def quote_expired(self, quote: Quote) -> bool:
        return (
            quote.quote_id in self.expired_quote_ids
            or self.clock.now >= quote.expires_at
            or quote.inventory_revision != self.revisions.get(quote.item_id)
        )

    def issue_quote(self, kind: str, item: dict[str, Any], party: Party, depart: date, ret: date, tool_call_id: str) -> Quote:
        item_id = item["flight_id"] if kind == "flight" else item["hotel_id"]
        revision = self.revisions[item_id]
        base_id = f"quote-{item_id}-r{revision}-{party.code()}"
        quote_id, n = base_id, 1
        while quote_id in self.quotes:
            existing = self.quotes[quote_id]
            if not self.quote_expired(existing):
                return existing  # same item, revision, and scope: the same quote
            n += 1
            quote_id = f"{base_id}-{n}"
        unit, total = self.price(kind, item, party)
        ttl = timedelta(minutes=self.inventory["quote_ttl_minutes"])
        quote = Quote(
            quote_id=quote_id,
            item_id=item_id,
            item_kind=kind,  # type: ignore[arg-type]
            label=item["label"],
            inventory_revision=revision,
            scope=QuoteScope(adults=party.adults, children=party.children, depart_date=depart, return_date=ret),
            pricing_basis=item["pricing"]["basis"],
            unit_amount_cents=unit,
            total_cents=total,
            currency=self.inventory["currency"],
            observed_at=self.clock.now,
            expires_at=self.clock.now + ttl,
            tool_call_id=tool_call_id,
            arrive_local=item.get("outbound_arrive_local"),
            stops=item.get("stops"),
            connection=item.get("connection"),
            seats_available=item.get("seats_available"),
            capacity=item.get("capacity"),
            walk_minutes=item.get("walk_minutes_to_venue"),
        )
        self.quotes[quote_id] = quote
        return quote

    # -- world events -----------------------------------------------------------------

    def apply_event(self, event: dict[str, Any]) -> list[str]:
        """Apply a scenario world event. Returns the IDs of quotes it expired."""
        self.events.append({**event, "at": self.clock.now.isoformat()})
        if event["type"] != "reprice":
            raise ValueError(f"Unknown world event {event['type']!r}")
        item_id = event["item_id"]
        expired = [q.quote_id for q in self.quotes.values() if q.item_id == item_id and q.quote_id not in self.expired_quote_ids]
        self.expired_quote_ids.update(expired)
        self.revisions[item_id] = int(event["new_revision"])
        return expired

    def side_effect_fingerprint(self) -> str:
        """Everything a booking or payment would have to change. Tools must leave it alone."""
        return json.dumps(
            {"inventory": self.inventory, "revisions": self.revisions, "events": self.events}, sort_keys=True
        )


# --------------------------------------------------------------------------------------
# Tool definitions shared by every implementation
# --------------------------------------------------------------------------------------

_DATE = {"type": "string", "description": "ISO date, e.g. 2027-04-12"}
_PARTY_PROPS = {
    "adults": {"type": "integer", "minimum": 1},
    "children": {"type": "integer", "minimum": 0},
}

TOOL_SPECS: dict[str, ToolSpec] = {
    "search_flights": ToolSpec(
        name="search_flights",
        description=(
            "Search round-trip flights and return a scoped quote per eligible option. Filters by dates and "
            "seats for the whole party. Optional arrive_before (HH:MM, America/Chicago) keeps only outbound "
            "arrivals strictly earlier than that time. Prices are per traveler; quotes report the party total."
        ),
        parameters={
            "type": "object",
            "properties": {
                "origin": {"type": "string"},
                "destination": {"type": "string", "description": "CHI matches ORD and MDW"},
                "depart_date": _DATE,
                "return_date": _DATE,
                **_PARTY_PROPS,
                "arrive_before": {"type": ["string", "null"], "description": "HH:MM local, or null"},
            },
            "required": ["origin", "destination", "depart_date", "return_date", "adults", "children"],
            "additionalProperties": False,
        },
    ),
    "search_hotels": ToolSpec(
        name="search_hotels",
        description=(
            "Search hotel rooms for the full stay. Returns a whole-stay quote for each available room whose "
            "capacity fits the party, plus ineligible rooms with the reason. Includes walking minutes to the venue."
        ),
        parameters={
            "type": "object",
            "properties": {"check_in": _DATE, "check_out": _DATE, **_PARTY_PROPS},
            "required": ["check_in", "check_out", "adults", "children"],
            "additionalProperties": False,
        },
    ),
    "refresh_quote": ToolSpec(
        name="refresh_quote",
        description=(
            "Request a current quote for one flight or hotel for the given party and dates. Returns a new scoped "
            "quote, or an unavailable result with the reason."
        ),
        parameters={
            "type": "object",
            "properties": {"item_id": {"type": "string"}, "depart_date": _DATE, "return_date": _DATE, **_PARTY_PROPS},
            "required": ["item_id", "depart_date", "return_date", "adults", "children"],
            "additionalProperties": False,
        },
    ),
    "validate_plan": ToolSpec(
        name="validate_plan",
        description=(
            "Independently compute the total for a flight quote plus a hotel quote and check it against the "
            "current requirements: budget, seats, capacity, dates, quote scope and freshness, preferences, and "
            "whether an accepted choice changed materially. Returns reason codes. It never modifies anything."
        ),
        parameters={
            "type": "object",
            "properties": {
                "flight_quote_id": {"type": "string"},
                "hotel_quote_id": {"type": "string"},
                "claimed_total_cents": {"type": ["integer", "null"]},
            },
            "required": ["flight_quote_id", "hotel_quote_id"],
            "additionalProperties": False,
        },
    ),
    "read_history": ToolSpec(
        name="read_history",
        description=(
            "Read retained conversation entries. Filters are exact: turn number, role "
            "(user, assistant, tool, notification, any), and a case-insensitive substring. No semantic search."
        ),
        parameters={
            "type": "object",
            "properties": {
                "turn": {"type": ["integer", "null"]},
                "role": {"type": "string", "enum": ["user", "assistant", "tool", "notification", "any"]},
                "contains": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["role"],
            "additionalProperties": False,
        },
    ),
    "present_plan": ToolSpec(
        name="present_plan",
        description=(
            "Present one plan to the user: a flight quote ID and a hotel quote ID from tool results, your claimed "
            "total in cents, and an explanation that discloses tradeoffs (connections, price changes, unmet "
            "preferences, anything awaiting confirmation). The application validates it independently before the "
            "user sees it; a failed validation comes back to you with reason codes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "flight_quote_id": {"type": "string"},
                "hotel_quote_id": {"type": "string"},
                "claimed_total_cents": {"type": ["integer", "null"]},
                "explanation": {"type": "string"},
            },
            "required": ["flight_quote_id", "hotel_quote_id", "explanation"],
            "additionalProperties": False,
        },
    ),
    "ask_user": ToolSpec(
        name="ask_user",
        description=(
            "Ask the user one targeted question when required information is missing or no plan satisfies the "
            "hard constraints. Name the constraint that is unresolved."
        ),
        parameters={
            "type": "object",
            "properties": {"question": {"type": "string"}, "unresolved_constraint": {"type": "string"}},
            "required": ["question", "unresolved_constraint"],
            "additionalProperties": False,
        },
    ),
}

READ_ONLY_TOOLS = ["search_flights", "search_hotels", "refresh_quote", "validate_plan", "read_history"]
TERMINAL_ACTIONS = ["present_plan", "ask_user"]
AGENT_TOOLS = READ_ONLY_TOOLS + TERMINAL_ACTIONS
ONE_SHOT_TOOLS = TERMINAL_ACTIONS


class ToolError(Exception):
    pass


def _party(args: dict[str, Any]) -> Party:
    return Party(adults=int(args["adults"]), children=int(args.get("children", 0)))


def _date(value: Any, name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ToolError(f"{name} must be an ISO date, got {value!r}") from exc


def _destination_matches(requested: str, airport: str) -> bool:
    return requested.upper() == airport.upper() or (requested.upper() == "CHI" and airport.upper() in {"ORD", "MDW"})


class ToolBox:
    """Executes read-only tools against one run's World.

    ``validate`` and ``read_history`` are injected so this module does not depend on the
    validator or the store.
    """

    def __init__(
        self,
        world: World,
        validate: Callable[[str, str, int | None], dict[str, Any]],
        read_history: Callable[[dict[str, Any]], list[dict[str, Any]]],
    ):
        self.world = world
        self._validate = validate
        self._read_history = read_history

    def execute(self, name: str, arguments: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        handler = {
            "search_flights": self.search_flights,
            "search_hotels": self.search_hotels,
            "refresh_quote": self.refresh_quote,
            "validate_plan": self.validate_plan,
            "read_history": self.read_history,
        }.get(name)
        if handler is None:
            return {"ok": False, "tool": name, "error": f"Unknown or non-executable tool {name!r}"}
        try:
            return {"ok": True, "tool": name, **handler(arguments, tool_call_id)}
        except (ToolError, KeyError, ValueError, TypeError, ValidationError) as exc:
            return {"ok": False, "tool": name, "error": f"{type(exc).__name__}: {exc}"}

    def search_flights(self, args: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        party = _party(args)
        depart, ret = _date(args["depart_date"], "depart_date"), _date(args["return_date"], "return_date")
        arrive_before = args.get("arrive_before") or None
        quotes, excluded = [], []
        for f in self.world.inventory["flights"]:
            if f["origin"].upper() != str(args["origin"]).upper() or not _destination_matches(str(args["destination"]), f["destination"]):
                continue
            if f["depart_date"] != depart.isoformat() or f["return_date"] != ret.isoformat():
                excluded.append({"item_id": f["flight_id"], "reason": f"dates {f['depart_date']}..{f['return_date']} do not match"})
                continue
            if f["seats_available"] < party.seats:
                excluded.append({"item_id": f["flight_id"], "reason": f"{f['seats_available']} seats available < {party.seats} needed"})
                continue
            if arrive_before and not f["outbound_arrive_local"] < arrive_before:
                excluded.append({"item_id": f["flight_id"], "reason": f"arrives {f['outbound_arrive_local']}, not before {arrive_before}"})
                continue
            quotes.append(self.world.issue_quote("flight", f, party, depart, ret, tool_call_id))
        return {
            "observed_at": self.world.clock.now.isoformat(),
            "scope": {"adults": party.adults, "children": party.children, "depart_date": depart.isoformat(), "return_date": ret.isoformat()},
            "arrive_before": arrive_before,
            "quotes": [q.model_dump(mode="json") for q in quotes],
            "excluded": excluded,
        }

    def search_hotels(self, args: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        party = _party(args)
        check_in, check_out = _date(args["check_in"], "check_in"), _date(args["check_out"], "check_out")
        quotes, ineligible = [], []
        for h in self.world.inventory["hotels"]:
            if h["check_in"] != check_in.isoformat() or h["check_out"] != check_out.isoformat():
                ineligible.append({"item_id": h["hotel_id"], "reason": "dates do not match"})
                continue
            if not h["available"]:
                ineligible.append({"item_id": h["hotel_id"], "reason": "sold out for these dates"})
                continue
            if h["capacity"] < party.occupancy:
                ineligible.append(
                    {"item_id": h["hotel_id"], "reason": f"capacity {h['capacity']} < occupancy {party.occupancy}", "walk_minutes": h["walk_minutes_to_venue"]}
                )
                continue
            quotes.append(self.world.issue_quote("hotel", h, party, check_in, check_out, tool_call_id))
        return {
            "observed_at": self.world.clock.now.isoformat(),
            "scope": {"adults": party.adults, "children": party.children, "depart_date": check_in.isoformat(), "return_date": check_out.isoformat()},
            "quotes": [q.model_dump(mode="json") for q in quotes],
            "ineligible": ineligible,
        }

    def refresh_quote(self, args: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        party = _party(args)
        depart, ret = _date(args["depart_date"], "depart_date"), _date(args["return_date"], "return_date")
        found = self.world.item(str(args["item_id"]))
        if found is None:
            return {"available": False, "item_id": args["item_id"], "reason": "unknown item", "quotes": []}
        kind, item = found
        if kind == "flight":
            if item["depart_date"] != depart.isoformat() or item["return_date"] != ret.isoformat():
                return {"available": False, "item_id": item["flight_id"], "reason": "dates do not match", "quotes": []}
            if item["seats_available"] < party.seats:
                return {"available": False, "item_id": item["flight_id"], "reason": "insufficient seats", "quotes": []}
        else:
            if not item["available"]:
                return {"available": False, "item_id": item["hotel_id"], "reason": "sold out", "quotes": []}
            if item["capacity"] < party.occupancy:
                return {"available": False, "item_id": item["hotel_id"], "reason": f"capacity {item['capacity']} < occupancy {party.occupancy}", "quotes": []}
        quote = self.world.issue_quote(kind, item, party, depart, ret, tool_call_id)
        return {"available": True, "item_id": quote.item_id, "quotes": [quote.model_dump(mode="json")]}

    def validate_plan(self, args: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        claimed = args.get("claimed_total_cents")
        return {"validation": self._validate(str(args["flight_quote_id"]), str(args["hotel_quote_id"]), claimed)}

    def read_history(self, args: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        return {"entries": self._read_history(args)}

    def snapshot(self, origin: str, destination: str, party: Party, depart: date, ret: date, arrive_before: str | None,
                 tool_call_id: str) -> dict[str, Any]:
        """Inventory snapshot prepared by the application for the one-shot planner."""
        flights = self.search_flights(
            {"origin": origin, "destination": destination, "depart_date": depart.isoformat(), "return_date": ret.isoformat(),
             "adults": party.adults, "children": party.children, "arrive_before": arrive_before},
            tool_call_id,
        )
        hotels = self.search_hotels(
            {"check_in": depart.isoformat(), "check_out": ret.isoformat(), "adults": party.adults, "children": party.children},
            tool_call_id,
        )
        return {"flights": flights, "hotels": hotels}
