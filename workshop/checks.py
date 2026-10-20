"""Grading checks.

Two families, both independent of the code under test:

* ``exercise checks`` drive the student's two functions through a scripted version of the
  canonical scenario using real tools and the independent validator, then assert on the
  resulting state and context.
* ``scenario checks`` read a recorded session (any implementation) and check outcomes
  against the world fixture and the change log.

Every failure is classified so a student can tell their bug from broken infrastructure:
``fail`` (an assertion about their result), ``todo`` (NotImplementedError), ``student_error``
(an exception raised inside the exercise file), or ``infrastructure_error`` (anything else).
"""

from __future__ import annotations

import json
import tempfile
import traceback
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .models import (
    Change,
    DecisionStatus,
    PlanningStep,
    Proposal,
    QuoteStatus,
    TaskKind,
    TaskState,
    ToolObservationReceived,
)
from .runner import Strategy, load_scenario, load_strategy
from .state import apply_logged, initial_state, open_tasks
from .store import RunStore
from .tools import ToolBox, World
from .validation import Authority, validate_plan

SESSION_DB = "session.sqlite"


# ======================================================================================
# Scenario checks (recorded sessions)
# ======================================================================================


def _check(cid: str, description: str, passed: bool | None, detail: str = "", category: str = "outcome") -> dict[str, Any]:
    return {"id": cid, "description": description, "passed": passed, "detail": detail, "category": category}


