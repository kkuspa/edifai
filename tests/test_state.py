"""State behavior: what the reference apply_change and build_context preserve, invalidate, and expose.

The scripted sequence comes from ``workshop.checks.Script``: fixed tool calls against the real
tools and the real validator, so only the state functions under test vary. The reference
functions stand in for a finished student solution.
"""

from __future__ import annotations

import json

import pytest

from tests.helpers import run_session
from workshop.checks import Script
from workshop.context import render_context
from workshop.models import (
    Change,
    DecisionStatus,
    PlanningStep,
    QuoteStatus,
    TaskKind,
    TaskState,
    Requirements,
)
from workshop.runner import Engine, SessionConfig, load_scenario, load_strategy
from workshop.state import apply_logged, current_quotes, initial_state, open_tasks
from workshop.store import ConflictingChangeError, RunStore
from workshop.tools import World, load_clock_start, load_inventory
from workshop.validation import Authority

PRESERVED = ("origin", "destination", "depart_date", "return_date", "nights", "budget", "walk_preference")


@pytest.fixture
def script(reference) -> Script:
    s = Script(reference)
    s.build()
    assert not s.errors, s.errors
    return s


def step(purpose: str = "respond_to_change") -> PlanningStep:
    return PlanningStep(purpose=purpose, turn=1, step=0, trigger="test", tool_calls_remaining=8)  # type: ignore[arg-type]


def kinds(state: TaskState, status: str = "open") -> list[tuple[str, str | None]]:
    return [(t.kind.value, t.target) for t in state.pending if t.status == status]


# ---------------------------------------------------------------------------------------
# Party grows
# ---------------------------------------------------------------------------------------


def test_adding_a_child_preserves_everything_except_the_party(script):
    before, after = script.stages["initial_plan"].requirements, script.stages["party_changed_raw"].requirements
    for name in PRESERVED:
        assert getattr(before, name) == getattr(after, name), name
    assert (before.party.adults, before.party.children) == (2, 0)
    assert (after.party.adults, after.party.children) == (2, 1)
    assert after.party.seats == after.party.occupancy == 3, "a child needs a seat and counts toward occupancy"
    assert after.revision == before.revision + 1
    assert after.earlier_arrival is None, "no earlier-arrival preference exists yet"


def test_old_party_quotes_are_marked_stale_and_kept_on_record(script):
    before, after = script.stages["initial_plan"], script.stages["party_changed_raw"]
    two_person = [oq for oq in after.quotes.values() if oq.quote.scope.children == 0]
    assert len(two_person) == len(before.quotes) > 0, "nothing was deleted"
    assert all(oq.status == QuoteStatus.STALE for oq in two_person)
    assert all(oq.status_reason and "2a0c" in oq.status_reason and oq.status_changed_by == "chg-t1-0" for oq in two_person)
    assert not current_quotes(after), "no quote prices the three-person trip yet"


def test_two_person_hotel_becomes_unsuitable_and_the_reason_survives(script):
    after = script.stages["party_changed_raw"]
    note = after.unsuitable_items["h11"]
    assert "capacity 2" in note.reason.lower() and "occupancy 3" in note.reason
    assert note.since_change_id == "chg-t1-0" and note.requirement_revision == 2
    assert "h17" not in after.unsuitable_items, "the three-person suite still fits"
    # Later transitions must not erase the explanation.
    for stage in ("accepted_and_earlier", "third_plan", "quote_expired", "quote_refreshed"):
        assert script.stages[stage].unsuitable_items["h11"] == note, stage


def test_party_change_flags_the_old_total_and_opens_the_right_work(script):
    after = script.stages["party_changed_raw"]
    assert after.proposal.proposal_id == "plan-001"
    assert after.proposal.status == "needs_recheck" and "Party changed" in after.proposal.status_reason
    assert after.proposal.computed_total_cents == 120_000, "the stored total is kept as history but not trusted"
    assert set(kinds(after)) == {
        (TaskKind.REFRESH_FLIGHT_QUOTE.value, "f1"),
        (TaskKind.FIND_HOTEL_CAPACITY.value, None),
        (TaskKind.REVALIDATE_PLAN.value, None),
    }


