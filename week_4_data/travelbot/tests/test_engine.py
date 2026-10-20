"""The action-observation loop: reference path, limits, the validation gate, and honest outcomes.

Behavior tests use the reference strategy and the deterministic simulator, plus small stub
planners that make the engine face situations a simulator never produces: planners that loop,
planners that propose stale or over-budget plans, planners that fail.
"""

from __future__ import annotations

import json

import pytest

from tests.helpers import (
    BUDGET_CENTS,
    RewritePlanner,
    ScriptedPlanner,
    add_scenario,
    displayed_plans,
    install_planner,
    outcome_kinds,
    record_counts,
    run_session,
)
from workshop.models import AgentContext, CallTool, Party, PlanDraft, PresentPlan
from workshop.planners import SimulatedPlanner
from workshop.runner import MAX_STEPS_PER_TURN, MAX_TOOL_CALLS_PER_TURN, Engine, SessionConfig, load_strategy
from workshop.store import RunStore
from workshop.tools import World, load_inventory

REFERENCE_PATH = [
    ("plan-001", "f1", "h11", 120_000, "pending_user_review"),
    ("plan-002", "f1", "h17", 162_000, "pending_user_review"),
    ("plan-003", "f2", "h17", 168_000, "pending_user_review"),
    ("plan-004", "f3", "h17", 174_000, "reconfirmation_required"),
]
AGENTS = ["state", "transcript"]


def open_engine(service, name: str, planner=None) -> tuple[Engine, RunStore]:
    store = RunStore(service.path(name) / "session.sqlite")
    config = SessionConfig.model_validate(store.get("config"))
    strategy = load_strategy(config.strategy) if config.implementation == "state" else None
    return Engine(store, config, planner, strategy), store


# ---------------------------------------------------------------------------------------
# The reference path
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("which", AGENTS)
def test_agents_follow_the_reference_path(runs, which):
    plans = displayed_plans(runs.service, getattr(runs, which))
    got = [(p["proposal_id"], p["flight_id"], p["hotel_id"], p["computed_total_cents"], p["confirmation_status"]) for p in plans]
    assert got == REFERENCE_PATH
    assert [p["turn"] for p in plans] == [0, 1, 2, 3]
    assert [p["requirement_revision"] for p in plans] == [1, 2, 3, 3]
    assert plans[-1]["flight_quote_id"] == "quote-f3-r1-2a1c" and plans[-1]["hotel_quote_id"] == "quote-h17-r2-2a1c"
    assert outcome_kinds(runs.service, getattr(runs, which)) == ["plan_presented"] * 4


@pytest.mark.parametrize("which", AGENTS)
def test_the_over_budget_combination_is_checked_and_never_shown(runs, which):
    name = getattr(runs, which)
    over = [v for v in runs.service.records(name, "validations") if v["result"]["computed_total_cents"] == 186_000]
    assert over, "the agent evaluated f2 x 3 + refreshed h17 = $1,860"
    result = over[0]["result"]
    assert "over_budget" in [r["code"] for r in result["reasons"]] and result["hard_constraints_ok"] is False
    shown = {p["computed_total_cents"] for p in displayed_plans(runs.service, name)}
    assert 186_000 not in shown


@pytest.mark.parametrize("name_attr", ["one_shot", "one_shot_restated", "transcript", "state", "state_nc", "transcript_nc"])
def test_no_displayed_plan_violates_a_hard_constraint(runs, name_attr):
    name = getattr(runs, name_attr)
    plans = displayed_plans(runs.service, name)
    assert plans
    for p in plans:
        assert p["computed_total_cents"] <= BUDGET_CENTS, p["proposal_id"]
        assert p["validation"]["hard_constraints_ok"] and p["validation"]["quotes_fresh"], p["proposal_id"]
        assert p["confirmation_status"] in ("pending_user_review", "reconfirmation_required")


@pytest.mark.parametrize("name_attr", ["one_shot", "transcript", "state", "state_nc"])
def test_displayed_totals_are_recomputed_from_the_world(runs, name_attr):
    name = getattr(runs, name_attr)
    store = RunStore(runs.service.path(name) / "session.sqlite")
    world = World.from_json(store.get("world"))
    store.close()
    for p in displayed_plans(runs.service, name):
        fq, hq = world.quotes[p["flight_quote_id"]], world.quotes[p["hotel_quote_id"]]
        assert fq.item_kind == "flight" and hq.item_kind == "hotel"
        assert fq.unit_amount_cents * (fq.scope.adults + fq.scope.children) + hq.total_cents == p["computed_total_cents"]
        assert (fq.item_id, hq.item_id) == (p["flight_id"], p["hotel_id"])


