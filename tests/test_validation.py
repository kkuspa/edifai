"""The inventory tools and the independent validator.

Everything here runs without any student or reference state function: a World, a change log,
and the tools. That is the point. The checker must not depend on unfinished code.
"""

from __future__ import annotations

import ast
import copy
import itertools
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

import workshop.checks
import workshop.validation
from tests.helpers import BUDGET_CENTS, TRIP_DATES, Env
from workshop.models import REASON_CATEGORY, ReasonCode
from workshop.tools import AGENT_TOOLS, TOOL_SPECS, World, load_inventory

# ---------------------------------------------------------------------------------------
# Reference arithmetic (spec section 4.3)
# ---------------------------------------------------------------------------------------


def test_reference_totals_follow_the_spec_table():
    env = Env()
    f2a, h2a = env.flights(2, 0), env.hotels(2, 0)
    v = env.validate(f2a["f1"], h2a["h11"])
    assert v.computed_total_cents == 120_000 and v.compliant, "initial: f1 x 2 + h11 = $1,200"

    env.world.clock.set_offset(10)
    env.add_child()
    f3a, h3a = env.flights(2, 1), env.hotels(2, 1)
    v = env.validate(f3a["f1"], h3a["h17"])
    assert v.computed_total_cents == 162_000 and v.compliant, "child added: f1 x 3 + h17 = $1,620"
    assert "h11" not in h3a, "the two-person room cannot be quoted for three"

    env.world.clock.set_offset(20)
    env.accept_hotel("h17", h3a["h17"])
    env.ask_earlier("18:00", "f1", "plan-002")
    earlier = env.flights(2, 1, arrive_before="18:00")
    assert "f1" not in earlier, "18:00 is not strictly earlier than 18:00"
    v = env.validate(earlier["f2"], h3a["h17"])
    assert v.computed_total_cents == 168_000 and v.compliant and v.preferences_met, "earlier flight: f2 x 3 + h17 = $1,680"

    env.world.clock.set_offset(40)
    env.reprice("h17", 2)
    fresh_h17 = env.refresh("h17", 2, 1)
    v = env.validate(earlier["f2"], fresh_h17)
    assert v.computed_total_cents == 186_000, "refreshed: f2 x 3 + h17 = $1,860"
    assert not v.compliant and not v.hard_constraints_ok
    assert ReasonCode.OVER_BUDGET.value in v.codes()

    v = env.validate(earlier["f3"], fresh_h17)
    assert v.computed_total_cents == 174_000, "alternative: f3 x 3 + refreshed h17 = $1,740"
    assert v.compliant and v.preferences_met
    assert v.confirmation_required, "the accepted hotel now costs $900 instead of $720"


def test_quote_units_and_scope_follow_the_fixture_rules():
    env = Env()
    two, three = env.flights(2, 0), env.flights(2, 1)
    for item, per_traveler in {"f1": 30_000, "f2": 32_000, "f3": 28_000}.items():
        q2, q3 = env.quote(two[item]), env.quote(three[item])
        assert q2.pricing_basis == q3.pricing_basis == "per_traveler"
        assert (q2.unit_amount_cents, q2.total_cents) == (per_traveler, per_traveler * 2)
        assert (q3.unit_amount_cents, q3.total_cents) == (per_traveler, per_traveler * 3), "a child needs a seat and pays the fare"
        assert q2.quote_id != q3.quote_id, "same item, different party: never the same quote"
        assert q2.scope.fingerprint() != q3.scope.fingerprint()
    h2, h3 = env.hotels(2, 0), env.hotels(2, 1)
    assert env.quote(h2["h11"]).total_cents == 60_000 and env.quote(h2["h11"]).pricing_basis == "whole_stay"
    assert env.quote(h2["h17"]).total_cents == 66_000
    assert env.quote(h3["h17"]).total_cents == 72_000, "h17 is $720 for the revised party, not $660"