def test_party_change_with_no_actual_difference_changes_nothing(reference, script):
    state = script.stages["initial_plan"]
    same = Change.model_validate({"change_id": "same-party", "seq": 500, "turn": 1, "source": "user", "at": script.world.clock.now,
                                 "event": {"type": "PartySizeChanged", "adults": 2, "children": 0}})
    new, applied = apply_logged(state, same, reference.apply_change)
    assert applied
    assert new.requirements == state.requirements and new.quotes == state.quotes and new.pending == state.pending


# ---------------------------------------------------------------------------------------
# Acceptance and the earlier-flight request
# ---------------------------------------------------------------------------------------


def test_acceptance_is_stored_as_its_own_decision(script):
    d = script.stages["accepted_and_earlier"].decisions.hotel
    assert (d.hotel_id, d.accepted_from_proposal_id, d.accepted_quote_id) == ("h17", "plan-002", "quote-h17-r1-2a1c")
    assert d.accepted_total_cents == 72_000 and d.accepted_by_change_id == "chg-t2-0"
    assert d.status == DecisionStatus.ACCEPTED and d.requires_reconfirmation is False
    assert script.stages["party_changed_raw"].decisions.hotel is None


def test_hotel_acceptance_survives_the_earlier_flight_request(script):
    for stage in ("accepted_and_earlier", "third_plan", "quote_expired"):
        d = script.stages[stage].decisions.hotel
        assert d is not None and d.hotel_id == "h17" and d.status == DecisionStatus.ACCEPTED, stage
    after = script.stages["accepted_and_earlier"]
    assert after.requirements.party.children == 1 and after.requirements.budget.max_total_cents == 180_000
    assert after.proposal.status == "needs_recheck", "the flight in plan-002 is now in question; the hotel choice is not"
    assert (TaskKind.SEARCH_EARLIER_FLIGHT.value, "18:00") in kinds(after)


def test_earlier_arrival_stays_anchored_to_the_referenced_proposal(script):
    pref = script.stages["accepted_and_earlier"].requirements.earlier_arrival
    assert (pref.reference_proposal_id, pref.reference_flight_id, pref.reference_arrival_local) == ("plan-002", "f1", "18:00")
    assert pref.requested_by_change_id == "chg-t2-1"
    assert pref.direction == "outbound_arrival" and pref.comparison == "strictly_before" and pref.time_zone == "America/Chicago"

    # Present two more proposals, one arriving earlier and one arriving at the reference time.
    # The reference must follow neither of them.
    script.present(3, "quote-f3-r1-2a1c", "quote-h17-r2-2a1c")  # 15:00 arrival
    assert script.state.requirements.earlier_arrival.reference_arrival_local == "18:00"
    script.present(3, "quote-f1-r1-2a1c", "quote-h17-r2-2a1c")  # 18:00 arrival, exactly $1,800
    final = script.state.requirements.earlier_arrival
    assert (final.reference_arrival_local, final.reference_proposal_id) == ("18:00", "plan-002")
    assert script.state.requirements.revision == 3, "proposals are not requirement changes"
    authority = Authority.from_changes(script.requirements, script.changes, script.world)
    assert (authority.earlier_reference_arrival, authority.earlier_reference_proposal_id) == ("18:00", "plan-002")


# ---------------------------------------------------------------------------------------
# Quote expiry and price rise
# ---------------------------------------------------------------------------------------


def test_expiry_keeps_the_hotel_identity_and_opens_a_refresh(script):
    after = script.stages["quote_expired"]
    d = after.decisions.hotel
    assert d.hotel_id == "h17" and d.status == DecisionStatus.ACCEPTED, "expiry alone is not a price change"
    old = after.quotes["quote-h17-r1-2a1c"]
    assert old.status == QuoteStatus.EXPIRED and old.quote.total_cents == 72_000, "the old price stays on record"
    assert (TaskKind.REFRESH_HOTEL_QUOTE.value, "h17") in kinds(after)
    assert after.proposal.status == "needs_recheck" and "expired" in after.proposal.status_reason
    assert any("selection of h17 is unchanged" in n.text for n in after.notes)
    # Unrelated quotes keep their status.
    assert after.quotes["quote-f2-r1-2a1c"].status == QuoteStatus.CURRENT