def test_validity_and_confirmation_are_reported_separately(runs):
    plans = {p["proposal_id"]: p for p in displayed_plans(runs.service, runs.state)}
    last = plans["plan-004"]
    assert last["validation"]["hard_constraints_ok"] and last["validation"]["quotes_fresh"], "valid"
    assert last["validation"]["confirmation_required"] and last["confirmation_status"] == "reconfirmation_required", "but awaiting confirmation"
    assert [r["code"] for r in last["validation"]["reasons"]] == ["confirmation_required"]
    assert "$900" in last["explanation"] and "$720" in last["explanation"], "the price change is disclosed"
    assert "connects" in last["explanation"], "the connection is disclosed"
    assert not plans["plan-003"]["validation"]["confirmation_required"]


def test_every_quote_a_run_issued_traces_to_a_recorded_tool_call(runs):
    for name in (runs.state, runs.transcript, runs.one_shot):
        store = RunStore(runs.service.path(name) / "session.sqlite")
        world = World.from_json(store.get("world"))
        store.close()
        call_ids = {t["tool_call_id"] for t in runs.service.records(name, "tool_calls")}
        assert world.quotes
        for q in world.quotes.values():
            assert q.tool_call_id in call_ids, (name, q.quote_id)
            assert q.quote_id.startswith(f"quote-{q.item_id}-r{q.inventory_revision}-")


# ---------------------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------------------


def test_scenario_changes_are_applied_in_order_with_resolved_references(runs):
    changes = runs.service.changes(runs.state)
    user_world = [c for c in changes if c["source"] in ("user", "world")]
    assert [c["change_id"] for c in user_world] == ["chg-t1-0", "chg-t2-0", "chg-t2-1", "chg-t3-0", "chg-t3-1"]
    assert len({c["change_id"] for c in changes}) == len(changes)
    seqs = [c["seq"] for c in changes]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "sequence numbers increase monotonically"
    events = {c["change_id"]: c["event"] for c in user_world}
    assert events["chg-t1-0"] == {"type": "PartySizeChanged", "adults": 2, "children": 1}
    assert events["chg-t2-0"] == {"type": "HotelAccepted", "hotel_id": "h17", "proposal_id": "plan-002", "quote_id": "quote-h17-r1-2a1c"}
    assert events["chg-t2-1"] == {"type": "EarlierFlightRequested", "reference_proposal_id": "plan-002", "reference_flight_id": "f1",
                                  "reference_arrival_local": "18:00"}
    assert {events["chg-t3-0"]["quote_id"], events["chg-t3-1"]["quote_id"]} == {"quote-h17-r1-2a0c", "quote-h17-r1-2a1c"}
    assert {e["item_id"] for k, e in events.items() if k.startswith("chg-t3")} == {"h17"}


def test_one_message_with_two_changes_gets_no_planning_step_between_them(runs):
    changes = runs.service.changes(runs.state)
    by_id = {c["change_id"]: c for c in changes}
    assert by_id["chg-t2-1"]["seq"] == by_id["chg-t2-0"]["seq"] + 1, "nothing was recorded between the two changes"
    assert by_id["chg-t2-0"]["message_id"] == by_id["chg-t2-1"]["message_id"] == "msg-t2"
    snapshot_ids = [s["change_id"] for s in RunStore(runs.service.path(runs.state) / "session.sqlite").snapshots()]
    assert snapshot_ids.index("chg-t2-1") == snapshot_ids.index("chg-t2-0") + 1
    # And the first planning step of the turn comes after both changes.
    first_call = next(c for c in runs.service.records(runs.state, "model_calls") if c["turn"] == 2)
    assert first_call["trigger"].endswith("chg-t2-0, chg-t2-1") and first_call["purpose"] == "respond_to_change"


def test_the_world_event_and_its_notification_are_recorded(runs):
    path = runs.service.path(runs.state)
    changes_jsonl = [json.loads(line) for line in (path / "changes.jsonl").read_text().splitlines()]
    assert [c.get("change_id") for c in changes_jsonl if "change_id" in c] == ["chg-t1-0", "chg-t2-0", "chg-t2-1", "chg-t3-0", "chg-t3-1"]
    world_events = [c["world_event"] for c in changes_jsonl if "world_event" in c]
    assert [(e["type"], e["item_id"], e["new_revision"]) for e in world_events] == [("reprice", "h17", 2)]
    notification = [e for e in runs.service.transcript(runs.transcript) if e["role"] == "notification"]
    assert len(notification) == 1 and "expired" in notification[0]["content"] and notification[0]["turn"] == 3