def scenario_checks(root: Path) -> list[dict[str, Any]]:
    store = RunStore(root / SESSION_DB)
    try:
        store.read().__enter__()  # one consistent snapshot; closing the connection ends it
        config = store.get("config")
        world = World.from_json(store.get("world"))
        changes = store.changes()
        plans = store.records("plans")
        outcomes = store.records("outcomes")
        model_calls = store.records("model_calls")
        snapshots = store.snapshots()
        transcript = store.transcript()
    finally:
        store.close()
    scenario = load_scenario(config["scenario"])
    requirements = scenario["initial_request"]["requirements"]
    displayed = [p["proposal"] for p in plans if p.get("displayed")]
    out: list[dict[str, Any]] = []

    bad = [p["proposal_id"] for p in displayed if not (p["validation"]["hard_constraints_ok"] and p["validation"]["quotes_fresh"])]
    out.append(_check("displayed_plans_compliant", "No displayed plan violates a hard constraint or relies on an invalid quote.",
                      not bad, f"violations: {bad}" if bad else f"{len(displayed)} displayed plans checked"))

    mismatches = []
    for p in displayed:
        fq, hq = world.quotes[p["flight_quote_id"]], world.quotes[p["hotel_quote_id"]]
        expected = fq.unit_amount_cents * (fq.scope.adults + fq.scope.children) + hq.total_cents
        if expected != p["computed_total_cents"]:
            mismatches.append(p["proposal_id"])
    out.append(_check("totals_match_arithmetic", "Displayed totals equal per-traveler fare x seats + whole-stay hotel price.",
                      not mismatches, f"mismatches: {mismatches}" if mismatches else "all totals recomputed from the fixture"))

    accept = next((c for c in changes if c.event.type == "HotelAccepted"), None)
    if accept is None:
        out.append(_check("accepted_hotel_preserved", "Plans after acceptance keep the accepted hotel.", None, "no hotel was accepted in this run"))
    else:
        later = [p for p in displayed if p["turn"] >= accept.turn]
        drift = [p["proposal_id"] for p in later if p["hotel_id"] != accept.event.hotel_id and p["confirmation_status"] != "reconfirmation_required"]
        out.append(_check("accepted_hotel_preserved", "Plans after acceptance keep the accepted hotel (or flag the replacement for confirmation).",
                          not drift, f"drifted: {drift}" if drift else f"{len(later)} later plans keep {accept.event.hotel_id}"))

    earlier = next((c for c in changes if c.event.type == "EarlierFlightRequested"), None)
    if earlier is None:
        out.append(_check("earlier_anchor_respected", "Flights after the request arrive before the original reference.", None, "no earlier flight was requested"))
    else:
        anchor = earlier.event.reference_arrival_local
        later = [p for p in displayed if p["turn"] >= earlier.turn]
        late = [p["proposal_id"] for p in later if not world.quotes[p["flight_quote_id"]].arrive_local < anchor]
        out.append(_check("earlier_anchor_respected", f"Flights after the request arrive before {anchor} (from {earlier.event.reference_proposal_id}).",
                          not late, f"not earlier: {late}" if late else f"{len(later)} later plans arrive before {anchor}"))

    authority = Authority.from_changes(requirements, changes, world)
    acc = authority.accepted_hotel
    repriced = [p for p in displayed if acc and p["hotel_id"] == acc.hotel_id and acc.accepted_total_cents is not None
                and world.quotes[p["hotel_quote_id"]].total_cents != acc.accepted_total_cents]
    if not repriced:
        out.append(_check("price_change_flagged", "A plan that shows the accepted hotel at a new price asks for reconfirmation.", None, "no repriced plan was displayed"))
    else:
        unflagged = [p["proposal_id"] for p in repriced if p["confirmation_status"] != "reconfirmation_required"]
        out.append(_check("price_change_flagged", "A plan that shows the accepted hotel at a new price asks for reconfirmation.",
                          not unflagged, f"unflagged: {unflagged}" if unflagged else f"flagged: {[p['proposal_id'] for p in repriced]}"))

    incomplete = [o["turn"] for o in outcomes if o["kind"] == "incomplete"]
    out.append(_check("turns_finished", "Every processed turn ended with a validated plan or a question.", not incomplete,
                      f"incomplete turns: {incomplete}" if incomplete else f"{len(outcomes)} turns finished"))

    # If the accepted hotel can no longer fit the budget with any flight, the agent must ask.
    if acc and authority.party and authority.budget and outcomes:
        hotel = world.hotel(acc.hotel_id)
        last = outcomes[-1]
        if hotel and hotel["capacity"] >= authority.party.occupancy:
            _, hotel_total = world.price("hotel", hotel, authority.party)
            fares = [world.price("flight", f, authority.party)[1] for f in world.inventory["flights"]
                     if f["seats_available"] >= authority.party.seats and f["depart_date"] == str(authority.depart_date)]
            infeasible = bool(fares) and hotel_total + min(fares) > authority.budget.max_total_cents
            if infeasible:
                ok = last["kind"] == "question_asked"
                out.append(_check("no_compliant_plan_asks", "When no compliant plan keeps the accepted hotel, the agent asks a targeted tradeoff question.",
                                  ok, f"final outcome: {last['kind']}; {last.get('question') or last.get('reason') or ''}"))
            else:
                out.append(_check("no_compliant_plan_asks", "When no compliant plan keeps the accepted hotel, the agent asks.", None,
                                  "a compliant plan with the accepted hotel exists"))

    labels_ok = all(mc["planner_mode"] == config["planner"] for mc in model_calls)
    out.append(_check("planner_mode_labeled", "Every model call is labeled with the configured planner mode.", labels_ok,
                      f"planner_mode={config['planner']}", category="integrity"))

    if config["implementation"] == "transcript":
        missing = []
        for mc in model_calls:
            inputs_so_far = [e for e in transcript if e.role in ("user", "notification") and e.turn <= mc["turn"] and "display" in (e.normalized or {})]
            sent = [m for m in mc["input"]["messages"] if m["role"] in ("user", "notification") and "display" in (m.get("normalized") or {})]
            if len(sent) < len(inputs_so_far):
                missing.append(mc["index"])
        out.append(_check("transcript_complete", "The transcript agent received the complete history and current notifications on every call.",
                          not missing, f"short inputs at calls {missing}" if missing else f"{len(model_calls)} calls checked", category="integrity"))

    if config["implementation"] == "state":
        out.extend(_state_snapshot_checks(snapshots, changes))
    return out


