"""Planners: a deterministic simulator and live adapters for Anthropic and OpenAI.

Every planner implements ``next_action(model_input, call_id) -> PlannerResponse`` and sees
exactly the ``ModelInput`` the runner built. The simulator reads that input the way a model
would: from the rendered context sections (state agent) or by scanning the conversation
(transcript and one-shot). It never reads scenario names, expected answers, or validator
internals, and it trusts whatever context it is given. Its results demonstrate workflow
and data behavior; they do not measure LLM capability.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from .models import (
    AskUser,
    CallTool,
    ModelInput,
    PlanDraft,
    PlannerAction,
    PlannerResponse,
    PresentPlan,
    ToolCallRecord,
    TranscriptEntry,
    Usage,
    dollars,
)
from .tools import TOOL_SPECS


class Planner(Protocol):
    mode: str

    def settings(self) -> dict[str, Any]: ...

    def next_action(self, model_input: ModelInput, call_id: str) -> PlannerResponse: ...


# ======================================================================================
# Simulator
# ======================================================================================


@dataclass
class Beliefs:
    """What the simulated planner believes after reading its input."""

    origin: str | None = None
    destination: str | None = None
    depart: str | None = None
    ret: str | None = None
    adults: int | None = None
    children: int | None = None
    budget_cents: int | None = None
    walk_max: int | None = None
    earlier_before: str | None = None
    earlier_ref_proposal: str | None = None
    unknowns: list[str] = field(default_factory=list)
    accepted: dict[str, Any] | None = None
    quotes: list[dict[str, Any]] = field(default_factory=list)
    unsuitable: set[str] = field(default_factory=set)
    proposal: dict[str, Any] | None = None
    need_hotel_search: bool = False
    need_flight_search: bool = False
    refresh_items: list[str] = field(default_factory=list)
    need_revalidate: bool = False
    rejected: set[tuple[str, str]] = field(default_factory=set)
    validated_ok: set[tuple[str, str]] = field(default_factory=set)
    validated_any: set[tuple[str, str]] = field(default_factory=set)
    tools: set[str] = field(default_factory=set)

    @property
    def seats(self) -> int:
        return (self.adults or 0) + (self.children or 0)


def _flatten_quote(q: dict[str, Any]) -> dict[str, Any]:
    """Accept either a raw Quote payload or a context quote_view."""
    scope = q.get("scope") or {}
    return {
        "quote_id": q["quote_id"],
        "item_id": q["item_id"],
        "kind": q.get("kind") or q.get("item_kind"),
        "label": q.get("label", q["item_id"]),
        "total_cents": q["total_cents"],
        "unit_amount_cents": q.get("unit_amount_cents"),
        "adults": q.get("adults", scope.get("adults")),
        "children": q.get("children", scope.get("children")),
        "depart": scope.get("depart_date") or (q.get("dates", "..").split("..")[0] or None),
        "ret": scope.get("return_date") or (q.get("dates", "..").split("..")[-1] or None),
        "revision": q.get("inventory_revision"),
        "arrive_local": q.get("arrive_local"),
        "stops": q.get("stops"),
        "connection": q.get("connection"),
        "seats_available": q.get("seats_available"),
        "capacity": q.get("capacity"),
        "walk_minutes": q.get("walk_minutes"),
    }


def beliefs_from_context(mi: ModelInput) -> Beliefs:
    assert mi.context is not None
    ctx = mi.context
    b = Beliefs(tools={t.name for t in mi.tools})

    def content(name: str) -> Any:
        s = ctx.section(name)
        return s.content if s is not None else None

    req = content("requirements")
    if not req:
        b.unknowns = ["requirements (not supplied in context)"]
        return b
    hard, prefs = req.get("hard_constraints", {}), req.get("preferences", {})
    party = hard.get("party") or {}
    b.origin, b.destination = hard.get("origin"), hard.get("destination")
    b.depart, b.ret = hard.get("depart_date"), hard.get("return_date")
    b.adults, b.children = party.get("adults"), party.get("children")
    b.budget_cents = (hard.get("budget") or {}).get("max_total_cents")
    b.walk_max = (prefs.get("walk") or {}).get("max_minutes")
    earlier = prefs.get("earlier_arrival") or {}
    b.earlier_before, b.earlier_ref_proposal = earlier.get("arrive_before_local"), earlier.get("reference_proposal_id")
    b.unknowns = list(content("unknowns") or [])
    b.accepted = (content("decisions") or {}).get("hotel")
    b.quotes = [_flatten_quote(q) for q in (content("current_quotes") or []) if q.get("status", "current") == "current"]
    b.unsuitable = {n["item_id"] for n in (content("unsuitable_items") or [])}
    b.proposal = content("current_proposal")

    occupancy = b.seats
    has_room = any(q["kind"] == "hotel" and (q["capacity"] or 0) >= occupancy for q in b.quotes)
    for task in content("pending_work") or []:
        kind, target = task.get("kind"), task.get("target")
        if kind in ("refresh_hotel_quote", "refresh_flight_quote") and target:
            b.refresh_items.append(target)
        elif kind == "find_hotel_capacity" and not has_room:
            b.need_hotel_search = True
        elif kind == "search_earlier_flight":
            b.need_flight_search = True
        elif kind == "revalidate_plan":
            b.need_revalidate = True
    for v in content("validation_feedback") or []:
        key = (v["flight_quote_id"], v["hotel_quote_id"])
        b.validated_any.add(key)
        (b.validated_ok if v["compliant"] else b.rejected).add(key)
    return b


def beliefs_from_transcript(mi: ModelInput) -> Beliefs:
    """Reconstruct current facts by reading the whole conversation, oldest first."""
    b = Beliefs(tools={t.name for t in mi.tools})
    seen: dict[str, tuple[int, dict[str, Any]]] = {}
    expired: set[str] = set()
    party_idx = earlier_idx = last_input_idx = -1
    last_hotel_search = last_flight_search = last_earlier_search = -1
    ineligible_by_idx: list[tuple[int, str]] = []
    have_requirements = False
    validations: list[tuple[int, dict[str, Any]]] = []
    accepted_total: int | None = None

    for i, e in enumerate(mi.messages):
        n = e.normalized or {}
        if e.role in ("user", "notification") and n:
            last_input_idx = i
            req = n.get("requirements")
            if req:
                have_requirements = True
                party = req.get("party") or {}
                b.origin, b.destination = req.get("origin"), req.get("destination")
                b.depart, b.ret = req.get("depart_date"), req.get("return_date")
                b.adults, b.children = party.get("adults"), party.get("children")
                b.budget_cents = (req.get("budget") or {}).get("max_total_cents")
                b.walk_max = (req.get("walk_preference") or {}).get("max_minutes")
                party_idx = i
                if req.get("earlier_arrival"):  # restated brief for the one-shot caller
                    b.earlier_before = req["earlier_arrival"]["arrive_before_local"]
                    b.earlier_ref_proposal = req["earlier_arrival"].get("reference_proposal_id")
                    earlier_idx = i
                if req.get("accepted_hotel"):
                    b.accepted = dict(req["accepted_hotel"])
                    accepted_total = b.accepted.get("accepted_total_cents")
            for q in (n.get("inventory_snapshot") or {}).get("quotes", []):
                seen[q["quote_id"]] = (i, _flatten_quote(q))
            for ch in n.get("changes", []):
                t = ch.get("type")
                if t == "PartySizeChanged":
                    b.adults, b.children, party_idx = ch["adults"], ch["children"], i
                elif t == "HotelAccepted":
                    accepted_total = seen.get(ch["quote_id"], (0, {}))[1].get("total_cents")
                    b.accepted = {"hotel_id": ch["hotel_id"], "accepted_quote_id": ch["quote_id"], "status": "accepted",
                                  "accepted_total_cents": accepted_total}
                elif t == "EarlierFlightRequested":
                    b.earlier_before, b.earlier_ref_proposal, earlier_idx = ch["reference_arrival_local"], ch["reference_proposal_id"], i
                elif t == "QuoteExpired":
                    expired.add(ch["quote_id"])
        elif e.role == "assistant" and e.tool_call is not None:
            name, args = e.tool_call.name, e.tool_call.arguments
            if name == "search_hotels":
                last_hotel_search = i
            elif name == "search_flights":
                last_flight_search = i
                if args.get("arrive_before") and args.get("arrive_before") == b.earlier_before:
                    last_earlier_search = i
        elif e.role == "tool" and n:
            for q in n.get("quotes", []):
                seen[q["quote_id"]] = (i, _flatten_quote(q))
            for inel in n.get("ineligible", []):
                if "capacity" in inel.get("reason", ""):
                    ineligible_by_idx.append((i, inel["item_id"]))
            if n.get("validation"):
                validations.append((i, n["validation"]))
            if n.get("displayed_proposal"):
                b.proposal = n["displayed_proposal"]

    if not have_requirements:
        b.unknowns = ["trip requirements (origin, destination, dates, party, budget)"]
        return b
    known = {"origin": b.origin, "destination": b.destination, "depart_date": b.depart, "return_date": b.ret,
             "party": b.adults, "budget": b.budget_cents}
    b.unknowns = [name for name, value in known.items() if value is None]
    if b.unknowns:
        return b

    # Keep quotes that price the current party and dates, are not expired, and are the newest per item+scope.
    newest: dict[tuple[str, int, int], tuple[int, dict[str, Any]]] = {}
    for qid, (idx, q) in seen.items():
        if qid in expired or q["adults"] != b.adults or q["children"] != b.children:
            continue
        if q["depart"] != b.depart or q["ret"] != b.ret:
            continue
        key = (q["item_id"], q["adults"], q["children"])
        if key not in newest or (q["revision"] or 0) >= (newest[key][1]["revision"] or 0):
            newest[key] = (idx, q)
    b.quotes = [q for _, q in newest.values()]
    b.unsuitable = {item for idx, item in ineligible_by_idx if idx > party_idx}

    if b.accepted is not None:
        current = next((q for q in b.quotes if q["item_id"] == b.accepted["hotel_id"]), None)
        if current is not None and accepted_total is not None and current["total_cents"] != accepted_total:
            b.accepted["status"] = "needs_reconfirmation"
        if b.accepted["hotel_id"] in b.unsuitable:
            b.accepted["status"] = "no_longer_suitable"
        elif current is None:
            b.refresh_items.append(b.accepted["hotel_id"])

    b.need_hotel_search = last_hotel_search < party_idx
    b.need_flight_search = last_flight_search < party_idx or (earlier_idx >= 0 and last_earlier_search < earlier_idx)
    if b.proposal is not None:
        current_ids = {q["quote_id"] for q in b.quotes}
        if not {b.proposal["flight_quote_id"], b.proposal["hotel_quote_id"]} <= current_ids:
            b.need_revalidate = True
    for idx, v in validations:
        if idx > last_input_idx:
            key = (v["flight_quote_id"], v["hotel_quote_id"])
            b.validated_any.add(key)
            compliant = v["hard_constraints_ok"] and v["quotes_fresh"]
            (b.validated_ok if compliant else b.rejected).add(key)
    return b


def _quote_for(b: Beliefs, item_id: str | None) -> dict[str, Any] | None:
    return next((q for q in b.quotes if q["item_id"] == item_id), None)


def _explain(b: Beliefs, f: dict[str, Any], h: dict[str, Any]) -> str:
    total = f["total_cents"] + h["total_cents"]
    stops = "nonstop" if not f["stops"] else f"{f['stops']} stop ({f['connection']})"
    lines = [
        f"Flight {f['item_id']}, {f['label']}: outbound arrives {f['arrive_local']} Chicago time, {stops}, "
        f"{dollars(f['unit_amount_cents'])} per traveler x {b.seats} = {dollars(f['total_cents'])}.",
        f"Hotel {h['item_id']}, {h['label']}: {h['walk_minutes']}-minute walk to the venue, sleeps {h['capacity']}, "
        f"{dollars(h['total_cents'])} for the full stay.",
        f"Total {dollars(total)} against the {dollars(b.budget_cents)} budget.",
    ]
    if b.earlier_before:
        if f["arrive_local"] < b.earlier_before:
            lines.append(f"Arrives earlier than the {b.earlier_before} arrival you were looking at in {b.earlier_ref_proposal}.")
        else:
            lines.append(f"Tradeoff: does not arrive earlier than {b.earlier_before}.")
    if f["stops"]:
        lines.append(f"Tradeoff: this flight connects ({f['connection']}).")
    if b.walk_max is not None and (h["walk_minutes"] or 0) > b.walk_max:
        lines.append(f"Tradeoff: the hotel is farther than your {b.walk_max}-minute walking preference.")
    acc = b.accepted
    if acc and acc["hotel_id"] == h["item_id"] and acc.get("accepted_total_cents") not in (None, h["total_cents"]):
        lines.append(
            f"The hotel you accepted now costs {dollars(h['total_cents'])} instead of {dollars(acc['accepted_total_cents'])}. "
            "Please reconfirm before relying on this plan."
        )
    elif acc and acc["hotel_id"] != h["item_id"]:
        lines.append(f"This replaces the hotel you accepted ({acc['hotel_id']}); please confirm.")
    lines.append("This is a proposal only; nothing has been booked.")
    return " ".join(lines)


def decide(b: Beliefs) -> tuple[PlannerAction, str]:
    """Pick one action from beliefs. Returns the action and a one-line rationale."""
    if b.unknowns:
        return AskUser(question=f"Before I can plan, I need: {', '.join(b.unknowns)}.", unresolved_constraint=b.unknowns[0]), "required inputs unknown"

    trip = {"depart_date": b.depart, "return_date": b.ret, "adults": b.adults, "children": b.children}
    for item in b.refresh_items:
        if "refresh_quote" in b.tools:
            return CallTool(name="refresh_quote", arguments={"item_id": item, **trip}), f"no current quote for {item}"

    hotels = [q for q in b.quotes if q["kind"] == "hotel" and (q["capacity"] or 0) >= b.seats and q["item_id"] not in b.unsuitable]
    acc = b.accepted
    accepted_usable = acc is not None and acc.get("status") != "no_longer_suitable" and acc["hotel_id"] not in b.unsuitable
    pool = [q for q in hotels if q["item_id"] == acc["hotel_id"]] if accepted_usable else hotels
    if ((b.need_hotel_search and not accepted_usable) or not pool) and "search_hotels" in b.tools:
        return CallTool(name="search_hotels", arguments={"check_in": b.depart, "check_out": b.ret, "adults": b.adults, "children": b.children}), "need hotel options for this party"

    flights = [q for q in b.quotes if q["kind"] == "flight" and (q["seats_available"] or 0) >= b.seats]
    if (b.need_flight_search or not flights) and "search_flights" in b.tools:
        return (
            CallTool(name="search_flights", arguments={"origin": b.origin, "destination": b.destination, **trip, "arrive_before": b.earlier_before}),
            "need flight options for this party" + (f" arriving before {b.earlier_before}" if b.earlier_before else ""),
        )
    if not pool or not flights:
        return AskUser(question="I could not find a flight and a room that fit your party and dates. Can any dates or party details change?",
                       unresolved_constraint="availability"), "no eligible inventory"

    if b.need_revalidate and b.proposal and "validate_plan" in b.tools:
        fq, hq = _quote_for(b, b.proposal["flight_id"]), _quote_for(b, b.proposal["hotel_id"])
        if fq and hq:
            key = (fq["quote_id"], hq["quote_id"])
            if key != (b.proposal["flight_quote_id"], b.proposal["hotel_quote_id"]) and key not in b.validated_any:
                return (
                    CallTool(name="validate_plan", arguments={"flight_quote_id": key[0], "hotel_quote_id": key[1], "claimed_total_cents": fq["total_cents"] + hq["total_cents"]}),
                    f"recheck {b.proposal['proposal_id']} with refreshed quotes",
                )

    def rank(pair: tuple[dict[str, Any], dict[str, Any]]) -> tuple:
        f, h = pair
        earlier_unmet = bool(b.earlier_before) and not f["arrive_local"] < b.earlier_before
        walk_unmet = b.walk_max is not None and (h["walk_minutes"] or 0) > b.walk_max
        return (earlier_unmet, walk_unmet, (f["stops"] or 0) > 0, f["total_cents"] + h["total_cents"], f["item_id"], h["item_id"])

    pairs = sorted(((f, h) for f in flights for h in pool), key=rank)
    affordable = [
        (f, h) for f, h in pairs
        if (f["quote_id"], h["quote_id"]) not in b.rejected and (b.budget_cents is None or f["total_cents"] + h["total_cents"] <= b.budget_cents)
    ]
    if not affordable:
        f, h = min(pairs, key=lambda p: p[0]["total_cents"] + p[1]["total_cents"])
        lowest = f["total_cents"] + h["total_cents"]
        hotel_note = (
            f"{h['label']} ({h['item_id']}), which you accepted, now costs {dollars(h['total_cents'])}"
            + (f" (it was {dollars(acc['accepted_total_cents'])})" if accepted_usable and acc.get("accepted_total_cents") not in (None, h["total_cents"]) else "")
            if accepted_usable else f"The lowest-priced suitable room is {h['label']} at {dollars(h['total_cents'])}"
        )
        question = (
            f"No combination fits the {dollars(b.budget_cents)} budget. {hotel_note}. The lowest total with it is "
            f"{dollars(lowest)} using flight {f['item_id']}. Would you rather raise the budget to at least {dollars(lowest)}, "
            "or have me look for a different hotel?"
        )
        return AskUser(question=question, unresolved_constraint="budget"), "no affordable combination"

    f, h = affordable[0]
    key = (f["quote_id"], h["quote_id"])
    total = f["total_cents"] + h["total_cents"]
    if "validate_plan" in b.tools and key not in b.validated_ok:
        return CallTool(name="validate_plan", arguments={"flight_quote_id": key[0], "hotel_quote_id": key[1], "claimed_total_cents": total}), "check the best candidate before presenting"
    return PresentPlan(plan=PlanDraft(flight_quote_id=key[0], hotel_quote_id=key[1], claimed_total_cents=total), explanation=_explain(b, f, h)), "best validated candidate"


def action_to_tool_call(action: PlannerAction, call_id: str) -> ToolCallRecord:
    if isinstance(action, CallTool):
        return ToolCallRecord(id=call_id, name=action.name, arguments=action.arguments)
    if isinstance(action, PresentPlan):
        return ToolCallRecord(id=call_id, name="present_plan", arguments={**action.plan.model_dump(), "explanation": action.explanation})
    return ToolCallRecord(id=call_id, name="ask_user", arguments={"question": action.question, "unresolved_constraint": action.unresolved_constraint})


class SimulatedPlanner:
    mode = "simulated"

    def settings(self) -> dict[str, Any]:
        return {"planner_mode": "simulated", "model": None, "note": "Deterministic rule-based planner. Results do not measure LLM capability."}

    def next_action(self, model_input: ModelInput, call_id: str) -> PlannerResponse:
        beliefs = beliefs_from_context(model_input) if model_input.context is not None else beliefs_from_transcript(model_input)
        action, rationale = decide(beliefs)
        entry = TranscriptEntry(turn=-1, role="assistant", content=f"[simulated] {rationale}", tool_call=action_to_tool_call(action, call_id))
        return PlannerResponse(action=action, action_call_id=call_id, usage=None, latency_ms=None, transcript_additions=[entry])


# ======================================================================================
# Live adapters
# ======================================================================================


def parse_tool_call(name: str, arguments: dict[str, Any]) -> PlannerAction:
    """Validate a model's tool call and turn it into a typed action. Raises ValueError."""
    spec = TOOL_SPECS.get(name)
    if spec is None:
        raise ValueError(f"Unknown tool {name!r}. Available: {', '.join(TOOL_SPECS)}")
    missing = [k for k in spec.parameters.get("required", []) if k not in arguments]
    if missing:
        raise ValueError(f"{name} is missing required arguments: {', '.join(missing)}")
    try:
        if name == "present_plan":
            return PresentPlan(
                plan=PlanDraft(
                    flight_quote_id=arguments["flight_quote_id"],
                    hotel_quote_id=arguments["hotel_quote_id"],
                    claimed_total_cents=arguments.get("claimed_total_cents"),
                ),
                explanation=str(arguments["explanation"]),
            )
        if name == "ask_user":
            return AskUser(question=str(arguments["question"]), unresolved_constraint=str(arguments["unresolved_constraint"]))
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    return CallTool(name=name, arguments=arguments)