def test_every_quote_carries_the_fields_the_spec_requires():
    env = Env()
    env.flights(2, 0), env.hotels(2, 1)
    ttl = timedelta(minutes=load_inventory()["quote_ttl_minutes"])
    quotes = list(env.world.quotes.values())
    assert len({q.quote_id for q in quotes}) == len(quotes)
    for q in quotes:
        assert q.item_id and q.inventory_revision == 1 and q.tool_call_id.startswith("tc-")
        assert q.currency == "USD" and isinstance(q.unit_amount_cents, int) and isinstance(q.total_cents, int)
        assert q.expires_at - q.observed_at == ttl
        assert q.observed_at == env.world.clock.now
        if q.item_kind == "flight":
            assert q.arrive_local and q.seats_available is not None
        else:
            assert q.capacity is not None and q.walk_minutes is not None


def test_fixture_clock_ignores_the_computer_clock():
    world = World()
    assert world.clock.now.isoformat() == "2027-03-15T14:00:00+00:00"
    assert World().clock.now == world.clock.now


# ---------------------------------------------------------------------------------------
# What the validator rejects
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flight_party, hotel_party",
    [((2, 0), (2, 1)), ((2, 1), (2, 0)), ((2, 0), (2, 0))],
    ids=["old-flight-new-hotel", "new-flight-old-hotel", "both-old"],
)
def test_two_person_quotes_cannot_validate_a_three_person_plan(flight_party, hotel_party):
    env = Env()
    flights = {p: env.flights(*p) for p in {(2, 0), (2, 1)}}
    hotels = {p: env.hotels(*p) for p in {(2, 0), (2, 1)}}
    env.add_child()
    v = env.validate(flights[flight_party]["f1"], hotels[hotel_party]["h17"])
    assert ReasonCode.QUOTE_SCOPE_MISMATCH.value in v.codes()
    assert not v.quotes_fresh and not v.compliant
    mismatch = next(r for r in v.reasons if r.code == ReasonCode.QUOTE_SCOPE_MISMATCH)
    assert mismatch.category == "quote_validity", "scope is about the evidence, not about a hard constraint"


def test_matching_scope_validates():
    env = Env()
    env.add_child()
    v = env.validate(env.flights(2, 1)["f1"], env.hotels(2, 1)["h17"])
    assert v.compliant and v.codes() == []


def test_the_two_person_room_fails_capacity_for_three():
    env = Env()
    flights, hotels = env.flights(2, 0), env.hotels(2, 0)
    env.add_child()
    v = env.validate(flights["f1"], hotels["h11"])
    assert ReasonCode.INSUFFICIENT_CAPACITY.value in v.codes()
    assert not v.hard_constraints_ok and not v.compliant


def test_a_nearly_full_flight_fails_seats_for_three():
    env = Env()
    two = env.flights(2, 0)
    assert "f5" in two, "two seats remain, enough for two adults"
    hotels = env.hotels(2, 0)
    env.add_child()
    v = env.validate(two["f5"], hotels["h22"])
    assert ReasonCode.INSUFFICIENT_SEATS.value in v.codes() and not v.hard_constraints_ok


def test_wrong_dates_and_sold_out_inventory_never_validate():
    env = Env()
    party = env.requirements["party"]
    from datetime import date

    from workshop.models import Party

    p = Party(**party)
    wrong_day = env.world.issue_quote("flight", env.world.flight("f6"), p, date(2027, 4, 12), date(2027, 4, 15), "tc-x")
    sold_out = env.world.issue_quote("hotel", env.world.hotel("h41"), p, date(2027, 4, 12), date(2027, 4, 15), "tc-y")
    hotels = env.hotels(2, 0)
    flights = env.flights(2, 0)
    v = env.validate(wrong_day.quote_id, hotels["h11"])
    assert ReasonCode.DATE_MISMATCH.value in v.codes() and not v.hard_constraints_ok
    v = env.validate(flights["f1"], sold_out.quote_id)
    assert ReasonCode.ITEM_UNAVAILABLE.value in v.codes() and not v.hard_constraints_ok


def test_unknown_and_swapped_quotes_are_rejected():
    env = Env()
    flights, hotels = env.flights(2, 0), env.hotels(2, 0)
    v = env.validate("quote-made-up", hotels["h11"])
    assert ReasonCode.QUOTE_UNKNOWN.value in v.codes()
    assert v.computed_total_cents is None and not v.compliant
    v = env.validate(hotels["h11"], flights["f1"])
    assert ReasonCode.WRONG_ITEM_KIND.value in v.codes() and not v.compliant