def test_a_price_increase_invalidates_the_old_total_and_marks_reconfirmation(script):
    after = script.stages["quote_refreshed"]
    d = after.decisions.hotel
    assert d.status == DecisionStatus.NEEDS_RECONFIRMATION and d.requires_reconfirmation is True
    assert d.hotel_id == "h17" and d.accepted_total_cents == 72_000, "the identity and the accepted price both survive"
    assert "$720" in d.reason and "$900" in d.reason
    assert after.proposal.status == "needs_recheck", "plan-003 priced the hotel at $720"
    assert (TaskKind.CONFIRM_WITH_USER.value, "h17") in kinds(after)
    new = after.quotes["quote-h17-r2-2a1c"]
    assert new.status == QuoteStatus.CURRENT and new.quote.total_cents == 90_000


def test_the_refresh_task_closes_on_evidence_and_the_new_quote_is_current(script):
    after = script.stages["quote_refreshed"]
    refresh = [t for t in after.pending if t.kind == TaskKind.REFRESH_HOTEL_QUOTE]
    assert len(refresh) == 1 and refresh[0].status == "done"
    assert refresh[0].closed_by == after.quotes["quote-h17-r2-2a1c"].quote.tool_call_id


def test_presenting_a_reconfirmed_plan_closes_the_confirmation_task(script):
    flights = script.tool(3, "search_flights", {"origin": "RDU", "destination": "CHI", **script.trip(2, 1), "arrive_before": "18:00"})
    f3 = next(q["quote_id"] for q in flights["quotes"] if q["item_id"] == "f3")
    proposal = script.present(3, f3, "quote-h17-r2-2a1c")
    assert proposal.confirmation_status == "reconfirmation_required"
    state = script.state
    assert state.proposal.proposal_id == proposal.proposal_id and state.proposal.status == "current"
    assert [p.proposal_id for p in state.proposal_history][-1] == "plan-003", "the previous proposal moves to history"
    assert not open_tasks(state), kinds(state)


def test_expiring_an_unrelated_quote_opens_no_work(reference, script):
    state = script.stages["third_plan"]
    before_tasks = [t.model_dump() for t in state.pending]
    change = Change.model_validate({"change_id": "exp-h30", "seq": 600, "turn": 3, "source": "world", "at": script.world.clock.now,
                                    "event": {"type": "QuoteExpired", "quote_id": "quote-h30-r1-2a1c", "item_id": "h30"}})
    new, _ = apply_logged(state, change, reference.apply_change)
    assert new.quotes["quote-h30-r1-2a1c"].status == QuoteStatus.EXPIRED
    assert [t.model_dump() for t in new.pending] == before_tasks
    assert new.proposal.status == "current"


# ---------------------------------------------------------------------------------------
# Pending work: tasks cannot vanish early and finished work does not repeat
# ---------------------------------------------------------------------------------------


def test_pending_work_only_moves_forward(script):
    names = ["party_changed_raw", "accepted_and_earlier", "third_plan", "quote_expired", "quote_refreshed"]
    for earlier, later in zip(names, names[1:]):
        before = {t.task_id: t for t in script.stages[earlier].pending}
        after = {t.task_id: t for t in script.stages[later].pending}
        assert set(before) <= set(after), f"{earlier} -> {later}: a task disappeared"
        for task_id, t in before.items():
            assert (after[task_id].kind, after[task_id].target) == (t.kind, t.target)
            assert not (t.status == "done" and after[task_id].status == "open"), f"{task_id} reopened"