@dataclass
class ProviderTurn:
    """One provider response, normalized."""

    entry: TranscriptEntry
    tool_name: str | None
    tool_args: dict[str, Any] | None
    tool_call_id: str | None
    usage: Usage
    error: str | None = None
    request: Any = None


class LivePlanner:
    mode = "live"
    provider = "base"

    def __init__(self, model: str, max_tokens: int = 16000, max_retries: int = 2):
        self.model = model
        self.max_tokens = max_tokens
        self.max_retries = max_retries

    def settings(self) -> dict[str, Any]:
        return {"planner_mode": "live", "provider": self.provider, "model": self.model, "max_tokens": self.max_tokens,
                "max_malformed_retries": self.max_retries}

    def _call(self, system: str, entries: list[TranscriptEntry], tools: list[Any]) -> ProviderTurn:
        raise NotImplementedError

    def next_action(self, model_input: ModelInput, call_id: str) -> PlannerResponse:
        entries = list(model_input.messages)
        additions: list[TranscriptEntry] = []
        errors: list[str] = []
        usage: Usage | None = None  # stays None unless the provider actually reports counts
        latency = 0.0
        requests: list[Any] = []
        for attempt in range(1, self.max_retries + 2):
            started = time.perf_counter()
            try:
                turn = self._call(model_input.system, entries, model_input.tools)
            except Exception as exc:  # network, auth, rate limit: record honestly and stop
                latency += (time.perf_counter() - started) * 1000
                errors.append(f"provider error: {type(exc).__name__}: {exc}")
                return PlannerResponse(action=None, action_call_id=call_id, usage=usage, latency_ms=latency, attempts=attempt,
                                       errors=errors, transcript_additions=additions, provider_request=requests)
            latency += (time.perf_counter() - started) * 1000
            requests.append(turn.request)
            usage = _add_usage(usage, turn.usage)
            entries.append(turn.entry)
            additions.append(turn.entry)
            error = turn.error
            action = None
            if error is None and turn.tool_name is not None:
                try:
                    action = parse_tool_call(turn.tool_name, turn.tool_args or {})
                except ValueError as exc:
                    error = f"invalid tool call: {exc}"
            if action is not None:
                return PlannerResponse(action=action, action_call_id=turn.tool_call_id or call_id, usage=usage, latency_ms=latency,
                                       attempts=attempt, errors=errors, transcript_additions=additions, provider_request=requests)
            errors.append(error or "no tool call in response")
            # Keep the failed attempt and the correction in the transcript (append-only history).
            if turn.tool_call_id:
                correction = TranscriptEntry(turn=-1, role="tool", tool_call_id=turn.tool_call_id,
                                             content=f"Error: {errors[-1]}. Call exactly one tool with valid arguments.")
            else:
                correction = TranscriptEntry(turn=-1, role="user", content=f"[Application] {errors[-1]}. Reply with exactly one tool call.")
            entries.append(correction)
            additions.append(correction)
        return PlannerResponse(action=None, action_call_id=call_id, usage=usage, latency_ms=latency, attempts=self.max_retries + 1,
                               errors=errors, transcript_additions=additions, provider_request=requests)