def test_applying_a_change_does_not_plan(service):
    service.create("p", implementation="state", strategy="reference")
    service.run_turn("p")
    before = record_counts(service, "p")
    result = service.apply_change("p")
    after = record_counts(service, "p")
    assert result["phase"] == "ready" and result["detail"]["applied"] == ["chg-t1-0"]
    assert (after["model_calls"], after["tool_calls"]) == (before["model_calls"], before["tool_calls"])
    assert after["changes"] == before["changes"] + 1 and after["transcript"] == before["transcript"] + 1


def test_step_mode_pauses_after_every_change_before_any_planning(service):
    service.create("s", implementation="state", strategy="reference")
    engine, store = open_engine(service, "s", SimulatedPlanner())
    pauses = []

    def pause(applied):
        pauses.append((applied["turn"], len(store.records("model_calls")), engine.cursor.phase))

    try:
        engine.run_to_completion(pause)
        calls_at_end_of_turn = []
        total = 0
        for turn in range(4):
            total += sum(1 for c in store.records("model_calls") if c["turn"] == turn)
            calls_at_end_of_turn.append(total)
    finally:
        store.close()
    assert [p[0] for p in pauses] == [1, 2, 3]
    assert [p[1] for p in pauses] == calls_at_end_of_turn[:3], "the pause comes before the turn's first model call"
    assert {p[2] for p in pauses} == {"ready"}


def test_the_acceptance_resolves_against_the_proposal_actually_displayed(service, monkeypatch):
    """Turn 1 shows f3 + h17 instead of the usual f1 + h17, so 'earlier' must anchor to 15:00."""

    replacement = PresentPlan(plan=PlanDraft(flight_quote_id="quote-f3-r1-2a1c", hotel_quote_id="quote-h17-r1-2a1c"), explanation="One connection, cheaper.")
    install_planner(monkeypatch, RewritePlanner(swap_once(lambda a: isinstance(a, PresentPlan) and a.plan.hotel_quote_id == "quote-h17-r1-2a1c", replacement)))
    run_session(service, "anchor", "transcript")
    plans = {p["proposal_id"]: p for p in displayed_plans(service, "anchor")}
    assert plans["plan-002"]["flight_id"] == "f3"
    events = {c["change_id"]: c["event"] for c in service.changes("anchor")}
    assert events["chg-t2-1"]["reference_flight_id"] == "f3" and events["chg-t2-1"]["reference_arrival_local"] == "15:00"
    assert events["chg-t2-1"]["reference_proposal_id"] == "plan-002"
    assert plans["plan-003"]["flight_id"] in ("f2", "f4"), "the next flight arrives before 15:00"
    assert "15:00" in next(e for e in service.transcript("anchor") if e["turn"] == 2 and e["role"] == "user")["content"]


@pytest.mark.parametrize("implementation", AGENTS)
def test_an_acceptance_that_differs_from_the_scenario_expectation_follows_the_display_and_says_so(service, monkeypatch, implementation):
    replacement = PresentPlan(plan=PlanDraft(flight_quote_id="quote-f1-r1-2a1c", hotel_quote_id="quote-h22-r1-2a1c"), explanation="Cheaper family room.")
    install_planner(monkeypatch, RewritePlanner(swap_once(lambda a: isinstance(a, PresentPlan) and a.plan.hotel_quote_id == "quote-h17-r1-2a1c", replacement)))
    run_session(service, "differs", implementation)
    events = {c["change_id"]: c["event"] for c in service.changes("differs")}
    assert events["chg-t2-0"]["hotel_id"] == "h22" and events["chg-t2-0"]["quote_id"] == "quote-h22-r1-2a1c"
    notes = service.records("differs", "notes")
    assert notes and "expected hotel h17" in notes[0]["note"] and "h22" in notes[0]["note"]
    user_entry = next(e for e in service.transcript("differs") if e["turn"] == 2 and e["role"] == "user")
    assert user_entry["normalized"]["notes"]
    if implementation == "state":
        decision = service.state("differs")["state"]["decisions"]["hotel"]
        assert (decision["hotel_id"], decision["accepted_quote_id"], decision["accepted_total_cents"]) == ("h22", "quote-h22-r1-2a1c", 50_000)


def test_the_scenario_cannot_resolve_a_reference_when_nothing_was_displayed(service, monkeypatch):
    install_planner(monkeypatch, ScriptedPlanner([], then=CallTool(name="read_history", arguments={"role": "any"})))
    service.create("none", implementation="transcript")
    service.run_turn("none")  # incomplete: no plan was ever displayed
    service.apply_change("none")  # turn 1 only needs the new party
    service.run_turn("none")
    result = service.apply_change("none")  # turn 2 refers to "that hotel" and "the flight I was looking at"
    assert result["detail"]["applied"] == []
    assert [u["type"] for u in result["detail"]["unresolved"]] == ["HotelAccepted", "EarlierFlightRequested"]
    assert all("no proposal was displayed" in u["reason"] for u in result["detail"]["unresolved"])