def test_finished_work_is_not_reopened_by_repeating_the_observation(script):
    state = script.stages["quote_refreshed"]
    tasks = [t.model_dump() for t in state.pending]
    script.tool(3, "refresh_quote", {"item_id": "h17", **script.trip(2, 1)})  # same quote again
    script.tool(3, "search_hotels", {"check_in": "2027-04-12", "check_out": "2027-04-15", "adults": 2, "children": 1})
    assert [t.model_dump() for t in script.state.pending] == tasks
    assert len(open_tasks(script.state)) == len(open_tasks(state))


def test_no_two_open_tasks_share_a_kind_and_target(script):
    for name, state in script.stages.items():
        open_ = kinds(state)
        assert len(open_) == len(set(open_)), (name, open_)


# ---------------------------------------------------------------------------------------
# Selective updates, not wholesale ones
# ---------------------------------------------------------------------------------------


def test_nothing_is_cleared_and_nothing_stale_is_current(script):
    names = ["initial_plan", "party_changed_raw", "accepted_and_earlier", "third_plan", "quote_expired", "quote_refreshed"]
    for earlier, later in zip(names, names[1:]):
        a, b = script.stages[earlier], script.stages[later]
        assert set(a.quotes) <= set(b.quotes), "quotes are never deleted"
        assert len(b.notes) >= len(a.notes) and len(b.searches) >= len(a.searches)
        for name in PRESERVED:
            assert getattr(a.requirements, name) == getattr(b.requirements, name), (later, name)
    final = script.stages["quote_refreshed"]
    for oq in current_quotes(final):
        assert oq.quote.scope.children == 1 and oq.quote.inventory_revision == (2 if oq.quote.item_id == "h17" else 1)


def test_state_functions_do_not_mutate_their_input(reference):
    s = Script(reference)
    seen: list[tuple[TaskState, dict, Change]] = []
    original_apply = s.apply

    def apply(change: Change) -> None:
        before = s.state
        snapshot = before.model_dump()
        original_apply(change)
        seen.append((before, snapshot, change))

    s.apply = apply  # type: ignore[method-assign]
    s.build()
    assert len(seen) >= 10
    for before, snapshot, change in seen:
        assert before.model_dump() == snapshot, f"{change.change_id} mutated its input state"


def test_the_independent_authority_and_the_task_state_agree_on_requirements(script):
    state = script.stages["quote_refreshed"]
    authority = Authority.from_changes(script.requirements, script.changes, script.world)
    assert state.requirements.party == authority.party
    assert state.requirements.budget == authority.budget and state.requirements.walk_preference == authority.walk
    assert state.requirements.earlier_arrival.reference_arrival_local == authority.earlier_reference_arrival
    assert state.requirements.revision == authority.revision
    assert state.decisions.hotel.hotel_id == authority.accepted_hotel.hotel_id
    assert state.decisions.hotel.accepted_total_cents == authority.accepted_hotel.accepted_total_cents


def test_replaying_the_change_log_rebuilds_the_same_state(reference, script):
    state = initial_state("check", script.requirements)
    for change in script.changes:
        state, _ = apply_logged(state, change, reference.apply_change)
    assert state.model_dump() == script.state.model_dump()


# ---------------------------------------------------------------------------------------
# Context: current versus historical
# ---------------------------------------------------------------------------------------


def test_context_keeps_history_available_without_posing_as_current(reference, script):
    ctx = reference.build_context(script.stages["party_changed_raw"], step())
    current = ctx.section("current_quotes")
    assert current.temporal == "current" and current.content == [], "no three-person quote exists at this point"
    historical = ctx.section("historical")
    assert historical.temporal == "historical"
    stale = historical.content["inactive_quotes"]
    assert stale and all(q["status"] == "stale" and q["quote_id"].endswith("2a0c") for q in stale)
    assert [n["item_id"] for n in ctx.section("unsuitable_items").content] == ["h11"]
    assert {t["kind"] for t in ctx.section("pending_work").content} == {"refresh_flight_quote", "find_hotel_capacity", "revalidate_plan"}
    assert ctx.section("current_proposal").content["status"] == "needs_recheck"
    assert any("Party changed" in note for note in historical.content["notes"])