def _state_snapshot_checks(snapshots: list[dict[str, Any]], changes: list[Change]) -> list[dict[str, Any]]:
    out = []
    by_change = {s["change_id"]: s for s in snapshots}
    party = next((c for c in changes if c.event.type == "PartySizeChanged"), None)
    if party and party.change_id in by_change:
        snap = by_change[party.change_id]
        before, after = snap["before"]["requirements"], snap["after"]["requirements"]
        keep = ["origin", "destination", "depart_date", "return_date", "nights", "budget", "walk_preference"]
        changed = [k for k in keep if before.get(k) != after.get(k)]
        out.append(_check("state_party_change_preserves", "Adding the child preserves destination, dates, budget, and walking preference.",
                          not changed and after["party"] == {"adults": party.event.adults, "children": party.event.children},
                          f"changed: {changed}" if changed else "preserved", category="state"))
        stale = [qid for qid, oq in snap["after"]["quotes"].items() if oq["status"] == "current"
                 and (oq["quote"]["scope"]["adults"], oq["quote"]["scope"]["children"]) != (party.event.adults, party.event.children)]
        out.append(_check("state_old_party_quotes_not_current", "Two-person quotes stop being current when the party grows.", not stale,
                          f"still current: {stale}" if stale else "all old-party quotes marked", category="state"))
        small = {oq["quote"]["item_id"] for oq in snap["after"]["quotes"].values()
                 if oq["quote"]["item_kind"] == "hotel" and (oq["quote"]["capacity"] or 0) < party.event.adults + party.event.children}
        unexplained = [h for h in small if not (snap["after"]["unsuitable_items"].get(h) or {}).get("reason")]
        out.append(_check("state_small_hotel_unsuitable", "Hotels too small for the new party are marked unsuitable with a reason.", not unexplained,
                          f"missing: {unexplained}" if unexplained else f"marked: {sorted(small)}", category="state"))
    accept = next((c for c in changes if c.event.type == "HotelAccepted"), None)
    if accept and accept.change_id in by_change:
        dec = (by_change[accept.change_id]["after"]["decisions"] or {}).get("hotel")
        out.append(_check("state_acceptance_recorded", "The hotel acceptance is stored as a separate decision.",
                          bool(dec) and dec["hotel_id"] == accept.event.hotel_id and dec["accepted_quote_id"] == accept.event.quote_id,
                          json.dumps(dec)[:200], category="state"))
    earlier = next((c for c in changes if c.event.type == "EarlierFlightRequested"), None)
    if earlier and snapshots:
        final_pref = snapshots[-1]["after"]["requirements"].get("earlier_arrival") or {}
        dec = (by_change.get(earlier.change_id, {}).get("after", {}).get("decisions") or {}).get("hotel")
        out.append(_check("state_acceptance_survives_earlier", "The accepted hotel survives the earlier-flight request.",
                          bool(dec) and accept is not None and dec["hotel_id"] == accept.event.hotel_id, json.dumps(dec)[:200], category="state"))
        out.append(_check("state_earlier_anchor_fixed", "The earlier-arrival reference stays anchored to the original proposal.",
                          final_pref.get("reference_arrival_local") == earlier.event.reference_arrival_local
                          and final_pref.get("reference_proposal_id") == earlier.event.reference_proposal_id,
                          json.dumps(final_pref)[:200], category="state"))
    expiry = [c for c in changes if c.event.type == "QuoteExpired"]
    if expiry and expiry[-1].change_id in by_change:
        after = by_change[expiry[-1].change_id]["after"]
        dec = (after["decisions"] or {}).get("hotel")
        statuses = {c.event.quote_id: (after["quotes"].get(c.event.quote_id) or {}).get("status") for c in expiry}
        refresh = [t for t in after["pending"] if t["status"] == "open" and t["kind"] == "refresh_hotel_quote"]
        out.append(_check("state_expiry_keeps_choice", "Quote expiry marks the quote expired, keeps the hotel choice, and opens a refresh task.",
                          bool(dec) and accept is not None and dec["hotel_id"] == accept.event.hotel_id
                          and all(s == "expired" for s in statuses.values()) and bool(refresh),
                          f"decision={bool(dec)} statuses={statuses} refresh_tasks={len(refresh)}", category="state"))
    if snapshots and accept:
        final = snapshots[-1]["after"]
        dec = (final["decisions"] or {}).get("hotel") or {}
        repriced = any(oq["quote"]["item_id"] == accept.event.hotel_id and oq["status"] == "current"
                       and dec.get("accepted_total_cents") is not None and oq["quote"]["total_cents"] != dec.get("accepted_total_cents")
                       for oq in final["quotes"].values())
        if repriced:
            out.append(_check("state_price_rise_reconfirm", "A price rise on the accepted hotel marks the decision for reconfirmation.",
                              dec.get("status") == "needs_reconfirmation" and dec.get("requires_reconfirmation") is True,
                              f"status={dec.get('status')} requires_reconfirmation={dec.get('requires_reconfirmation')}", category="state"))
    if snapshots:
        still_open = [f"{t['kind']}({t['target']})" for t in snapshots[-1]["after"]["pending"] if t["status"] == "open"]
        out.append(_check("state_no_stuck_tasks", "No pending task stays open at the end of the run.", not still_open,
                          f"open: {still_open}" if still_open else "all tasks closed", category="state"))
    return out