# ---------------------------------------------------------------------------------------
# No compliant plan
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["state_nc", "transcript_nc"])
def test_when_nothing_complies_the_agent_asks_a_specific_budget_question(runs, which):
    name = getattr(runs, which)
    outcomes = runs.service.records(name, "outcomes")
    assert [o["kind"] for o in outcomes] == ["plan_presented"] * 3 + ["question_asked"]
    last = outcomes[-1]
    assert last["unresolved_constraint"] == "budget"
    world = World()
    world.revisions["h17"] = 3
    hotel = world.price("hotel", world.hotel("h17"), Party(adults=2, children=1))[1]
    cheapest_flight = min(world.price("flight", f, Party(adults=2, children=1))[1] for f in world.inventory["flights"]
                          if f["seats_available"] >= 3 and f["depart_date"] == "2027-04-12")
    lowest = hotel + cheapest_flight
    assert lowest == 204_000
    assert "$1,800" in last["question"] and "$2,040" in last["question"], "the question names the budget and the number that would fit"
    assert "h17" in last["question"] and "accepted" in last["question"]
    assert "raise the budget" in last["question"] and "different hotel" in last["question"], "the tradeoff is stated"
    assert not [p for p in displayed_plans(runs.service, name) if p["turn"] == 3], "no plan was presented in the last turn"
    checks = json.loads((runs.service.path(name) / "checks.json").read_text())
    assert {c["id"]: c["passed"] for c in checks}["no_compliant_plan_asks"] is True


def test_the_no_compliant_scenario_really_has_no_compliant_plan_with_the_accepted_hotel():
    inventory = load_inventory()
    assert inventory["quote_ttl_minutes"] == 120
    world = World()
    world.revisions["h17"] = 3
    party = Party(adults=2, children=1)
    hotel = world.price("hotel", world.hotel("h17"), party)[1]
    fares = [world.price("flight", f, party)[1] for f in world.inventory["flights"] if f["seats_available"] >= 3 and f["depart_date"] == "2027-04-12"]
    assert all(hotel + fare > BUDGET_CENTS for fare in fares)


# ---------------------------------------------------------------------------------------
# One-shot semantics
# ---------------------------------------------------------------------------------------


def test_one_shot_makes_one_bounded_call_per_request_without_a_tool_loop(runs):
    calls = runs.service.records(runs.one_shot, "model_calls")
    assert len(calls) == 1
    assert [t["name"] for t in calls[0]["input"]["tools"]] == ["present_plan", "ask_user"]
    assert "no search tools" in calls[0]["input"]["system"]
    assert runs.service.records(runs.one_shot, "tool_calls")[0]["caller"] == "application", "the application, not the model, fetched inventory"
    assert [p["computed_total_cents"] for p in displayed_plans(runs.service, runs.one_shot)] == [120_000]
    assert runs.service.state(runs.one_shot)["available"] is False


def test_the_one_shot_caller_must_restate_the_brief_or_the_model_has_to_ask(runs):
    unrestated = runs.service.records(runs.one_shot_unrestated, "outcomes")
    assert [o["kind"] for o in unrestated] == ["plan_presented", "question_asked", "question_asked", "question_asked"]
    assert "requirements" in unrestated[1]["question"]
    calls = runs.service.records(runs.one_shot_unrestated, "model_calls")
    turn1 = next(c for c in calls if c["turn"] == 1)["input"]["messages"][0]
    assert turn1["normalized"]["requirements"] is None and turn1["normalized"]["changes"], "only the change was supplied"

    restated = runs.service.records(runs.one_shot_restated, "model_calls")
    turn3 = next(c for c in restated if c["turn"] == 3)["input"]["messages"][0]
    assert turn3["content"].startswith("[Brief restated by the calling application]")
    brief = turn3["normalized"]["requirements"]
    assert brief["party"] == {"adults": 2, "children": 1} and brief["accepted_hotel"]["hotel_id"] == "h17"
    assert brief["earlier_arrival"]["arrive_before_local"] == "18:00"
    totals = [p["computed_total_cents"] for p in displayed_plans(runs.service, runs.one_shot_restated)]
    assert totals == [120_000, 162_000, 168_000, 174_000], "with the brief rebuilt by the caller, one call per turn is enough"