def test_a_wrong_claimed_total_is_a_claim_not_a_price():
    env = Env()
    flights, hotels = env.flights(2, 0), env.hotels(2, 0)
    right = env.validate(flights["f1"], hotels["h11"], 120_000)
    assert right.codes() == [] and right.claimed_total_cents == 120_000
    wrong = env.validate(flights["f1"], hotels["h11"], 100)
    assert wrong.computed_total_cents == 120_000, "the computed total is authoritative"
    claim = next(r for r in wrong.reasons if r.code == ReasonCode.CLAIMED_TOTAL_MISMATCH)
    assert claim.category == "claim"


def test_validation_records_when_and_against_which_requirements():
    env = Env()
    flights, hotels = env.flights(2, 0), env.hotels(2, 0)
    assert env.validate(flights["f1"], hotels["h11"]).requirement_revision == 1
    env.world.clock.set_offset(30)
    v = env.validate(flights["f1"], hotels["h11"])
    assert v.checked_at == env.world.clock.now
    env.add_child()
    assert env.validate(flights["f1"], hotels["h11"]).requirement_revision == 2
    env.ask_earlier()
    assert env.validate(flights["f1"], hotels["h11"]).requirement_revision == 3


# ---------------------------------------------------------------------------------------
# Hard constraints, preferences, quote freshness, and confirmation stay separate
# ---------------------------------------------------------------------------------------


def three_person_env() -> tuple[Env, dict[str, str], dict[str, str]]:
    env = Env()
    env.flights(2, 0), env.hotels(2, 0)
    env.world.clock.set_offset(10)
    env.add_child()
    return env, env.flights(2, 1), env.hotels(2, 1)


def test_every_reason_code_has_exactly_one_category():
    assert set(REASON_CATEGORY) == set(ReasonCode)
    assert REASON_CATEGORY[ReasonCode.OVER_BUDGET] == "hard_constraint"
    assert REASON_CATEGORY[ReasonCode.INSUFFICIENT_CAPACITY] == "hard_constraint"
    assert REASON_CATEGORY[ReasonCode.QUOTE_EXPIRED] == "quote_validity"
    assert REASON_CATEGORY[ReasonCode.QUOTE_SCOPE_MISMATCH] == "quote_validity"
    assert REASON_CATEGORY[ReasonCode.ARRIVAL_PREFERENCE_UNMET] == "preference"
    assert REASON_CATEGORY[ReasonCode.WALK_PREFERENCE_UNMET] == "preference"
    assert REASON_CATEGORY[ReasonCode.CONFIRMATION_REQUIRED] == "confirmation"


def test_an_unmet_preference_is_not_a_hard_violation():
    env, flights, hotels = three_person_env()
    env.accept_hotel("h17", hotels["h17"])
    env.ask_earlier("18:00")
    v = env.validate(flights["f1"], hotels["h17"])  # arrives 18:00, not before
    assert v.codes() == [ReasonCode.ARRIVAL_PREFERENCE_UNMET.value]
    assert v.hard_constraints_ok and v.quotes_fresh and v.compliant
    assert not v.preferences_met and not v.confirmation_required


def test_the_walking_preference_is_a_preference_not_a_hard_constraint():
    env, flights, hotels = three_person_env()
    v = env.validate(flights["f3"], hotels["h22"])  # 25-minute walk against a 15-minute preference
    assert v.codes() == [ReasonCode.WALK_PREFERENCE_UNMET.value]
    assert v.compliant and not v.preferences_met


def test_confirmation_is_neither_a_hard_violation_nor_a_stale_quote():
    env, flights, hotels = three_person_env()
    env.accept_hotel("h17", hotels["h17"])
    env.reprice("h17", 2)
    fresh = env.refresh("h17", 2, 1)
    v = env.validate(flights["f3"], fresh)
    assert v.codes() == [ReasonCode.CONFIRMATION_REQUIRED.value]
    assert v.compliant and v.confirmation_required and v.preferences_met