def _add_usage(total: Usage | None, new: Usage | None) -> Usage | None:
    """Sum reported token counts. A count nobody reported stays None rather than becoming 0."""
    if new is None or (new.input_tokens is None and new.output_tokens is None):
        return total
    total = total or Usage()

    def add(a: int | None, b: int | None) -> int | None:
        return b if a is None else (a if b is None else a + b)

    return Usage(input_tokens=add(total.input_tokens, new.input_tokens), output_tokens=add(total.output_tokens, new.output_tokens))


def _user_text(e: TranscriptEntry) -> str:
    return f"[Notification] {e.content}" if e.role == "notification" else e.content


class AnthropicPlanner(LivePlanner):
    provider = "anthropic"

    def __init__(self, model: str, max_tokens: int = 16000, max_retries: int = 2, effort: str | None = None, fallbacks: str | None = None):
        super().__init__(model, max_tokens, max_retries)
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.effort = effort or None
        self.fallbacks = fallbacks or None

    def settings(self) -> dict[str, Any]:
        return {**super().settings(), "effort": self.effort, "server_side_fallbacks": self.fallbacks,
                "tool_choice": "auto, one tool per reply (forced tool choice is not supported on current models)"}

    @staticmethod
    def to_messages(entries: list[TranscriptEntry], strip_thinking: bool = False) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []

        def user_block(block: dict[str, Any]) -> None:
            if messages and messages[-1]["role"] == "user":
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})

        for e in entries:
            if e.role == "assistant":
                raw = e.provider_raw or {}
                if raw.get("provider") == "anthropic":
                    content = [dict(b) for b in raw["content"]]
                    if strip_thinking:
                        content = [b for b in content if b.get("type") not in ("thinking", "redacted_thinking")]
                else:
                    content = []
                    if e.content:
                        content.append({"type": "text", "text": e.content})
                    if e.tool_call:
                        content.append({"type": "tool_use", "id": e.tool_call.id, "name": e.tool_call.name, "input": e.tool_call.arguments})
                messages.append({"role": "assistant", "content": content or [{"type": "text", "text": "(no content)"}]})
            elif e.role == "tool":
                user_block({"type": "tool_result", "tool_use_id": e.tool_call_id, "content": e.content})
            else:
                user_block({"type": "text", "text": _user_text(e)})
        return messages

    def _call(self, system: str, entries: list[TranscriptEntry], tools: list[Any]) -> ProviderTurn:
        extra_body: dict[str, Any] = {}
        extra_headers: dict[str, str] = {}
        if self.effort:
            extra_body["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            extra_body["fallbacks"] = self.fallbacks
            extra_headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        request = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": self.to_messages(entries),
            "tools": [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools],
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        }
        try:
            resp = self.client.messages.create(**request, extra_body=extra_body or None, extra_headers=extra_headers or None)
        except self._anthropic.BadRequestError as exc:
            # A replayed thinking block can be rejected if history was edited. Strip once and retry.
            if "thinking" not in str(exc) and "signature" not in str(exc):
                raise
            request["messages"] = self.to_messages(entries, strip_thinking=True)
            resp = self.client.messages.create(**request, extra_body=extra_body or None, extra_headers=extra_headers or None)
        blocks = [b.model_dump(mode="json", exclude_none=True) for b in resp.content]
        text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
        usage = Usage(input_tokens=getattr(resp.usage, "input_tokens", None), output_tokens=getattr(resp.usage, "output_tokens", None))
        error = None
        if resp.stop_reason == "refusal":
            error = f"model refused (stop_details={getattr(resp, 'stop_details', None)})"
        elif resp.stop_reason == "max_tokens":
            error = "response hit max_tokens before finishing"
        tool = tool_uses[0] if tool_uses else None
        entry = TranscriptEntry(
            turn=-1,
            role="assistant",
            content=text,
            tool_call=ToolCallRecord(id=tool["id"], name=tool["name"], arguments=tool.get("input") or {}) if tool else None,
            provider_raw={"provider": "anthropic", "model": resp.model, "stop_reason": resp.stop_reason, "content": blocks},
        )
        return ProviderTurn(entry, tool["name"] if tool else None, tool.get("input") if tool else None, tool["id"] if tool else None,
                            usage, error, request={**request, "extra_body": extra_body, "extra_headers": extra_headers})