def test_a_one_shot_model_that_asks_for_a_tool_is_incomplete_not_obeyed(service, monkeypatch):
    install_planner(monkeypatch, ScriptedPlanner([], then=CallTool(name="search_hotels", arguments={})))
    service.create("os", implementation="one-shot", scenario="initial")
    service.run_to_completion("os")
    outcome = service.records("os", "outcomes")[0]
    assert outcome["kind"] == "incomplete" and "no tool loop" in outcome["reason"]
    assert [t for t in service.records("os", "tool_calls") if t["caller"] == "agent"] == []


def test_a_one_shot_plan_that_fails_validation_is_not_retried_or_shown(service, monkeypatch):
    bad = PresentPlan(plan=PlanDraft(flight_quote_id="quote-f4-r1-2a0c", hotel_quote_id="quote-h30-r1-2a0c"), explanation="Premium.")
    planner = install_planner(monkeypatch, ScriptedPlanner([bad], then=bad))
    service.create("os2", implementation="one-shot", scenario="initial")
    service.run_to_completion("os2")
    outcome = service.records("os2", "outcomes")[0]
    assert outcome["kind"] == "incomplete" and "over_budget" in outcome["reason"]
    assert displayed_plans(service, "os2") == [] and len(planner.inputs) == 1


# ---------------------------------------------------------------------------------------
# Limits end in explicit incomplete outcomes
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("implementation", ["transcript", "state"])
def test_a_planner_that_loops_on_tools_hits_the_tool_call_limit(service, monkeypatch, implementation):
    loop = install_planner(monkeypatch, ScriptedPlanner([], then=CallTool(name="read_history", arguments={"role": "any"})))
    service.create("loop", implementation=implementation, scenario="initial", strategy="reference")
    service.run_to_completion("loop")
    [outcome] = service.records("loop", "outcomes")
    assert outcome["kind"] == "incomplete"
    assert f"Tool-call limit ({MAX_TOOL_CALLS_PER_TURN})" in outcome["reason"] and MAX_TOOL_CALLS_PER_TURN == 8
    assert outcome["tool_calls"] == MAX_TOOL_CALLS_PER_TURN and outcome["steps"] == MAX_TOOL_CALLS_PER_TURN + 1
    assert len([t for t in service.records("loop", "tool_calls") if t["caller"] == "agent"]) == MAX_TOOL_CALLS_PER_TURN
    assert len(loop.inputs) == MAX_TOOL_CALLS_PER_TURN + 1, "the ninth request was refused, not executed"
    assert displayed_plans(service, "loop") == [], "no plan was invented"
    assert service.status("loop")["cursor"]["phase"] == "complete"
    last = service.transcript("loop")[-1]
    assert last["role"] == "tool" and "budget for this turn is exhausted" in last["content"]


def test_the_step_limit_also_ends_the_turn_as_incomplete(service, monkeypatch):
    bogus = PresentPlan(plan=PlanDraft(flight_quote_id="quote-nope", hotel_quote_id="quote-nada"), explanation="Trust me.")
    loop = install_planner(monkeypatch, ScriptedPlanner([], then=bogus))
    service.create("steps", implementation="transcript", scenario="initial")
    service.run_to_completion("steps")
    [outcome] = service.records("steps", "outcomes")
    assert outcome["kind"] == "incomplete" and f"Step limit ({MAX_STEPS_PER_TURN})" in outcome["reason"] and MAX_STEPS_PER_TURN == 12
    assert outcome["steps"] == MAX_STEPS_PER_TURN and outcome["tool_calls"] == 0
    assert len(loop.inputs) == MAX_STEPS_PER_TURN, "the planner is not asked a thirteenth time"
    plans = service.records("steps", "plans")
    assert len(plans) == MAX_STEPS_PER_TURN and not any(p["displayed"] for p in plans)
    assert all("quote_unknown" in [r["code"] for r in p["validation"]["reasons"]] for p in plans)


def test_limits_apply_to_each_turn_separately(service, monkeypatch):
    """A turn that ended incomplete does not poison the next turn's budget."""
    planner = ScriptedPlanner([], then=CallTool(name="read_history", arguments={"role": "any"}))
    install_planner(monkeypatch, planner)
    service.create("per-turn", implementation="transcript")
    service.run_turn("per-turn")
    service.apply_change("per-turn")
    service.run_turn("per-turn")
    outcomes = service.records("per-turn", "outcomes")
    assert [o["kind"] for o in outcomes] == ["incomplete", "incomplete"]
    assert [o["tool_calls"] for o in outcomes] == [8, 8]