def test_replacing_the_accepted_hotel_asks_for_confirmation():
    env, flights, hotels = three_person_env()
    env.accept_hotel("h17", hotels["h17"])
    v = env.validate(flights["f3"], hotels["h22"])
    assert ReasonCode.CONFIRMATION_REQUIRED.value in v.codes() and v.compliant


def test_the_same_hotel_at_the_same_price_needs_no_confirmation():
    env, flights, hotels = three_person_env()
    env.accept_hotel("h17", hotels["h17"])
    v = env.validate(flights["f1"], hotels["h17"])
    assert not v.confirmation_required and v.codes() == []


def test_freshness_is_a_separate_question_from_constraints():
    env, flights, hotels = three_person_env()
    env.world.clock.set_offset(10 + 130)
    v = env.validate(flights["f1"], hotels["h17"])
    assert v.hard_constraints_ok, "the combination is affordable and fits the party"
    assert not v.quotes_fresh and not v.compliant, "but the evidence is stale"
    assert v.codes().count(ReasonCode.QUOTE_EXPIRED.value) == 2


# ---------------------------------------------------------------------------------------
# Budget and alternatives
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flight, hotel, total, compliant, expect_codes",
    [
        ("f3", "h17", 174_000, True, {"confirmation_required"}),
        ("f1", "h17", 180_000, True, {"confirmation_required", "arrival_preference_unmet"}),  # exactly at budget
        ("f2", "h17", 186_000, False, {"confirmation_required", "over_budget"}),
        ("f3", "h22", 134_000, True, {"confirmation_required", "walk_preference_unmet"}),  # a different hotel, disclosed
        ("f4", "h22", 212_000, False, {"confirmation_required", "walk_preference_unmet", "over_budget"}),
        ("f3", "h30", 199_000, False, {"confirmation_required", "over_budget"}),
    ],
)
def test_alternatives_after_the_hotel_reprice(flight, hotel, total, compliant, expect_codes):
    env, flights, hotels = three_person_env()
    env.accept_hotel("h17", hotels["h17"])
    env.ask_earlier("18:00")
    env.world.clock.set_offset(40)
    env.reprice("h17", 2)
    hotels = {**hotels, "h17": env.refresh("h17", 2, 1)}
    v = env.validate(flights[flight], hotels[hotel])
    assert v.computed_total_cents == total
    assert v.compliant is compliant
    assert set(v.codes()) == expect_codes
    assert (ReasonCode.OVER_BUDGET.value in v.codes()) == (total > BUDGET_CENTS)


def test_exactly_at_budget_is_compliant_with_the_preference_disclosed():
    """f1 x 3 + refreshed h17 = $1,800: allowed by the budget, but 18:00 is not earlier than 18:00."""
    env, flights, hotels = three_person_env()
    env.accept_hotel("h17", hotels["h17"])
    env.ask_earlier("18:00")
    env.reprice("h17", 2)
    v = env.validate(flights["f1"], env.refresh("h17", 2, 1))
    assert v.computed_total_cents == 180_000
    assert v.compliant and v.hard_constraints_ok
    unmet = [r for r in v.reasons if r.code == ReasonCode.ARRIVAL_PREFERENCE_UNMET]
    assert unmet and unmet[0].category == "preference"
    assert not any(REASON_CATEGORY[ReasonCode(c)] == "hard_constraint" for c in v.codes())


def test_no_over_budget_combination_is_ever_compliant():
    env, flights, hotels = three_person_env()
    checked = 0
    for (fid, fq), (hid, hq) in itertools.product(flights.items(), hotels.items()):
        v = env.validate(fq, hq)
        checked += 1
        assert v.computed_total_cents is not None
        assert v.compliant == (v.computed_total_cents <= BUDGET_CENTS), (fid, hid, v.codes())
        if v.computed_total_cents > BUDGET_CENTS:
            assert ReasonCode.OVER_BUDGET.value in v.codes() and not v.hard_constraints_ok
    assert checked >= 12


# ---------------------------------------------------------------------------------------
# Stale quotes
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("minutes, expired", [(0, False), (119, False), (120, True), (121, True)])
def test_a_quote_lives_for_exactly_the_ttl(minutes, expired):
    env = Env()
    flights, hotels = env.flights(2, 0), env.hotels(2, 0)
    env.world.clock.set_offset(minutes)
    v = env.validate(flights["f1"], hotels["h11"])
    assert (ReasonCode.QUOTE_EXPIRED.value in v.codes()) is expired
    assert v.quotes_fresh is not expired and v.compliant is not expired


