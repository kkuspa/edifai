"""Event store, snapshots, receipts: infrastructure that must pass in the starter."""

from __future__ import annotations

import pytest

from agent_state_workshop import baseline
from agent_state_workshop.models import Event, WorkingState
from agent_state_workshop.scenarios import flawed_summary_fixture, full_history, scenario_parts
from agent_state_workshop.store import EventConflictError, EventStore, SequenceError


def test_append_is_idempotent_and_rejects_conflicts(tmp_path):
    events = full_history()
    with EventStore(tmp_path / "e.sqlite") as store:
        assert all(r.appended for r in store.append_many(events[:5]))
        again = store.append(events[2])
        assert not again.appended
        assert store.last_sequence("release-demo") == 5
        changed = events[2].model_copy(update={"actor": "someone-else"})
        with pytest.raises(EventConflictError):
            store.append(changed)
        with pytest.raises(SequenceError):
            store.append(events[7])  # sequence 8 while 6 is next
        assert [e.event_id for e in store.events("release-demo")] == [e.event_id for e in events[:5]]


def test_fixture_history_is_complete_and_ordered():
    events = full_history()
    assert [e.sequence for e in events] == list(range(1, 43))
    assert len({e.event_id for e in events}) == 42
    decisive = {e.event_id for e in events if e.type in ("head.changed", "test.completed", "approval.granted",
                                                          "session.paused", "session.resume_requested")}
    assert {"evt-008", "evt-010", "evt-012", "evt-020", "evt-021", "evt-032", "evt-040", "evt-041", "evt-042"} <= decisive


def test_snapshot_round_trip_and_hash(tmp_path):
    state = WorkingState.model_validate({"session_id": "release-demo", "through_sequence": 3})
    with EventStore(tmp_path / "e.sqlite") as store:
        row = store.save_snapshot("reference", state)
        loaded = store.load_snapshot("release-demo", "reference")
    assert loaded is not None and loaded.state_hash == row.state_hash == state.state_hash()


def test_baseline_summary_matches_documented_fixture():
    before, _ = scenario_parts("stale-resume")
    assert baseline.summarize_at_pause(before) == flawed_summary_fixture()


def test_event_payload_validation():
    with pytest.raises(ValueError):
        Event(event_id="evt-x", session_id="s", sequence=1, occurred_at="t", type="test.completed",
              actor="ci", payload={"commit": "a1"})