def test_a_planner_failure_is_recorded_and_ends_the_turn(service, monkeypatch):
    install_planner(monkeypatch, ScriptedPlanner([None], fail_with=["provider error: RuntimeError: boom"]))
    service.create("fail", implementation="transcript", scenario="initial")
    service.run_to_completion("fail")
    [outcome] = service.records("fail", "outcomes")
    assert outcome["kind"] == "incomplete" and "boom" in outcome["reason"]
    [call] = service.records("fail", "model_calls")
    assert call["action"] is None and call["errors"] == ["provider error: RuntimeError: boom"]
    assert displayed_plans(service, "fail") == []


def test_stepping_outside_a_turn_is_refused_without_changing_anything(service):
    from workshop.service import InvalidOperation

    service.create("phase", implementation="transcript")
    with pytest.raises(InvalidOperation):
        service.apply_change("phase")  # the initial turn is not finished
    service.run_turn("phase")
    before = record_counts(service, "phase")
    with pytest.raises(InvalidOperation):
        service.step("phase")  # a change is due, not a step
    assert record_counts(service, "phase") == before
    assert service.status("phase")["cursor"]["version"] == 2


# ---------------------------------------------------------------------------------------
# The validation gate
# ---------------------------------------------------------------------------------------


def swap_once(predicate, replacement):
    state = {"done": False}

    def rewrite(index, mi, response):
        if state["done"] or not predicate(response.action):
            return None
        state["done"] = True
        return replacement(response.action) if callable(replacement) else replacement

    return rewrite


@pytest.mark.parametrize("implementation", AGENTS)
def test_a_plan_over_budget_is_rejected_with_reason_codes_and_never_shown(service, monkeypatch, implementation):
    over = PresentPlan(plan=PlanDraft(flight_quote_id="quote-f2-r1-2a1c", hotel_quote_id="quote-h17-r2-2a1c", claimed_total_cents=186_000),
                       explanation="Looks fine to me.")
    planner = install_planner(monkeypatch, RewritePlanner(swap_once(
        lambda a: isinstance(a, CallTool) and a.name == "validate_plan" and a.arguments["flight_quote_id"] == "quote-f3-r1-2a1c", over)))
    run_session(service, "gate", implementation)
    rejected = [p for p in service.records("gate", "plans") if not p["displayed"]]
    assert len(rejected) == 1
    codes = [r["code"] for r in rejected[0]["validation"]["reasons"]]
    assert "over_budget" in codes and rejected[0]["validation"]["computed_total_cents"] == 186_000
    assert rejected[0]["draft"] == over.plan.model_dump(), "the draft is recorded exactly as the planner sent it"
    assert any(v["source"] == "runner_gate" and v["result"]["computed_total_cents"] == 186_000 for v in service.records("gate", "validations"))
    shown = displayed_plans(service, "gate")
    assert [p["proposal_id"] for p in shown] == ["plan-001", "plan-002", "plan-003", "plan-004"], "a rejection does not consume a proposal ID"
    assert shown[-1]["computed_total_cents"] == 174_000
    # The planner was told, in its next input, which hard constraint failed.
    if implementation == "state":
        after = next(mi for mi in planner.inputs if mi.context and mi.context.purpose.purpose == "reconsider_after_rejection")
        feedback = after.context.section("validation_feedback").content
        assert any(r["code"] == "over_budget" for f in feedback for r in f["reasons"])
    else:
        after = next(mi for mi in planner.inputs if "Not shown to the user. Validation failed" in mi.messages[-1].content)
        assert "over_budget" in after.messages[-1].content


@pytest.mark.parametrize("implementation", AGENTS)
def test_a_stale_quote_cannot_support_a_displayed_plan_even_when_the_total_fits(service, monkeypatch, implementation):
    stale = PresentPlan(plan=PlanDraft(flight_quote_id="quote-f3-r1-2a1c", hotel_quote_id="quote-h17-r1-2a1c", claimed_total_cents=156_000),
                        explanation="Same hotel, same price as before.")
    install_planner(monkeypatch, RewritePlanner(swap_once(lambda a: isinstance(a, CallTool) and a.name == "refresh_quote" and a.arguments["item_id"] == "h17", stale)))
    run_session(service, "stale", implementation)
    rejected = [p for p in service.records("stale", "plans") if not p["displayed"]]
    assert len(rejected) == 1
    v = rejected[0]["validation"]
    assert v["computed_total_cents"] == 156_000 <= BUDGET_CENTS
    assert v["hard_constraints_ok"] is True and v["quotes_fresh"] is False, "affordable, but the evidence is gone"
    assert "quote_expired" in [r["code"] for r in v["reasons"]]
    assert 156_000 not in {p["computed_total_cents"] for p in displayed_plans(service, "stale")}
    assert displayed_plans(service, "stale")[-1]["hotel_quote_id"] == "quote-h17-r2-2a1c", "the agent went on to use the refreshed quote"