class OpenAIPlanner(LivePlanner):
    provider = "openai"

    def __init__(self, model: str, max_tokens: int = 16000, max_retries: int = 2, reasoning_effort: str | None = None, temperature: float | None = None):
        super().__init__(model, max_tokens, max_retries)
        import openai

        self.client = openai.OpenAI()
        self.reasoning_effort = reasoning_effort or None
        self.temperature = temperature

    def settings(self) -> dict[str, Any]:
        return {**super().settings(), "reasoning_effort": self.reasoning_effort, "temperature": self.temperature,
                "tool_choice": "required", "parallel_tool_calls": False}

    @staticmethod
    def to_messages(system: str, entries: list[TranscriptEntry]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for e in entries:
            if e.role == "assistant":
                # A reply with neither text nor a tool call (an empty reply) still needs content to be valid.
                msg: dict[str, Any] = {"role": "assistant", "content": e.content or (None if e.tool_call else "(empty reply)")}
                if e.tool_call:
                    raw_args = (e.provider_raw or {}).get("arguments") if (e.provider_raw or {}).get("provider") == "openai" else None
                    msg["tool_calls"] = [{"id": e.tool_call.id, "type": "function",
                                          "function": {"name": e.tool_call.name, "arguments": raw_args or json.dumps(e.tool_call.arguments)}}]
                messages.append(msg)
            elif e.role == "tool":
                messages.append({"role": "tool", "tool_call_id": e.tool_call_id, "content": e.content})
            else:
                messages.append({"role": "user", "content": _user_text(e)})
        return messages

    def _call(self, system: str, entries: list[TranscriptEntry], tools: list[Any]) -> ProviderTurn:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": self.to_messages(system, entries),
            "tools": [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools],
            "tool_choice": "required",
            "parallel_tool_calls": False,
            "max_completion_tokens": self.max_tokens,
        }
        if self.reasoning_effort:
            request["reasoning_effort"] = self.reasoning_effort
        if self.temperature is not None:
            request["temperature"] = self.temperature
        resp = self.client.chat.completions.create(**request)
        choice = resp.choices[0]
        msg = choice.message
        usage = Usage(input_tokens=getattr(resp.usage, "prompt_tokens", None), output_tokens=getattr(resp.usage, "completion_tokens", None))
        call = (msg.tool_calls or [None])[0]
        args: dict[str, Any] | None = None
        error = None
        if call is not None:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                error = f"tool arguments were not valid JSON: {exc}"
        if getattr(msg, "refusal", None):
            error = f"model refused: {msg.refusal}"
        entry = TranscriptEntry(
            turn=-1,
            role="assistant",
            content=msg.content or getattr(msg, "refusal", None) or "",
            tool_call=ToolCallRecord(id=call.id, name=call.function.name, arguments=args or {}) if call else None,
            provider_raw={"provider": "openai", "model": resp.model, "finish_reason": choice.finish_reason,
                          "arguments": call.function.arguments if call else None},
        )
        return ProviderTurn(entry, call.function.name if call else None, args, call.id if call else None, usage, error, request=request)