def test_context_after_the_price_change_shows_decision_work_and_both_prices(reference, script):
    ctx = reference.build_context(script.stages["quote_refreshed"], step("reconsider_after_tool_result"))
    decision = ctx.section("decisions").content["hotel"]
    assert decision["status"] == "needs_reconfirmation" and decision["accepted_total_cents"] == 72_000
    assert decision["scope"].endswith("not a booking")
    current = {q["quote_id"]: q for q in ctx.section("current_quotes").content}
    assert current["quote-h17-r2-2a1c"]["total_cents"] == 90_000 and "quote-h17-r1-2a1c" not in current
    expired = {q["quote_id"] for q in ctx.section("historical").content["inactive_quotes"] if q["status"] == "expired"}
    assert "quote-h17-r1-2a1c" in expired
    prefs = ctx.section("requirements").content["preferences"]["earlier_arrival"]
    assert prefs["arrive_before_local"] == "18:00" and prefs["reference_proposal_id"] == "plan-002"
    assert {s.name for s in ctx.sections} >= {"requirements", "unknowns", "decisions", "current_quotes", "unsuitable_items",
                                              "current_proposal", "pending_work", "validation_feedback", "historical", "history_access"}


def test_context_purpose_is_a_reason_not_an_answer_key(reference, script):
    ctx = reference.build_context(script.stages["quote_expired"], step("reconsider_after_tool_result"))
    purpose = ctx.section("purpose")
    assert purpose.temporal == "instructions"
    assert set(purpose.content) == {"purpose", "turn", "step", "trigger", "tool_calls_remaining_this_turn"}
    rendered = render_context(ctx)
    assert "next_action" not in rendered and "expected" not in rendered.lower()
    assert rendered.count("Choose exactly one action") == 1


def test_rendered_context_labels_each_section_current_or_historical(reference, script):
    rendered = render_context(reference.build_context(script.stages["quote_refreshed"], step()))
    assert "## current_quotes [CURRENT]" in rendered and "## historical [HISTORICAL]" in rendered
    assert "## purpose [INSTRUCTIONS]" in rendered


# ---------------------------------------------------------------------------------------
# Missing data
# ---------------------------------------------------------------------------------------


def test_missing_data_is_explicit_in_state_and_context(reference):
    requirements = dict(load_scenario("changing-trip")["initial_request"]["requirements"])
    requirements.pop("party")
    requirements.pop("budget")
    state = initial_state("missing", requirements)
    assert state.requirements.party is None and state.requirements.budget is None
    assert state.requirements.missing_fields() == ["party", "budget"]
    ctx = reference.build_context(state, step())
    assert ctx.section("unknowns").content == ["party", "budget"]
    assert "party" in render_context(ctx)
    assert ctx.section("requirements").content["hard_constraints"]["party"] is None


def test_optional_preferences_are_not_reported_as_missing():
    requirements = dict(load_scenario("changing-trip")["initial_request"]["requirements"])
    requirements.pop("walk_preference")
    assert initial_state("x", requirements).requirements.missing_fields() == []


def test_complete_requirements_have_no_unknowns(reference):
    state = initial_state("x", load_scenario("changing-trip")["initial_request"]["requirements"])
    assert state.requirements.missing_fields() == []
    assert reference.build_context(state, step()).section("unknowns").content == []


# ---------------------------------------------------------------------------------------
# Duplicate and conflicting changes
# ---------------------------------------------------------------------------------------


def make_change(change_id: str, children: int, seq: int = 1) -> Change:
    return Change.model_validate({"change_id": change_id, "seq": seq, "turn": 1, "source": "user", "at": load_clock_start(),
                                  "event": {"type": "PartySizeChanged", "adults": 2, "children": children}})


def test_an_identical_repeated_change_is_a_noop(reference, script):
    state = script.stages["initial_plan"]
    change = make_change("dup-1", 1)
    once, first = apply_logged(state, change, reference.apply_change)
    again, second = apply_logged(once, change, reference.apply_change)
    rebuilt, third = apply_logged(again, Change.model_validate(change.model_dump(mode="json")), reference.apply_change)
    assert first and not second and not third
    assert again.model_dump() == once.model_dump() == rebuilt.model_dump()
    assert once.applied_change_ids.count("dup-1") == 1
    assert once.revision == state.revision + 1 and once.requirements.revision == state.requirements.revision + 1