def test_a_wrong_claimed_total_does_not_become_the_displayed_total(service, monkeypatch):
    def lie(action):
        return PresentPlan(plan=action.plan.model_copy(update={"claimed_total_cents": 100}), explanation=action.explanation)

    install_planner(monkeypatch, RewritePlanner(swap_once(lambda a: isinstance(a, PresentPlan), lie)))
    run_session(service, "claim", "transcript")
    first = displayed_plans(service, "claim")[0]
    assert first["computed_total_cents"] == 120_000 and first["claimed_total_cents"] == 100
    assert "claimed_total_mismatch" in [r["code"] for r in first["validation"]["reasons"]]
    summary = next(e for e in service.transcript("claim") if e["role"] == "tool" and (e["normalized"] or {}).get("displayed_proposal"))
    assert "Computed total $1,200" in summary["content"]


# ---------------------------------------------------------------------------------------
# Student state cannot talk the validator into anything
# ---------------------------------------------------------------------------------------


def test_a_state_function_that_inflates_the_budget_cannot_get_an_over_budget_plan_shown(service, monkeypatch):
    from workshop.models import Budget

    ref = load_strategy("reference")

    def hostile_apply(state, change):
        new = ref.apply_change(state, change)
        new.requirements = new.requirements.model_copy(update={"budget": Budget(max_total_cents=10**9)})
        return new

    from workshop.runner import Strategy

    hostile = Strategy("student", hostile_apply, ref.build_context, ref.module_file)
    monkeypatch.setattr("workshop.service.load_strategy", lambda name: hostile)
    run_session(service, "hostile", "state", strategy="student")
    context_budgets = {
        s["content"]["hard_constraints"]["budget"]["max_total_cents"]
        for c in service.records("hostile", "model_calls") if c["turn"] >= 1 and c["input"]["context"]
        for s in c["input"]["context"]["sections"] if s["name"] == "requirements"
    }
    assert 10**9 in context_budgets, "the model really was told the budget was unlimited"
    shown = displayed_plans(service, "hostile")
    assert shown and all(p["computed_total_cents"] <= BUDGET_CENTS for p in shown)
    caught = [v for v in service.records("hostile", "validations") if "over_budget" in [r["code"] for r in v["result"]["reasons"]]]
    assert caught, "the independent validator, not the state, held the line"
    assert [c for c in json.loads((service.path("hostile") / "checks.json").read_text()) if c["id"] == "displayed_plans_compliant"][0]["passed"]


# ---------------------------------------------------------------------------------------
# read_history gives both agents exact access to the retained conversation
# ---------------------------------------------------------------------------------------


def history_results(service, name):
    return [t["result"] for t in service.records(name, "tool_calls") if t["name"] == "read_history"]


def test_read_history_filters_are_exact_and_not_semantic(service, monkeypatch):
    def call(**args):
        return CallTool(name="read_history", arguments={"role": "any", **args})

    reads = [call(role="user"), call(contains="ROUND-TRIP"), call(contains="youngster"), call(role="assistant"), call(limit=1), call(turn=5)]
    install_planner(monkeypatch, ScriptedPlanner(reads, then=None, fail_with=["finished reading"]))  # then=None ends the turn
    service.create("hist", implementation="transcript", scenario="initial")
    service.run_to_completion("hist")
    users, contains, none, assistants, limited, wrong_turn = history_results(service, "hist")
    assert [e["role"] for e in users["entries"]] == ["user"] and "three-night trip" in users["entries"][0]["content"]
    assert contains["entries"] and all("round-trip" in e["content"].lower() for e in contains["entries"])
    assert none["entries"] == [], "there is no embedding or synonym matching"
    assert assistants["entries"] and all(e["role"] == "assistant" for e in assistants["entries"])
    assert len(limited["entries"]) == 1 and limited["entries"][0]["role"] == "assistant", "limit keeps the most recent entry"
    assert wrong_turn["entries"] == []


def test_the_state_agent_can_read_the_retained_transcript(service, monkeypatch):
    def rewrite(index, mi, response):
        if rewrite.done or mi.context is None or mi.context.purpose.turn != 1:
            return None
        rewrite.done = True
        return CallTool(name="read_history", arguments={"role": "user", "contains": "child"})

    rewrite.done = False
    install_planner(monkeypatch, RewritePlanner(rewrite))
    run_session(service, "state-hist", "state")
    [result] = history_results(service, "state-hist")
    assert result["ok"] and [e["turn"] for e in result["entries"]] == [1]
    assert "bringing our child" in result["entries"][0]["content"]
    call = next(c for c in service.records("state-hist", "model_calls") if c["turn"] == 1)
    assert "read_history" in call["input"]["context"]["sections"][-1]["content"]["note"], "the context points the model at the tool"


