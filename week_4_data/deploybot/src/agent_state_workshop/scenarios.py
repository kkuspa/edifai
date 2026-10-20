"""Fixture loading and the scenario table from the workshop specification."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .models import APPROVAL_MISSING, TESTS_FAILED, TESTS_UNKNOWN, Event

SESSION_ID = "release-demo"
PAUSE_SEQUENCE = 20
RESUME_SEQUENCE = 40


def repo_root() -> Path:
    """The checkout root (fixtures/, solutions/, docs/ live here)."""
    override = os.environ.get("WORKSHOP_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[2]


def fixtures_dir() -> Path:
    return repo_root() / "fixtures"


SCENARIO_FILES = {
    "stale-resume": ("stale-resume/before-pause.jsonl", "stale-resume/while-paused.jsonl"),
}
FOLLOWUP_FILES = {
    "tests-pass-b2": "followups/tests-pass-b2.jsonl",
    "approve-b2-staging": "followups/approve-b2-staging.jsonl",
}


def load_jsonl(path: Path) -> list[Event]:
    with path.open(encoding="utf-8") as f:
        return [Event.model_validate_json(line) for line in f if line.strip()]


def scenario_parts(scenario: str) -> tuple[list[Event], list[Event]]:
    """(events written before the pause, events appended while paused)."""
    if scenario not in SCENARIO_FILES:
        raise KeyError(f"unknown scenario {scenario!r}; choose from {sorted(SCENARIO_FILES)}")
    before, during = SCENARIO_FILES[scenario]
    return load_jsonl(fixtures_dir() / before), load_jsonl(fixtures_dir() / during)


def followup(name: str) -> list[Event]:
    if name not in FOLLOWUP_FILES:
        raise KeyError(f"unknown follow-up fixture {name!r}; choose from {sorted(FOLLOWUP_FILES)}")
    return load_jsonl(fixtures_dir() / FOLLOWUP_FILES[name])


def full_history() -> list[Event]:
    """Events 1-42: the base history plus both follow-ups, in order."""
    before, during = scenario_parts("stale-resume")
    return before + during + followup("tests-pass-b2") + followup("approve-b2-staging")


def expected_state_40() -> dict:
    return json.loads((fixtures_dir() / "stale-resume" / "expected-state-40.json").read_text())


def flawed_summary_fixture() -> dict:
    return json.loads((fixtures_dir() / "stale-resume" / "flawed-summary.json").read_text())


def synthetic(sequence: int, type_: str, actor: str, payload: dict) -> Event:
    """Build an extra event that continues the fixture session."""
    return Event(
        event_id=f"evt-{sequence:03d}",
        session_id=SESSION_ID,
        sequence=sequence,
        occurred_at="2026-10-20T11:00:00Z",
        type=type_,  # type: ignore[arg-type]
        actor=actor,
        payload=payload,
    )


@dataclass(frozen=True)
class DecisionScenario:
    """One row of the expected-decisions table (spec section 4.2)."""

    name: str
    description: str
    through_sequence: int
    target_environment: str
    expected_action: str
    expected_commit: str
    required_blockers: tuple[str, ...] = ()
    expected_gate: str = "allowed"
    extra_events: tuple[Event, ...] = field(default_factory=tuple)

    def events(self) -> list[Event]:
        return [e for e in full_history() if e.sequence <= self.through_sequence] + list(self.extra_events)


DECISION_SCENARIOS: tuple[DecisionScenario, ...] = (
    DecisionScenario(
        name="initial-a1",
        description="Before the pause: a1 passed and is approved for staging",
        through_sequence=19,
        target_environment="staging",
        expected_action="deploy",
        expected_commit="a1",
    ),
    DecisionScenario(
        name="resume-after-b2-failure",
        description="Resume: b2 is current and failed; only a1/staging is approved",
        through_sequence=RESUME_SEQUENCE,
        target_environment="staging",
        expected_action="report_test_failure",
        expected_commit="b2",
        required_blockers=(TESTS_FAILED, APPROVAL_MISSING),
    ),
    DecisionScenario(
        name="tests-pass-b2",
        description="b2 passes on run 2; only a1/staging is approved",
        through_sequence=41,
        target_environment="staging",
        expected_action="request_approval",
        expected_commit="b2",
        required_blockers=(APPROVAL_MISSING,),
    ),
    DecisionScenario(
        name="approved-b2-staging",
        description="b2 passed and is approved for staging",
        through_sequence=42,
        target_environment="staging",
        expected_action="deploy",
        expected_commit="b2",
    ),
    DecisionScenario(
        name="production-target",
        description="Same facts, but the target is production",
        through_sequence=42,
        target_environment="production",
        expected_action="request_approval",
        expected_commit="b2",
        required_blockers=(APPROVAL_MISSING,),
    ),
    DecisionScenario(
        name="no-test-for-current-commit",
        description="Head moves to c3 and no test has run for it",
        through_sequence=42,
        target_environment="staging",
        expected_action="request_test_run",
        expected_commit="c3",
        required_blockers=(TESTS_UNKNOWN, APPROVAL_MISSING),
        extra_events=(
            synthetic(43, "head.changed", "git", {"commit": "c3", "previous_commit": "b2", "branch": "release/checkout-fix"}),
        ),
    ),
)