# ======================================================================================
# Factory
# ======================================================================================


def load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    except ImportError:
        pass


def live_provider_status() -> dict[str, bool]:
    """Which providers have credentials configured. Never returns the credentials."""
    load_env()
    return {
        "anthropic": bool(
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_PROFILE")
            or _ant_profile_present()
        ),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
    }


def _ant_profile_present() -> bool:
    """True after `ant auth login`; the Anthropic SDK reads that profile without an env var."""
    creds = Path.home() / ".config" / "anthropic" / "credentials"
    return creds.is_dir() and any(creds.iterdir())


def resolve_provider(requested: str | None) -> str:
    load_env()
    provider = (requested or os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if provider:
        if provider not in ("anthropic", "openai"):
            raise ValueError(f"Unknown provider {provider!r}; use 'anthropic' or 'openai'.")
        return provider
    status = live_provider_status()
    if status["anthropic"] and not status["openai"]:
        return "anthropic"
    if status["openai"] and not status["anthropic"]:
        return "openai"
    if status["anthropic"] and status["openai"]:
        return "anthropic"
    raise ValueError("No live provider configured. Copy .env.example to .env and set ANTHROPIC_API_KEY or OPENAI_API_KEY.")


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def make_planner(mode: str, provider: str | None = None) -> Planner:
    if mode == "simulated":
        return SimulatedPlanner()
    if mode != "live":
        raise ValueError(f"Unknown planner mode {mode!r}")
    provider = resolve_provider(provider)
    max_tokens = _int_env("LLM_MAX_TOKENS", 16000)
    retries = _int_env("LLM_MAX_RETRIES", 2)
    try:
        return _construct_live(provider, max_tokens, retries)
    except Exception as exc:  # missing key, bad profile, SDK import problem
        key = "ANTHROPIC_API_KEY (or `ant auth login`)" if provider == "anthropic" else "OPENAI_API_KEY"
        raise ValueError(f"Could not set up the {provider} client: {exc}. Check {key} in .env.") from exc


def _construct_live(provider: str, max_tokens: int, retries: int) -> Planner:
    if provider == "anthropic":
        return AnthropicPlanner(
            model=os.environ.get("ANTHROPIC_MODEL", "").strip() or "claude-opus-5-5",
            max_tokens=max_tokens,
            max_retries=retries,
            effort=os.environ.get("ANTHROPIC_EFFORT", "").strip() or None,
            fallbacks=os.environ.get("ANTHROPIC_SERVER_FALLBACKS", "").strip() or None,
        )
    temperature = os.environ.get("OPENAI_TEMPERATURE", "").strip()
    return OpenAIPlanner(
        model=os.environ.get("OPENAI_MODEL", "").strip() or "gpt-5.5",
        max_tokens=max_tokens,
        max_retries=retries,
        reasoning_effort=os.environ.get("OPENAI_REASONING_EFFORT", "").strip() or None,
        temperature=float(temperature) if temperature else None,
    )