# ---------------------------------------------------------------------------------------
# Tools stay read-only during a whole run
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("implementation", AGENTS)
def test_agent_steps_never_change_inventory_outside_scenario_events(service, implementation):
    service.create("ro", implementation=implementation, strategy="reference")
    engine, store = open_engine(service, "ro", SimulatedPlanner())
    baseline = engine.world.side_effect_fingerprint()
    steps = 0
    try:
        while engine.cursor.phase != "complete":
            if engine.cursor.phase == "awaiting_change":
                engine.apply_next_change()  # the scenario's reprice is the one allowed world event
                baseline = engine.world.side_effect_fingerprint()
            else:
                engine.step()
                steps += 1
                assert engine.world.side_effect_fingerprint() == baseline
        assert engine.world.inventory == load_inventory()
        assert engine.world.revisions["h17"] == 2 and len(engine.world.events) == 1
    finally:
        store.close()
    assert steps > 10


# ---------------------------------------------------------------------------------------
# Missing required inputs
# ---------------------------------------------------------------------------------------


def missing_party(data):
    data["initial_request"]["text"] = "Plan a three-night trip from RDU to Chicago. Keep round-trip flights and the hotel under $1,800 total."
    data["initial_request"]["requirements"].pop("party")


def test_the_state_agent_asks_when_the_party_is_unknown(service, tmp_path, monkeypatch):
    add_scenario(tmp_path, monkeypatch, "missing-party", missing_party)
    service.create("miss", implementation="state", scenario="missing-party", strategy="reference")
    service.run_to_completion("miss")
    [outcome] = service.records("miss", "outcomes")
    assert outcome["kind"] == "question_asked" and outcome["unresolved_constraint"] == "party"
    assert "party" in outcome["question"]
    assert [t for t in service.records("miss", "tool_calls")] == [] and displayed_plans(service, "miss") == []
    context = service.records("miss", "model_calls")[0]["input"]["context"]
    assert next(s for s in context["sections"] if s["name"] == "unknowns")["content"] == ["party"]


@pytest.mark.parametrize("implementation", ["transcript", "one-shot"])
def test_agents_without_structured_state_also_ask_when_the_party_is_unknown(service, tmp_path, monkeypatch, implementation):
    """The spec asks every implementation to ask rather than guess when a required input is missing."""
    add_scenario(tmp_path, monkeypatch, "missing-party", missing_party)
    service.create("miss", implementation=implementation, scenario="missing-party")
    service.run_to_completion("miss")
    [outcome] = service.records("miss", "outcomes")
    assert outcome["kind"] == "question_asked", outcome
    assert "party" in outcome["unresolved_constraint"].lower(), f"the question must name the unresolved input: {outcome}"
    assert [t for t in service.records("miss", "tool_calls") if t["caller"] == "agent"] == []


# ---------------------------------------------------------------------------------------
# Context handed to the state agent
# ---------------------------------------------------------------------------------------


def test_the_state_agents_input_separates_current_working_context_from_history(runs):
    calls = runs.service.records(runs.state, "model_calls")
    first_after_child = next(c for c in calls if c["turn"] == 1 and c["step"] == 0)
    sections = {s["name"]: s for s in first_after_child["input"]["context"]["sections"]}
    assert sections["current_quotes"]["temporal"] == "current" and sections["historical"]["temporal"] == "historical"
    assert not [q for q in sections["current_quotes"]["content"] if q["quote_id"].endswith("2a0c")]
    stale = sections["historical"]["content"]["inactive_quotes"]
    assert stale and {q["status"] for q in stale} == {"stale"}
    assert [n["item_id"] for n in sections["unsuitable_items"]["content"]] == ["h11"]
    context = AgentContext.model_validate(first_after_child["input"]["context"])
    message = first_after_child["input"]["messages"][0]["content"]
    from workshop.context import render_context

    assert message == render_context(context), "the model saw exactly the rendered context"
    assert sections["requirements"]["content"]["hard_constraints"]["party"] == {"adults": 2, "children": 1}


def test_the_state_agents_transcript_is_retained_even_though_it_is_not_sent(runs):
    transcript = runs.service.transcript(runs.state)
    inputs = [e for e in transcript if e["role"] in ("user", "notification") and "display" in (e["normalized"] or {})]
    assert [(e["turn"], e["role"]) for e in inputs] == [(0, "user"), (1, "user"), (2, "user"), (3, "notification")]
    sent = runs.service.records(runs.state, "model_calls")[-1]["input"]["messages"]
    assert len(sent) == 1, "one rendered context message, not the history"