def test_a_stale_quote_cannot_support_a_compliant_plan_until_it_is_refreshed():
    env = Env()
    flights, hotels = env.flights(2, 0), env.hotels(2, 0)
    env.world.clock.advance(121)
    assert not env.validate(flights["f1"], hotels["h11"]).compliant
    new_flight, new_hotel = env.refresh("f1", 2, 0), env.refresh("h11", 2, 0)
    assert new_flight != flights["f1"] and new_hotel != hotels["h11"], "an expired quote is replaced, not revived"
    assert env.validate(new_flight, new_hotel).compliant
    assert not env.validate(flights["f1"], new_hotel).compliant, "the old flight quote stays expired"


def test_a_reprice_expires_old_quotes_before_their_ttl():
    env, flights, hotels = three_person_env()
    expired = env.reprice("h17", 2)
    assert hotels["h17"] in expired
    v = env.validate(flights["f1"], hotels["h17"])
    assert ReasonCode.QUOTE_EXPIRED.value in v.codes() and not v.compliant
    assert env.world.clock.now < env.quote(hotels["h17"]).expires_at, "the TTL had not elapsed; the reprice did it"
    assert env.validate(flights["f1"], env.refresh("h17", 2, 1)).quotes_fresh


def test_a_refreshed_quote_has_the_new_price_and_revision():
    env, _, hotels = three_person_env()
    old = env.quote(hotels["h17"])
    env.reprice("h17", 2)
    new = env.quote(env.refresh("h17", 2, 1))
    assert (old.inventory_revision, old.total_cents) == (1, 72_000)
    assert (new.inventory_revision, new.total_cents) == (2, 90_000)
    assert new.quote_id != old.quote_id


# ---------------------------------------------------------------------------------------
# The validator is independent of student code
# ---------------------------------------------------------------------------------------


def test_validation_module_imports_only_models_and_tools():
    tree = ast.parse(Path(workshop.validation.__file__).read_text())
    package_imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            package_imports.add("." * node.level + (node.module or ""))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            assert not any(n.split(".")[0] in ("solutions", "workshop") for n in names), names
    assert package_imports <= {".models", ".tools"}, package_imports