def test_the_store_ignores_an_identical_repeat_and_rejects_a_conflicting_payload(tmp_path):
    store = RunStore(tmp_path / "s.sqlite")
    original = make_change("c-1", 1)
    assert store.record_change(original) is True
    assert store.record_change(make_change("c-1", 1)) is False, "same ID, same payload: a no-op"
    with pytest.raises(ConflictingChangeError):
        store.record_change(make_change("c-1", 2))
    assert [c.event.children for c in store.changes()] == [1], "the conflicting payload was not stored"
    assert len(store.changes()) == 1


def test_redelivering_a_recorded_change_does_not_alter_a_live_session(service):
    run_session(service, "dup", "state")
    path = service.path("dup")
    store = RunStore(path / "session.sqlite")
    try:
        config = SessionConfig.model_validate(store.get("config"))
        engine = Engine(store, config, None, load_strategy("reference"))
        before, snapshots, changes = engine.state.model_dump(), len(store.snapshots()), len(store.changes())
        for change in store.changes():
            engine._record_and_apply(change)
        assert engine.state.model_dump() == before
        assert len(store.snapshots()) == snapshots and len(store.changes()) == changes
    finally:
        store.close()


# ---------------------------------------------------------------------------------------
# Isolation between runs
# ---------------------------------------------------------------------------------------


def test_worlds_do_not_share_inventory_clock_or_quotes():
    a, b = World(), World()
    a.apply_event({"type": "reprice", "item_id": "h17", "new_revision": 2})
    a.inventory["hotels"][0]["available"] = False
    a.inventory["flights"][0]["seats_available"] = 0
    a.clock.advance(500)
    assert b.revisions["h17"] == 1 and b.events == []
    assert b.hotel("h11")["available"] is True and b.flight("f1")["seats_available"] == 9
    assert b.clock.now == load_clock_start() and load_inventory()["hotels"][0]["available"] is True


def test_a_world_copies_the_inventory_it_is_given():
    inventory = load_inventory()
    world = World(inventory)
    inventory["hotels"][0]["capacity"] = 99
    assert world.hotel("h11")["capacity"] == 2


def test_changes_in_one_session_do_not_leak_into_another(service):
    run_session(service, "a", "state")  # reprices h17 and advances the clock
    service.create("b", implementation="state", strategy="reference")
    stored = {}
    for name in ("a", "b"):
        store = RunStore(service.path(name) / "session.sqlite")
        stored[name] = (store.get("world"), store.load_state())
        store.close()
    (world_a, state_a), (world_b, state_b) = stored["a"], stored["b"]
    assert world_a["revisions"]["h17"] == 2 and len(world_a["events"]) == 1 and world_a["clock_now"] != world_a["clock_start"]
    assert world_b["revisions"]["h17"] == 1 and world_b["events"] == [] and world_b["quotes"] == {}
    assert world_b["clock_now"] == world_b["clock_start"] == load_clock_start().isoformat()
    assert state_b.requirements.party.children == 0 and not state_b.quotes and state_b.decisions.hotel is None
    assert state_a.requirements.party.children == 1
    assert service.path("a") / "session.sqlite" != service.path("b") / "session.sqlite"


def test_interleaved_sessions_match_a_session_run_alone(service):
    from tests.helpers import displayed_plans, plan_summary

    for name in ("x", "y"):
        service.create(name, implementation="state", strategy="reference")
    while any(service.status(n)["cursor"]["phase"] != "complete" for n in ("x", "y")):
        for name in ("x", "y"):
            st = service.status(name)
            if st["can_apply_change"]:
                service.apply_change(name)
            elif st["can_step"]:
                service.step(name)
    run_session(service, "alone", "state")
    alone = [plan_summary(p) for p in displayed_plans(service, "alone")]
    assert [plan_summary(p) for p in displayed_plans(service, "x")] == alone
    assert [plan_summary(p) for p in displayed_plans(service, "y")] == alone
    assert [p["total_cents"] for p in alone] == [120_000, 162_000, 168_000, 174_000]