# ======================================================================================
# Exercise checks (student functions)
# ======================================================================================


class Stage(Exception):
    """Raised when a check depends on a stage that could not be built."""


def _classify(exc: BaseException) -> str:
    from .service import StudentCodeError, student_location

    if isinstance(exc, StudentCodeError) and exc.__cause__ is not None:
        return _classify(exc.__cause__)
    if isinstance(exc, NotImplementedError):
        return "todo"
    if isinstance(exc, AssertionError):
        return "fail"
    if student_location(exc):
        return "student_error"
    return "infrastructure_error"


class Script:
    """The canonical scenario driven with fixed tool calls, so only the student code varies."""

    def __init__(self, strategy: Strategy):
        self.strategy = strategy
        self.requirements = load_scenario("changing-trip")["initial_request"]["requirements"]
        self.world = World()
        self.changes: list[Change] = []
        self.seq = 0
        self.calls = 0
        self.toolbox = ToolBox(self.world, self._validate, lambda args: [])
        self.state = initial_state("check", self.requirements)
        self.stages: dict[str, TaskState] = {"initial": self.state}
        self.errors: dict[str, BaseException] = {}
        self.proposals = 0

    def _validate(self, fq: str, hq: str, claimed: int | None) -> dict[str, Any]:
        authority = Authority.from_changes(self.requirements, self.changes, self.world)
        return validate_plan(self.world, authority, fq, hq, claimed).model_dump(mode="json")

    def _change(self, turn: int, source: str, event: dict[str, Any], cid: str | None = None) -> Change:
        self.seq += 1
        return Change.model_validate({"change_id": cid or f"chg-{self.seq}", "seq": self.seq, "turn": turn, "source": source,
                                      "at": self.world.clock.now, "event": event})

    def apply(self, change: Change) -> None:
        self.changes.append(change)
        self.state, _ = apply_logged(self.state, change, self.strategy.apply_change)

    def tool(self, turn: int, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        call_id = f"tc-{self.calls:04d}"
        result = self.toolbox.execute(name, args, call_id)
        self.apply(self._change(turn, "tool", ToolObservationReceived(tool_call_id=call_id, tool_name=name, arguments=args, payload=result).model_dump(mode="json")))
        return result

    def present(self, turn: int, fq: str, hq: str) -> Proposal:
        authority = Authority.from_changes(self.requirements, self.changes, self.world)
        v = validate_plan(self.world, authority, fq, hq, None)
        assert v.compliant, f"scripted plan should validate: {v.codes()}"
        self.proposals += 1
        p = Proposal(proposal_id=f"plan-{self.proposals:03d}", turn=turn, flight_id=v.flight_id or "", hotel_id=v.hotel_id or "",
                     flight_quote_id=fq, hotel_quote_id=hq, requirement_revision=authority.revision, computed_total_cents=v.computed_total_cents,
                     claimed_total_cents=None, validation=v,
                     confirmation_status="reconfirmation_required" if v.confirmation_required else "pending_user_review",
                     explanation="scripted", presented_at=self.world.clock.now)
        self.apply(self._change(turn, "agent", {"type": "ProposalPresented", "proposal": p.model_dump(mode="json")}))
        return p

    def trip(self, adults: int, children: int) -> dict[str, Any]:
        return {"depart_date": "2027-04-12", "return_date": "2027-04-15", "adults": adults, "children": children}

    def build(self) -> None:
        steps: list[tuple[str, Callable[[], None]]] = [
            ("initial_plan", self._initial),
            ("party_changed", self._party),
            ("accepted_and_earlier", self._accept_earlier),
            ("third_plan", self._third),
            ("quote_expired", self._expire),
            ("quote_refreshed", self._refresh),
        ]
        for name, fn in steps:
            try:
                fn()
                self.stages[name] = self.state
            except BaseException as exc:  # noqa: BLE001 - classified below
                self.errors[name] = exc
                break

    def _initial(self) -> None:
        t = self.trip(2, 0)
        self.tool(0, "search_hotels", {"check_in": t["depart_date"], "check_out": t["return_date"], "adults": 2, "children": 0})
        self.tool(0, "search_flights", {"origin": "RDU", "destination": "CHI", **t, "arrive_before": None})
        self.present(0, "quote-f1-r1-2a0c", "quote-h11-r1-2a0c")

    def _party(self) -> None:
        self.world.clock.set_offset(10)
        self.apply(self._change(1, "user", {"type": "PartySizeChanged", "adults": 2, "children": 1}, "chg-t1-0"))
        self.stages["party_changed_raw"] = self.state
        t = self.trip(2, 1)
        self.tool(1, "search_hotels", {"check_in": t["depart_date"], "check_out": t["return_date"], "adults": 2, "children": 1})
        self.tool(1, "search_flights", {"origin": "RDU", "destination": "CHI", **t, "arrive_before": None})
        self.present(1, "quote-f1-r1-2a1c", "quote-h17-r1-2a1c")

    def _accept_earlier(self) -> None:
        self.world.clock.set_offset(20)
        self.apply(self._change(2, "user", {"type": "HotelAccepted", "hotel_id": "h17", "proposal_id": "plan-002", "quote_id": "quote-h17-r1-2a1c"}, "chg-t2-0"))
        self.apply(self._change(2, "user", {"type": "EarlierFlightRequested", "reference_proposal_id": "plan-002", "reference_flight_id": "f1",
                                            "reference_arrival_local": "18:00"}, "chg-t2-1"))

    def _third(self) -> None:
        t = self.trip(2, 1)
        self.tool(2, "search_flights", {"origin": "RDU", "destination": "CHI", **t, "arrive_before": "18:00"})
        self.present(2, "quote-f2-r1-2a1c", "quote-h17-r1-2a1c")

    def _expire(self) -> None:
        self.world.clock.set_offset(40)
        expired = self.world.apply_event({"type": "reprice", "item_id": "h17", "new_revision": 2})
        for k, qid in enumerate(sorted(expired)):
            self.apply(self._change(3, "world", {"type": "QuoteExpired", "quote_id": qid, "item_id": "h17"}, f"chg-t3-{k}"))

    def _refresh(self) -> None:
        self.tool(3, "refresh_quote", {"item_id": "h17", **self.trip(2, 1)})


def _step(purpose: str = "respond_to_change") -> PlanningStep:
    return PlanningStep(purpose=purpose, turn=1, step=0, trigger="check", tool_calls_remaining=8)  # type: ignore[arg-type]


def _content(ctx: Any, name: str) -> Any:
    s = ctx.section(name)
    return s.content if s is not None else None


def exercise_checks(strategy: Strategy) -> list[dict[str, Any]]:
    script = Script(strategy)
    script.build()

    def need(stage: str) -> TaskState:
        if stage in script.stages:
            return script.stages[stage]
        failed = next(iter(script.errors), None)
        exc = script.errors.get(failed) if failed else None
        raise Stage(f"stage {stage!r} was not reached; building {failed!r} raised {type(exc).__name__}: {exc}") from exc

    checks: list[tuple[str, str, Callable[[], str]]] = []

    def check(cid: str, desc: str) -> Callable[[Callable[[], str]], Callable[[], str]]:
        def deco(fn: Callable[[], str]) -> Callable[[], str]:
            checks.append((cid, desc, fn))
            return fn
        return deco

    @check("child_preserves_requirements", "Adding a child preserves destination, dates, budget, and walking preference.")
    def _() -> str:
        before, after = need("initial_plan").requirements, need("party_changed_raw").requirements
        for f in ("origin", "destination", "depart_date", "return_date", "nights", "budget", "walk_preference"):
            assert getattr(before, f) == getattr(after, f), f"{f} changed: {getattr(before, f)} -> {getattr(after, f)}"
        assert after.party is not None and (after.party.adults, after.party.children) == (2, 1), f"party is {after.party}"
        return "requirements preserved; party is 2 adults, 1 child"

    @check("old_quotes_not_current", "Two-person quotes stop being current (kept on record, not deleted).")
    def _() -> str:
        s = need("party_changed_raw")
        old = [oq for oq in s.quotes.values() if oq.quote.scope.children == 0]
        assert old, "two-person quotes were deleted; keep them and change their status"
        current = [oq.quote.quote_id for oq in old if oq.status == QuoteStatus.CURRENT]
        assert not current, f"still current: {current}"
        return f"{len(old)} two-person quotes kept with non-current status"

    @check("small_hotel_unsuitable_with_reason", "h11 (capacity 2) becomes unsuitable and the reason is recorded.")
    def _() -> str:
        s = need("party_changed_raw")
        note = s.unsuitable_items.get("h11")
        assert note is not None, "h11 not in unsuitable_items"
        assert note.reason, "reason is empty"
        return note.reason

    @check("proposal_flagged_after_party_change", "The two-person proposal's total is no longer trusted.")
    def _() -> str:
        s = need("party_changed_raw")
        assert s.proposal is not None and s.proposal.status != "current", f"proposal status is {s.proposal.status if s.proposal else None}"
        return f"proposal status {s.proposal.status}"

    @check("pending_work_after_party_change", "Adding a child opens pending work (requote, room capacity, revalidate).")
    def _() -> str:
        tasks = open_tasks(need("party_changed_raw"))
        assert tasks, "no open pending tasks"
        return ", ".join(f"{t.kind.value}({t.target})" for t in tasks)

    @check("two_person_quote_cannot_validate", "The validator rejects a two-person quote for a three-person plan (infrastructure).")
    def _() -> str:
        need("party_changed")
        authority = Authority.from_changes(script.requirements, script.changes, script.world)
        # h30 never reprices in this scenario, so the only quote problem is the party scope.
        v = validate_plan(script.world, authority, "quote-f1-r1-2a0c", "quote-h30-r1-2a1c")
        assert "quote_scope_mismatch" in v.codes(), f"codes: {v.codes()}"
        return ", ".join(v.codes())

    @check("acceptance_recorded", "Hotel acceptance is stored as a decision with its proposal and quote.")
    def _() -> str:
        d = need("accepted_and_earlier").decisions.hotel
        assert d is not None, "decisions.hotel is None"
        assert (d.hotel_id, d.accepted_from_proposal_id, d.accepted_quote_id) == ("h17", "plan-002", "quote-h17-r1-2a1c"), d.model_dump_json()
        return f"{d.hotel_id} from {d.accepted_from_proposal_id}, status {d.status.value}"

    @check("acceptance_survives_earlier_request", "The accepted hotel survives the request for an earlier flight.")
    def _() -> str:
        d = need("accepted_and_earlier").decisions.hotel
        assert d is not None and d.hotel_id == "h17" and d.status == DecisionStatus.ACCEPTED, f"decision: {d}"
        return "h17 still accepted"

    @check("earlier_anchor_set", "The earlier-arrival preference records 18:00 from plan-002.")
    def _() -> str:
        pref = need("accepted_and_earlier").requirements.earlier_arrival
        assert pref is not None, "requirements.earlier_arrival is None"
        assert (pref.reference_arrival_local, pref.reference_proposal_id) == ("18:00", "plan-002"), pref.model_dump_json()
        return "anchored to 18:00 (plan-002)"

    @check("earlier_anchor_stays_fixed", "Presenting a 12:00 flight does not move the 18:00 reference.")
    def _() -> str:
        pref = need("third_plan").requirements.earlier_arrival
        assert pref is not None and pref.reference_arrival_local == "18:00" and pref.reference_proposal_id == "plan-002", f"anchor moved: {pref}"
        return "still 18:00 (plan-002) after plan-003"

    @check("expiry_keeps_hotel_identity", "Quote expiry keeps the hotel choice, marks the quote expired, and opens a refresh task.")
    def _() -> str:
        s = need("quote_expired")
        d = s.decisions.hotel
        assert d is not None and d.hotel_id == "h17", f"hotel decision lost: {d}"
        oq = s.quotes.get("quote-h17-r1-2a1c")
        assert oq is not None and oq.status == QuoteStatus.EXPIRED, f"quote status: {oq.status if oq else 'missing'}"
        refresh = [t for t in open_tasks(s) if t.kind == TaskKind.REFRESH_HOTEL_QUOTE and t.target == "h17"]
        assert refresh, "no open refresh_hotel_quote task for h17"
        return "h17 kept; quote expired; refresh task open"

    @check("expiry_flags_proposal", "The proposal priced with the expired quote needs a recheck.")
    def _() -> str:
        s = need("quote_expired")
        assert s.proposal is not None and s.proposal.status == "needs_recheck", f"proposal status: {s.proposal.status if s.proposal else None}"
        return "plan-003 needs_recheck"

    @check("price_rise_needs_reconfirmation", "A refreshed $900 quote marks the $720 acceptance for reconfirmation.")
    def _() -> str:
        d = need("quote_refreshed").decisions.hotel
        assert d is not None and d.hotel_id == "h17", f"decision: {d}"
        assert d.status == DecisionStatus.NEEDS_RECONFIRMATION and d.requires_reconfirmation, f"status={d.status.value} requires_reconfirmation={d.requires_reconfirmation}"
        return d.reason or "needs reconfirmation"

    @check("duplicate_change_is_noop", "Applying the same change twice leaves state unchanged.")
    def _() -> str:
        s = need("initial_plan")
        change = Change.model_validate({"change_id": "dup", "seq": 999, "turn": 1, "source": "user", "at": script.world.clock.now,
                                        "event": {"type": "PartySizeChanged", "adults": 2, "children": 1}})
        once, _ = apply_logged(s, change, strategy.apply_change)
        twice, applied = apply_logged(once, change, strategy.apply_change)
        assert not applied and once.model_dump() == twice.model_dump(), "second application changed state"
        return "no-op"

    @check("missing_data_explicit", "Unknown required inputs appear in the context's unknowns section.")
    def _() -> str:
        reqs = dict(script.requirements)
        reqs.pop("party")
        s = initial_state("missing", reqs)
        ctx = strategy.build_context(s, _step())
        unknowns = _content(ctx, "unknowns") or []
        assert "party" in unknowns, f"unknowns section: {unknowns}"
        return f"unknowns: {unknowns}"

    @check("context_required_sections", "Context includes requirements, decisions, current quotes, unknowns, and pending work.")
    def _() -> str:
        ctx = strategy.build_context(need("quote_refreshed"), _step("reconsider_after_tool_result"))
        names = {s.name for s in ctx.sections}
        required = {"requirements", "decisions", "current_quotes", "unknowns", "pending_work"}
        assert required <= names, f"missing sections: {sorted(required - names)}"
        return ", ".join(s.name for s in ctx.sections)

    @check("context_current_vs_historical", "Old-party quotes are not presented as current; they appear only as history.")
    def _() -> str:
        ctx = strategy.build_context(need("party_changed_raw"), _step())
        current = _content(ctx, "current_quotes") or []
        ids = [q.get("quote_id") for q in current]
        assert not [i for i in ids if i and i.endswith("2a0c")], f"two-person quotes listed as current: {ids}"
        historical = json.dumps([s.content for s in ctx.sections if s.temporal == "historical"], default=str)
        assert "2a0c" in historical, "two-person quotes are not available in any historical section"
        return "current and historical separated"

    @check("context_shows_decision_and_work", "After expiry, context shows the accepted hotel and the refresh task.")
    def _() -> str:
        ctx = strategy.build_context(need("quote_expired"), _step())
        decision = (_content(ctx, "decisions") or {}).get("hotel") or {}
        assert decision.get("hotel_id") == "h17", f"decisions section: {decision}"
        work = _content(ctx, "pending_work") or []
        assert any(w.get("kind") == "refresh_hotel_quote" for w in work), f"pending_work: {work}"
        return "decision and refresh task visible"

    results = []
    for cid, desc, fn in checks:
        try:
            detail = fn()
            results.append({"id": cid, "description": desc, "status": "pass", "detail": detail})
        except Stage as exc:
            cause = exc.__cause__
            results.append({"id": cid, "description": desc, "status": _classify(cause) if cause else "infrastructure_error", "detail": str(exc)})
        except BaseException as exc:  # noqa: BLE001
            results.append({"id": cid, "description": desc, "status": _classify(exc),
                            "detail": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(limit=4)})
    return results


def run_checks(strategy_name: str = "student", output: Path | None = None) -> dict[str, Any]:
    """Exercise checks plus simulated state-agent runs of both changing scenarios."""
    from .service import WorkshopService

    try:
        strategy = load_strategy(strategy_name)
    except Exception as exc:  # noqa: BLE001
        report = {"strategy": strategy_name, "overall": _classify(exc),
                  "error": f"Could not import the {strategy_name} functions: {type(exc).__name__}: {exc}", "exercise_checks": [], "scenario_runs": []}
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2))
        return report

    exercise = exercise_checks(strategy)
    runs = []
    with tempfile.TemporaryDirectory(prefix="workshop-check-") as tmp:
        service = WorkshopService(root=Path(tmp))
        for scenario in ("changing-trip", "no-compliant"):
            name = f"check-{scenario}"
            try:
                service.create(name, implementation="state", scenario=scenario, planner="simulated", strategy=strategy_name)
                service.run_to_completion(name)
                checks = scenario_checks(service.path(name))
                runs.append({"scenario": scenario, "status": "ran", "checks": checks})
            except BaseException as exc:  # noqa: BLE001
                runs.append({"scenario": scenario, "status": _classify(exc), "error": f"{type(exc).__name__}: {exc}",
                             "traceback": traceback.format_exc(limit=6), "checks": []})

    statuses = [c["status"] for c in exercise]
    run_statuses = [r["status"] for r in runs]
    run_fail = any(st != "ran" for st in run_statuses) or any(c["passed"] is False for r in runs for c in r["checks"])
    every = statuses + run_statuses
    if "infrastructure_error" in every:
        overall = "infrastructure_error"
    elif "student_error" in every:
        overall = "student_error"
    elif "todo" in every:
        overall = "todo"
    elif all(st == "pass" for st in statuses) and not run_fail:
        overall = "pass"
    else:
        overall = "fail"
    report = {
        "strategy": strategy_name,
        "module": strategy.module_file,
        "overall": overall,
        "counts": {s: statuses.count(s) for s in sorted(set(statuses))},
        "exercise_checks": exercise,
        "scenario_runs": runs,
    }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2))
    return report