def test_importing_the_validator_loads_no_student_or_state_code(subprocess_env):
    code = (
        "import sys\n"
        "import workshop.validation\n"
        "banned = ['workshop.exercises', 'solutions', 'solutions.reference', 'workshop.state', 'workshop.context',\n"
        "          'workshop.runner', 'workshop.store', 'workshop.service', 'workshop.checks', 'workshop.planners']\n"
        "print(sorted(m for m in sys.modules if m in banned))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=subprocess_env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_validation_and_scenario_checks_never_call_student_functions(runs, monkeypatch):
    import solutions.reference
    import workshop.exercises

    def boom(*args, **kwargs):
        raise AssertionError("the checker called a student function")

    for module in (workshop.exercises, solutions.reference):
        monkeypatch.setattr(module, "apply_change", boom)
        monkeypatch.setattr(module, "build_context", boom)

    env, flights, hotels = three_person_env()
    assert env.validate(flights["f1"], hotels["h17"]).compliant
    for name in (runs.state, runs.transcript, runs.state_nc):
        results = workshop.checks.scenario_checks(runs.service.path(name))
        assert results and all(c["passed"] is not False for c in results), [c for c in results if c["passed"] is False]


# ---------------------------------------------------------------------------------------
# Tools: filters, scoping, and no side effects
# ---------------------------------------------------------------------------------------


def test_flight_search_filters_dates_seats_and_arrival():
    env = Env()
    out = env.tool("search_flights", origin="RDU", destination="CHI", adults=2, children=1, arrive_before="18:00", **TRIP_DATES)
    ids = {q["item_id"] for q in out["quotes"]}
    assert ids == {"f2", "f3", "f4"}
    reasons = {e["item_id"]: e["reason"] for e in out["excluded"]}
    assert "not before 18:00" in reasons["f1"], "arrival must be strictly earlier than the reference"
    assert "seats" in reasons["f5"] and "do not match" in reasons["f6"]
    assert out["scope"] == {"adults": 2, "children": 1, **TRIP_DATES}

    everything = env.tool("search_flights", origin="RDU", destination="CHI", adults=2, children=0, arrive_before=None, **TRIP_DATES)
    assert {q["item_id"] for q in everything["quotes"]} == {"f1", "f2", "f3", "f4", "f5"}


def test_hotel_search_reports_why_a_room_is_ineligible():
    env = Env()
    out = env.tool("search_hotels", check_in=TRIP_DATES["depart_date"], check_out=TRIP_DATES["return_date"], adults=2, children=1)
    assert {q["item_id"] for q in out["quotes"]} == {"h17", "h22", "h30"}
    reasons = {e["item_id"]: e["reason"] for e in out["ineligible"]}
    assert "capacity 2 < occupancy 3" in reasons["h11"]
    assert "sold out" in reasons["h41"]
    wrong_dates = env.tool("search_hotels", check_in="2027-05-01", check_out="2027-05-04", adults=2, children=0)
    assert wrong_dates["quotes"] == []


def test_refresh_reports_unavailable_instead_of_inventing_a_quote():
    env = Env()
    assert env.tool("refresh_quote", item_id="zz9", adults=2, children=0, **TRIP_DATES)["available"] is False
    small = env.tool("refresh_quote", item_id="h11", adults=2, children=1, **TRIP_DATES)
    assert small["available"] is False and "capacity" in small["reason"] and small["quotes"] == []
    full = env.tool("refresh_quote", item_id="f5", adults=2, children=1, **TRIP_DATES)
    assert full["available"] is False and "seats" in full["reason"]
    assert env.tool("refresh_quote", item_id="h41", adults=2, children=0, **TRIP_DATES)["available"] is False


def test_malformed_arguments_return_errors_instead_of_raising():
    env = Env()
    bad_date = env.tool("search_hotels", check_in="tomorrow", check_out="2027-04-15", adults=2, children=0)
    assert bad_date["ok"] is False and "ISO date" in bad_date["error"]
    missing = env.tool("search_flights", origin="RDU")
    assert missing["ok"] is False
    assert env.toolbox.execute("book_hotel", {}, "tc-1")["ok"] is False


def test_read_only_tools_leave_the_world_untouched():
    env = Env()
    pristine = copy.deepcopy(env.world.inventory)
    fingerprint = env.world.side_effect_fingerprint()
    flights, hotels = env.flights(2, 0), env.hotels(2, 0)
    env.refresh("h11", 2, 0)
    env.tool("refresh_quote", item_id="zz9", adults=2, children=0, **TRIP_DATES)
    env.tool("search_hotels", check_in="bad", check_out="worse", adults=2, children=0)
    env.tool("validate_plan", flight_quote_id=flights["f1"], hotel_quote_id=hotels["h11"], claimed_total_cents=120_000)
    env.tool("read_history", role="any")
    env.validate(flights["f1"], hotels["h11"])
    assert env.world.side_effect_fingerprint() == fingerprint
    assert env.world.inventory == pristine
    assert env.world.events == [] and set(env.world.revisions.values()) == {1}


def test_no_tool_can_book_pay_or_reserve():
    forbidden = ("book", "pay", "reserve", "charge", "purchase", "buy", "checkout")
    assert not [n for n in TOOL_SPECS if any(w in n for w in forbidden)]
    assert set(AGENT_TOOLS) == {"search_flights", "search_hotels", "refresh_quote", "validate_plan", "read_history", "present_plan", "ask_user"}
    env = Env()
    for terminal in ("present_plan", "ask_user"):
        assert env.toolbox.execute(terminal, {}, "tc-1")["ok"] is False, "terminal actions are handled by the runner, not executed as tools"


def test_every_tool_spec_is_a_strict_json_schema():
    for spec in TOOL_SPECS.values():
        params = spec.parameters
        assert params["type"] == "object" and params["additionalProperties"] is False
        assert set(params["required"]) <= set(params["properties"])
        assert spec.description.strip()
