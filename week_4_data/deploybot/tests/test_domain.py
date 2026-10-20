"""Domain behaviour: reference solution, baseline failure, independent gate, receipts."""

from __future__ import annotations

from agent_state_workshop import checks, policy, tools
from agent_state_workshop.models import Proposal
from agent_state_workshop.scenarios import full_history
from agent_state_workshop.store import EventStore
from conftest import ok


def test_reference_solution_passes_every_check():
    results = checks.run_checks("reference")
    failing = [r for r in results if r.status != "pass"]
    assert not failing, failing


def test_starter_reports_unfinished_work_without_errors():
    results = checks.run_checks("student")
    statuses = {r.status for r in results}
    # In the starter every check is "todo"; after a student finishes, "pass".
    # Either way an error would mean the runtime, not the student, is broken.
    assert "error" not in statuses, [r for r in results if r.status == "error"]


def test_baseline_proposes_b2_and_gate_blocks(prepared):
    r = ok(prepared.execute("run", {"db": ".workshop/s.sqlite", "strategy": "baseline", "out": "runs/before"}))
    assert (r["proposal"]["action"], r["proposal"]["commit"]) == ("deploy", "b2")
    assert r["gate"]["outcome"] == "blocked"
    assert set(r["gate"]["reason_codes"]) == {"tests_failed", "approval_missing_for_target"}
    assert r["executed_action"] == "none" and r["receipt"] is None


def test_gate_ignores_cited_evidence(tmp_path):
    with EventStore(tmp_path / "g.sqlite") as store:
        store.append_many(full_history()[:40])
        lying = Proposal(action="deploy", commit="b2", environment="staging",
                         evidence_event_ids=["evt-010", "evt-012"], blockers=[], explanation="a1 evidence")
        gate = policy.validate(store, "release-demo", lying, "staging")
    assert gate.outcome == "blocked"
    assert gate.authoritative.test_source_event_id == "evt-032"


def test_gate_rejects_environment_and_commit_substitution(tmp_path):
    with EventStore(tmp_path / "g.sqlite") as store:
        store.append_many(full_history())
        prod = Proposal(action="deploy", commit="b2", environment="production", explanation="x")
        assert "approval_missing_for_target" in policy.validate(store, "release-demo", prod, "production").reason_codes
        old = Proposal(action="deploy", commit="a1", environment="staging", explanation="x")
        assert "commit_not_current_head" in policy.validate(store, "release-demo", old, "staging").reason_codes
        good = Proposal(action="deploy", commit="b2", environment="staging", explanation="x")
        gate = policy.validate(store, "release-demo", good, "staging")
        assert gate.allowed
        first = tools.mock_deploy(store, "release-demo", good, gate, "run-1")
        second = tools.mock_deploy(store, "release-demo", good, gate, "run-2")
    assert not first.reused_existing and second.reused_existing
    assert first.receipt_id == second.receipt_id and second.run_id == "run-1"


def test_reference_follow_up_sequence(prepared):
    db = ".workshop/s.sqlite"
    after = ok(prepared.execute("run", {"db": db, "strategy": "reference"}))
    assert after["proposal"]["action"] == "report_test_failure"
    assert {"tests_failed", "approval_missing_for_target"} <= set(after["proposal"]["blockers"])
    ok(prepared.execute("append", {"db": db, "fixture": "tests-pass-b2"}))
    assert ok(prepared.execute("run", {"db": db, "strategy": "reference"}))["proposal"]["action"] == "request_approval"
    ok(prepared.execute("append", {"db": db, "fixture": "approve-b2-staging"}))
    approved = ok(prepared.execute("run", {"db": db, "strategy": "reference"}))
    assert approved["executed_action"] == "deploy" and approved["gate"]["outcome"] == "allowed"
    prod = ok(prepared.execute("run", {"db": db, "strategy": "reference", "target_environment": "production"}))
    assert (prod["proposal"]["action"], prod["proposal"]["environment"]) == ("request_approval", "production")
    again = ok(prepared.execute("run", {"db": db, "strategy": "reference"}))
    assert again["receipt"]["reused_existing"] and again["receipt"]["receipt_id"] == approved["receipt"]["receipt_id"]