def format_report(report: dict[str, Any]) -> str:
    symbols = {"pass": "PASS", "fail": "FAIL", "todo": "TODO", "student_error": "ERROR (your code)", "infrastructure_error": "ERROR (infrastructure)"}
    lines = [f"Exercise checks for strategy={report['strategy']}"]
    if report.get("error"):
        lines.append(f"  {report['error']}")
    for c in report["exercise_checks"]:
        lines.append(f"  {symbols.get(c['status'], c['status']):<24} {c['id']}: {c['detail']}")
    for r in report["scenario_runs"]:
        lines.append(f"Simulated state-agent run, scenario={r['scenario']}: {r['status']}")
        if r.get("error"):
            lines.append(f"  {r['error']}")
        for c in r["checks"]:
            mark = "PASS" if c["passed"] else ("n/a " if c["passed"] is None else "FAIL")
            lines.append(f"  {mark:<24} {c['id']}: {c['detail']}")
    verdict = {
        "pass": "All checks pass.",
        "todo": "Some TODOs are not implemented yet (NotImplementedError).",
        "fail": "Some checks fail. Read the detail lines above; they describe your function's result.",
        "infrastructure_error": "Infrastructure error: something outside workshop/exercises.py broke. Tell the instructor.",
        "student_error": "Your code raised an exception; see the detail above.",
    }[report["overall"]]
    lines.append(f"Overall: {report['overall'].upper()}. {verdict}")
    return "\n".join(lines)