def test_each_new_session_starts_from_the_fixture_clock_and_revisions(service):
    run_session(service, "first", "transcript")
    service.create("second", implementation="transcript")
    store = RunStore(service.path("second") / "session.sqlite")
    world = World.from_json(store.get("world"))
    store.close()
    assert world.clock.now == load_clock_start() and set(world.revisions.values()) == {1}
    assert service.status("second")["clock"] == load_clock_start().isoformat()


# ---------------------------------------------------------------------------------------
# Persistence round trips
# ---------------------------------------------------------------------------------------


def test_task_state_round_trips_through_json_and_sqlite(tmp_path, script):
    final = script.stages["quote_refreshed"]
    assert TaskState.model_validate(final.model_dump(mode="json")) == final
    store = RunStore(tmp_path / "state.sqlite")
    store.save_state(final)
    store.close()
    reopened = RunStore(tmp_path / "state.sqlite")
    loaded = reopened.load_state()
    reopened.close()
    assert loaded == final and loaded.model_dump() == final.model_dump()
    assert loaded.decisions.hotel.status == DecisionStatus.NEEDS_RECONFIRMATION
    assert Requirements.model_validate(final.requirements.model_dump(mode="json")) == final.requirements


def test_a_finished_session_reloads_with_identical_state_transcript_and_changes(service):
    run_session(service, "persist", "state")
    path = service.path("persist")
    first = RunStore(path / "session.sqlite")
    snapshots, transcript, changes, state = first.snapshots(), first.transcript(), first.changes(), first.load_state()
    first.close()
    second = RunStore(path / "session.sqlite")  # a different connection, as a different process would use
    try:
        assert second.snapshots() == snapshots
        assert second.transcript() == transcript and second.changes() == changes
        assert second.load_state() == state
        assert snapshots[-1]["after"] == state.model_dump(mode="json"), "the last snapshot is the current state"
        assert [e.index for e in transcript] == list(range(len(transcript)))
        on_disk = json.loads((path / "transcript.json").read_text())
        assert on_disk == [e.model_dump(mode="json") for e in transcript]
    finally:
        second.close()


def test_transcript_entries_keep_tool_calls_normalized_data_and_raw_output(tmp_path):
    from workshop.models import ToolCallRecord, TranscriptEntry

    store = RunStore(tmp_path / "t.sqlite")
    entries = [
        TranscriptEntry(turn=0, role="user", content="plan a trip", normalized={"kind": "initial_request", "display": "plan a trip"}),
        TranscriptEntry(turn=0, role="assistant", content="", tool_call=ToolCallRecord(id="toolu_1", name="search_hotels", arguments={"adults": 2}),
                        provider_raw={"provider": "anthropic", "content": [{"type": "thinking", "thinking": "hm", "signature": "sig"}]}),
        TranscriptEntry(turn=0, role="tool", content='{"ok": true}', tool_call_id="toolu_1", normalized={"ok": True}),
        TranscriptEntry(turn=1, role="notification", content="expired"),
    ]
    saved = [store.append_transcript(e) for e in entries]
    store.close()
    again = RunStore(tmp_path / "t.sqlite")
    assert again.transcript() == saved
    assert again.transcript()[1].provider_raw["content"][0]["signature"] == "sig"
    again.close()


def test_world_state_round_trips(script):
    world = script.world
    restored = World.from_json(json.loads(json.dumps(world.to_json())))
    assert restored.to_json() == world.to_json()
    assert restored.side_effect_fingerprint() == world.side_effect_fingerprint()
    assert restored.clock.now == world.clock.now and restored.expired_quote_ids == world.expired_quote_ids
    assert all(restored.quote_expired(q) == world.quote_expired(q) for q in world.quotes.values())
