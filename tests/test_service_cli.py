"""The shared services and the command-line interface.

The CLI is the complete workflow: everything here runs with no GUI dependency. The same
services back the GUI, so the operation rules (IDs, versions, locks, rollback) are tested here
once and the GUI tests only need to show that the HTTP layer passes them through.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

import pytest

from tests.helpers import (
    REFERENCE_IMPORTS,
    artifact_mtimes,
    db_fingerprint,
    displayed_plans,
    plan_summary,
    read_jsonl,
    record_counts,
    run_session,
    student_strategy,
)
from workshop.cli import main
from workshop.runner import load_strategy
from workshop.service import InvalidOperation, SessionExists, SessionNotFound, StudentCodeError, VersionConflict, WorkshopService

PATH = [("plan-001", 120_000), ("plan-002", 162_000), ("plan-003", 168_000), ("plan-004", 174_000)]


def cli(capsys, root, *argv: str) -> tuple[int, str, str]:
    code = main(["--root", str(root), *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


# ---------------------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------------------


def test_cli_run_reproduces_the_reference_path_and_writes_every_artifact(capsys, tmp_path):
    root = tmp_path / "runs"
    code, out, err = cli(capsys, root, "run", "--implementation", "state", "--strategy", "reference", "--out", "state")
    assert code == 0 and err == ""
    assert "plan-001 flight f1 + hotel h11, total $1,200" in out and "plan-004 flight f3 + hotel h17, total $1,740" in out
    assert "confirmation=reconfirmation_required" in out and "Scenario complete." in out
    files = {p.name for p in (root / "state").iterdir()}
    assert {"manifest.json", "changes.jsonl", "state_snapshots.jsonl", "transcript.json", "model_calls.jsonl", "tool_calls.jsonl",
            "plans.jsonl", "checks.json", "report.html", "session.sqlite"} <= files
    plans = [(p["proposal"]["proposal_id"], p["proposal"]["computed_total_cents"]) for p in read_jsonl(root / "state" / "plans.jsonl") if p["displayed"]]
    assert plans == PATH


@pytest.mark.parametrize("implementation, scenario", [("one-shot", "initial"), ("transcript", "changing-trip"), ("state", "no-compliant")])
def test_cli_run_supports_every_implementation_and_scenario(capsys, tmp_path, implementation, scenario):
    code, out, err = cli(capsys, tmp_path / "r", "run", "--implementation", implementation, "--strategy", "reference", "--scenario", scenario, "--out", "x")
    assert code == 0 and err == "" and f"implementation={implementation}" in out and f"scenario={scenario}" in out
    assert "planner=simulated" in out


def test_cli_run_refuses_to_overwrite_a_session_unless_asked(capsys, tmp_path):
    root = tmp_path / "runs"
    assert cli(capsys, root, "run", "--implementation", "transcript", "--out", "keep")[0] == 0
    before = db_fingerprint(root / "keep")
    code, out, err = cli(capsys, root, "run", "--implementation", "transcript", "--out", "keep")
    assert code == 2 and "already holds a session" in err and "--overwrite" in err
    assert db_fingerprint(root / "keep") == before, "the refused run changed nothing"
    first_run = json.loads((root / "keep" / "manifest.json").read_text())["run_id"]
    assert cli(capsys, root, "run", "--implementation", "transcript", "--out", "keep", "--overwrite")[0] == 0
    assert json.loads((root / "keep" / "manifest.json").read_text())["run_id"] != first_run


def test_cli_run_refuses_a_directory_that_holds_something_else(capsys, tmp_path):
    root = tmp_path / "runs"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "keep-me.txt").write_text("a student's notes")
    code, _, err = cli(capsys, root, "run", "--implementation", "transcript", "--out", "notes")
    assert code == 2 and "not empty" in err
    assert (root / "notes" / "keep-me.txt").read_text() == "a student's notes" and not (root / "notes" / "session.sqlite").exists()


def test_cli_run_accepts_an_empty_existing_directory(capsys, tmp_path):
    (tmp_path / "runs" / "empty").mkdir(parents=True)
    assert cli(capsys, tmp_path / "runs", "run", "--implementation", "transcript", "--out", "empty")[0] == 0


def test_cli_reports_bad_names_and_missing_credentials_without_a_traceback(capsys, tmp_path):
    root = tmp_path / "runs"
    code, _, err = cli(capsys, root, "run", "--implementation", "state", "--scenario", "no-such-scenario", "--out", "x")
    assert code == 2 and "Unknown scenario 'no-such-scenario'" in err and "changing-trip" in err
    code, _, err = cli(capsys, root, "run", "--implementation", "state", "--planner", "live", "--out", "y")
    assert code == 2 and "No live provider configured" in err
    assert not (root / "y" / "session.sqlite").exists(), "no half-built session was left behind"
    code, _, err = cli(capsys, root, "session", "status", "ghost")
    assert code == 2 and "No session at" in err
    assert cli(capsys, root, "run", "--implementation", "state", "--strategy", "reference", "--out", "y")[0] == 0, "and the name is still usable"


def test_cli_step_flag_without_a_terminal_still_completes(capsys, tmp_path):
    code, out, _ = cli(capsys, tmp_path / "runs", "run", "--implementation", "state", "--strategy", "reference", "--out", "s", "--step")
    assert code == 0 and "needs an interactive terminal" in out and "Scenario complete." in out


# ---------------------------------------------------------------------------------------
# session workflow
# ---------------------------------------------------------------------------------------


def test_cli_session_workflow_one_action_at_a_time(capsys, tmp_path):
    root = tmp_path / "runs"
    code, out, _ = cli(capsys, root, "session", "start", "w", "--implementation", "state", "--strategy", "reference")
    assert code == 0 and "version=1" in out and "phase=ready" in out and "workshop session step w" in out
    code, out, err = cli(capsys, root, "session", "apply-change", "w")
    assert code == 2 and "Cannot apply a change while the session is 'ready'" in err

    code, out, _ = cli(capsys, root, "session", "step", "w", "--count", "2")
    assert code == 0 and out.count("tool ") == 2 and "version=" in out
    code, out, _ = cli(capsys, root, "session", "run-turn", "w")
    assert code == 0 and "plan presented plan-001 total $1,200" in out
    status = json.loads(cli(capsys, root, "session", "status", "w", "--json")[1])
    assert status["cursor"]["phase"] == "awaiting_change" and status["can_apply_change"] and status["next_change"]["turn"] == 1

    code, out, _ = cli(capsys, root, "session", "apply-change", "w")
    assert code == 0 and "applied turn 1: chg-t1-0" in out
    assert "workshop session step w" in cli(capsys, root, "session", "status", "w")[1]
    code, out, _ = cli(capsys, root, "session", "run", "w")
    assert code == 0 and "plan presented plan-004 total $1,740 (reconfirmation_required)" in out
    assert "Scenario complete." in cli(capsys, root, "session", "status", "w")[1]
    listing = cli(capsys, root, "session", "list")[1]
    assert "w " in listing and "state" in listing and "phase=complete" in listing


def test_cli_show_views_cover_state_diffs_inputs_tools_proposals_and_validation(capsys, tmp_path):
    root = tmp_path / "runs"
    cli(capsys, root, "run", "--implementation", "state", "--strategy", "reference", "--out", "v")
    state = json.loads(cli(capsys, root, "session", "show", "v", "state")[1])
    assert state["available"] and state["state"]["decisions"]["hotel"]["hotel_id"] == "h17" and len(state["snapshots"]) > 10
    snapshots = {s["change_id"]: s["index"] for s in state["snapshots"]}
    earlier = json.loads(cli(capsys, root, "session", "show", "v", "state", "--index", str(snapshots["chg-t1-0"]), "--json")[1])
    assert earlier["state"]["requirements"]["party"] == {"adults": 2, "children": 1} and earlier["state"]["decisions"]["hotel"] is None

    out = cli(capsys, root, "session", "show", "v", "diff", "--index", str(snapshots["chg-t1-0"]))[1]
    assert "requirements.party.children: 0 -> 1" in out
    assert "requirements.origin" not in out and "requirements.budget" not in out, "preserved fields do not appear in the diff"

    model_input = cli(capsys, root, "session", "show", "v", "model-input", "--index", "1")[1]
    assert "planner_mode=simulated" in model_input and "--- system ---" in model_input and "--- returned action ---" in model_input
    assert "usage --- not reported" in model_input
    raw = json.loads(cli(capsys, root, "session", "show", "v", "model-input", "--json")[1])
    assert raw["input"]["context"] and raw["usage"] is None

    tools = cli(capsys, root, "session", "show", "v", "tools")[1]
    assert "search_hotels(" in tools and "validate_plan(" in tools
    proposals = json.loads(cli(capsys, root, "session", "show", "v", "proposals")[1])
    assert [p["displayed"] for p in proposals].count(True) == 4
    validations = json.loads(cli(capsys, root, "session", "show", "v", "validation")[1])
    assert any(v["source"] == "runner_gate" for v in validations) and any(v["source"] == "agent_tool" for v in validations)
    assert len(json.loads(cli(capsys, root, "session", "show", "v", "transcript")[1])) > 20
    assert len(json.loads(cli(capsys, root, "session", "show", "v", "changes")[1])) > 10


def test_cli_inspection_of_a_transcript_session_says_there_is_no_state(capsys, tmp_path):
    root = tmp_path / "runs"
    cli(capsys, root, "run", "--implementation", "transcript", "--out", "t")
    assert json.loads(cli(capsys, root, "session", "show", "t", "state")[1])["available"] is False
    assert "Task state: none" in cli(capsys, root, "session", "status", "t")[1]
    assert cli(capsys, root, "session", "show", "t", "diff", "--json")[0] == 0


def test_cli_operation_ids_and_expected_versions(capsys, tmp_path):
    root = tmp_path / "runs"
    cli(capsys, root, "session", "start", "o", "--implementation", "state", "--strategy", "reference")
    first = json.loads(cli(capsys, root, "session", "step", "o", "--op-id", "op-1", "--expected-version", "1", "--json")[1])
    assert first["replayed"] is False and first["version"] == 2
    again = json.loads(cli(capsys, root, "session", "step", "o", "--op-id", "op-1", "--expected-version", "1", "--json")[1])
    assert again["replayed"] is True and again["version"] == 2 and again["detail"] == first["detail"]
    assert "(replayed, nothing executed)" in cli(capsys, root, "session", "step", "o", "--op-id", "op-1")[1]
    status = json.loads(cli(capsys, root, "session", "status", "o", "--json")[1])
    assert status["cursor"]["version"] == 2 and status["counts"]["model_calls"] == 1, "three requests, one execution"
    code, _, err = cli(capsys, root, "session", "step", "o", "--op-id", "op-2", "--expected-version", "1")
    assert code == 2 and "Session is at version 2, request expected 1" in err
    assert json.loads(cli(capsys, root, "session", "status", "o", "--json")[1])["counts"]["model_calls"] == 1


def test_cli_reset_restarts_with_the_same_settings(capsys, tmp_path):
    root = tmp_path / "runs"
    cli(capsys, root, "run", "--implementation", "state", "--strategy", "reference", "--scenario", "no-compliant", "--out", "r")
    before = json.loads(cli(capsys, root, "session", "status", "r", "--json")[1])
    assert before["cursor"]["phase"] == "complete"
    result = json.loads(cli(capsys, root, "session", "reset", "r", "--op-id", "reset-1", "--json")[1])
    assert result["kind"] == "reset" and result["version"] == 1 and result["phase"] == "ready" and result["replayed"] is False
    after = json.loads(cli(capsys, root, "session", "status", "r", "--json")[1])
    assert after["counts"] == {"model_calls": 0, "tool_calls": 0, "snapshots": 1, "changes": 0}
    for key in ("implementation", "scenario", "planner", "strategy"):
        assert after["config"][key] == before["config"][key], key
    assert after["config"]["run_id"] != before["config"]["run_id"]
    assert after["clock"] == "2027-03-15T14:00:00+00:00" and after["displayed_proposal"] is None
    cli(capsys, root, "session", "run", "r")
    assert json.loads(cli(capsys, root, "session", "reset", "r", "--op-id", "reset-1", "--json")[1])["replayed"] is True, "a repeated reset is not a second reset"
    assert json.loads(cli(capsys, root, "session", "status", "r", "--json")[1])["cursor"]["phase"] == "complete"


def test_cli_export_rewrites_the_artifacts(capsys, tmp_path):
    root = tmp_path / "runs"
    cli(capsys, root, "run", "--implementation", "transcript", "--out", "e")
    (root / "e" / "report.html").unlink()
    (root / "e" / "checks.json").write_text("[]")
    code, out, _ = cli(capsys, root, "session", "export", "e")
    assert code == 0 and "report.html" in out
    assert (root / "e" / "report.html").exists() and json.loads((root / "e" / "checks.json").read_text())


def test_cli_check_and_compare_write_where_they_are_told(capsys, tmp_path):
    root = tmp_path / "runs"
    cli(capsys, root, "run", "--implementation", "transcript", "--out", "a")
    cli(capsys, root, "run", "--implementation", "state", "--strategy", "reference", "--out", "b")
    out_file = tmp_path / "out" / "cmp.html"
    code, out, _ = cli(capsys, root, "compare", "--runs", "a", "b", "--output", str(out_file))
    assert code == 0 and str(out_file) in out and "Run comparison" in out_file.read_text()
    check_file = tmp_path / "out" / "check.json"
    code, out, _ = cli(capsys, root, "check", "--strategy", "reference", "--output", str(check_file))
    assert code == 0 and "18 pass, 0 not yet" in out and "Every check passes" in out
    assert json.loads(check_file.read_text())["overall"] == "pass"
    code, out, _ = cli(capsys, root, "check", "--strategy", "reference", "--output", str(check_file), "--json")
    assert json.loads(out.rsplit("Saved", 1)[0])["strategy"] == "reference"
    code, _, err = cli(capsys, root, "compare", "--runs", "a", "missing", "--output", str(out_file))
    assert code == 2 and "No session at" in err


# ---------------------------------------------------------------------------------------
# Operation rules at the service layer
# ---------------------------------------------------------------------------------------


def test_a_repeated_operation_id_returns_the_stored_result_without_running_again(service):
    service.create("s", implementation="state", strategy="reference")
    first = service.run_turn("s", op_id="op-a")
    counts = record_counts(service, "s")
    for _ in range(3):
        again = service.run_turn("s", op_id="op-a")
        assert again["replayed"] is True and again["detail"] == first["detail"] and again["version"] == first["version"]
    assert record_counts(service, "s") == counts
    assert service.status("s")["cursor"]["version"] == 2


def test_a_replay_survives_later_operations_and_a_stale_expected_version(service):
    service.create("s", implementation="state", strategy="reference")
    first = service.run_turn("s", op_id="op-a", expected_version=1)
    service.apply_change("s", op_id="op-b", expected_version=2)
    replay = service.run_turn("s", op_id="op-a", expected_version=1)
    assert replay["replayed"] is True and replay["detail"] == first["detail"], "a lost response can be retried safely"
    assert service.status("s")["cursor"]["version"] == 3


def test_a_stale_expected_version_is_refused_and_changes_nothing(service):
    service.create("s", implementation="transcript")
    service.step("s")
    before = (record_counts(service, "s"), db_fingerprint(service.path("s")), artifact_mtimes(service.path("s")))
    with pytest.raises(VersionConflict) as exc:
        service.step("s", expected_version=1)
    assert exc.value.status == 409 and "version 2" in str(exc.value)
    assert (record_counts(service, "s"), db_fingerprint(service.path("s")), artifact_mtimes(service.path("s"))) == before
    assert service.step("s", expected_version=2)["version"] == 3


def test_invalid_operations_do_not_bump_the_version_or_store_the_operation_id(service):
    service.create("s", implementation="transcript")
    for _ in range(2):
        with pytest.raises(InvalidOperation) as exc:
            service.apply_change("s", op_id="op-early")
        assert exc.value.status == 409
    assert service.status("s")["cursor"]["version"] == 1
    service.run_turn("s")
    assert service.apply_change("s", op_id="op-early")["replayed"] is False, "the failed attempts left no stored result behind"


def test_create_is_idempotent_by_operation_id_and_refuses_a_second_session_under_the_name(service):
    first = service.create("s", implementation="transcript", op_id="op-create")
    assert first["version"] == 1 and first["replayed"] is False
    assert service.create("s", implementation="transcript", op_id="op-create")["replayed"] is True
    with pytest.raises(SessionExists):
        service.create("s", implementation="state", op_id="op-other")
    assert service.status("s")["config"]["implementation"] == "transcript"
    assert service.create("s", implementation="state", strategy="reference", op_id="op-third", overwrite=True)["replayed"] is False
    assert service.status("s")["config"]["implementation"] == "state"


def test_reset_is_explicit_and_leaves_other_sessions_alone(service):
    run_session(service, "a", "state")
    run_session(service, "b", "state")
    other = db_fingerprint(service.path("b"))
    result = service.reset("a")
    assert result["kind"] == "reset" and result["phase"] == "ready"
    assert db_fingerprint(service.path("b")) == other
    with pytest.raises(SessionNotFound):
        service.reset("never-existed")


def test_reads_never_take_the_lock_or_change_anything(service):
    run_session(service, "r", "state")
    path = service.path("r")
    before = (db_fingerprint(path), artifact_mtimes(path), record_counts(service, "r"))
    for _ in range(3):
        service.status("r"), service.state("r"), service.diff("r"), service.summary("r"), service.list_sessions(), service.options()
        for kind in ("model_calls", "tool_calls", "plans", "validations", "outcomes", "notes"):
            service.records("r", kind)
        service.transcript("r"), service.changes("r"), service.compare_data(["r"])
    assert (db_fingerprint(path), artifact_mtimes(path), record_counts(service, "r")) == before


def test_unknown_record_kinds_and_snapshots_are_refused(service):
    from workshop.service import ServiceError

    run_session(service, "r", "state")
    with pytest.raises(ServiceError, match="Unknown record kind"):
        service.records("r", "world")
    with pytest.raises(SessionNotFound):
        service.state("r", snapshot=9999)
    with pytest.raises(SessionNotFound):
        service.diff("r", snapshot=9999)


def test_status_describes_what_a_page_needs_to_enable_its_buttons(service):
    service.create("s", implementation="state", strategy="reference")
    st = service.status("s")
    assert (st["can_step"], st["can_apply_change"]) == (True, False) and st["cursor"]["phase"] == "ready"
    assert [t["applied"] for t in st["scenario"]["turns"]] == [False, False, False] and not any(t["next"] for t in st["scenario"]["turns"])
    service.run_turn("s")
    st = service.status("s")
    assert (st["can_step"], st["can_apply_change"]) == (False, True) and st["next_change"]["turn"] == 1
    assert [t["next"] for t in st["scenario"]["turns"]] == [True, False, False]
    assert st["displayed_proposal"]["proposal_id"] == "plan-001" and st["state_summary"]["party"] == "2 adults"
    assert [c["speaker"] for c in st["conversation"]] == ["user", "assistant"]
    assert st["scenario"]["turns"][1]["change_types"] == ["HotelAccepted", "EarlierFlightRequested"]
    service.run_to_completion("s")
    st = service.status("s")
    assert (st["can_step"], st["can_apply_change"]) == (False, False) and st["cursor"]["phase"] == "complete" and st["next_change"] is None


# ---------------------------------------------------------------------------------------
# Student code that raises
# ---------------------------------------------------------------------------------------

BROKEN_APPLY = REFERENCE_IMPORTS + '''
def apply_change(state, change):
    if change.event.type == "PartySizeChanged":
        raise ZeroDivisionError("student bug")
    return _apply(state, change)

def build_context(state, step):
    return _context(state, step)
'''
BROKEN_CONTEXT = REFERENCE_IMPORTS + '''
def apply_change(state, change):
    return _apply(state, change)

def build_context(state, step):
    return {}["no such section"]
'''


@pytest.mark.parametrize("source, operation", [(BROKEN_APPLY, "apply_change"), (BROKEN_CONTEXT, "run_turn")])
def test_an_exception_in_student_code_rolls_everything_back_and_can_be_retried(service, monkeypatch, tmp_path, source, operation):
    current = {"strategy": student_strategy(tmp_path, source)}
    monkeypatch.setattr("workshop.service.load_strategy", lambda name: current["strategy"])
    service.create("s", implementation="state", strategy="student")
    if operation == "apply_change":
        service.run_turn("s")  # the broken function only fails on the party change
    path = service.path("s")
    before = (db_fingerprint(path), artifact_mtimes(path), record_counts(service, "s"), service.status("s")["cursor"]["version"])
    with pytest.raises(StudentCodeError) as exc:
        getattr(service, operation)("s", op_id="op-retry")
    assert exc.value.status == 422
    message = str(exc.value)
    assert "exercises.py" in message and "Nothing was saved" in message
    assert ("ZeroDivisionError" if operation == "apply_change" else "KeyError") in message
    after = (db_fingerprint(path), artifact_mtimes(path), record_counts(service, "s"), service.status("s")["cursor"]["version"])
    assert after == before, "the failed operation left the session exactly as it was"

    current["strategy"] = load_strategy("reference")  # the student fixes the file
    result = getattr(service, operation)("s", op_id="op-retry")
    assert result["replayed"] is False and result["version"] == before[3] + 1, "the failed attempt did not consume the operation ID"


# ---------------------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------------------


def run_in_threads(count: int, fn):
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results[i] = fn(i)
        except Exception as exc:  # noqa: BLE001 - collected for the assertions
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads)
    return results


def test_two_threads_with_the_same_operation_id_execute_it_once(service):
    service.create("c", implementation="state", strategy="reference")
    service.create("twin", implementation="state", strategy="reference")
    baseline = service.run_turn("twin")["detail"]
    results = run_in_threads(2, lambda i: WorkshopService(service.root).run_turn("c", op_id="op-same", expected_version=1))
    assert all(isinstance(r, dict) for r in results), results
    assert sorted(r["replayed"] for r in results) == [False, True]
    assert results[0]["detail"] == results[1]["detail"] == baseline
    assert service.status("c")["cursor"]["version"] == 2
    assert record_counts(service, "c") == record_counts(service, "twin"), "the turn ran once, not twice"


def test_two_threads_racing_with_different_operation_ids_and_one_version_let_only_one_win(service):
    service.create("c", implementation="transcript")
    results = run_in_threads(2, lambda i: service.step("c", op_id=f"op-{i}", expected_version=1))
    wins = [r for r in results if isinstance(r, dict)]
    losses = [r for r in results if isinstance(r, VersionConflict)]
    assert len(wins) == 1 and len(losses) == 1, results
    assert service.status("c")["cursor"]["version"] == 2 and service.status("c")["counts"]["model_calls"] == 1


def test_many_threads_stepping_never_lose_or_duplicate_work(service):
    service.create("c", implementation="state", strategy="reference")
    service.create("twin", implementation="state", strategy="reference")
    service.run_turn("twin")

    def work(i: int):
        return service.step("c", op_id=f"op-{i}")

    results = run_in_threads(4, work)
    assert all(isinstance(r, dict) for r in results), results
    assert sorted(r["version"] for r in results) == [2, 3, 4, 5]
    assert service.status("c")["cursor"]["phase"] == "awaiting_change"
    assert record_counts(service, "c") == record_counts(service, "twin"), "four steps from four threads equal one run_turn"


def test_three_cli_processes_with_one_operation_id_execute_it_once(service, tmp_path, subprocess_env):
    service.create("mp", implementation="state", strategy="reference")
    service.create("base", implementation="state", strategy="reference")
    service.run_turn("base")
    command = [sys.executable, "-m", "workshop.cli", "--root", str(service.root), "session", "run-turn", "mp", "--op-id", "op-shared", "--json"]
    procs = [subprocess.Popen(command, env=subprocess_env, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    outputs = [p.communicate(timeout=180) for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0], [o[1] for o in outputs]
    results = [json.loads(o[0]) for o in outputs]
    assert sorted(r["replayed"] for r in results) == [False, True, True]
    assert {r["version"] for r in results} == {2}
    assert record_counts(service, "mp") == record_counts(service, "base")


def test_the_cli_and_a_service_object_serialize_against_each_other(service, tmp_path, subprocess_env):
    """A CLI process and an in-process caller take the same per-session file lock; only one wins version 1."""
    service.create("x", implementation="state", strategy="reference")
    command = [sys.executable, "-m", "workshop.cli", "--root", str(service.root), "session", "run-turn", "x", "--expected-version", "1", "--json"]
    proc = subprocess.Popen(command, env=subprocess_env, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        in_process = service.run_turn("x", expected_version=1)
    except VersionConflict as exc:
        in_process = exc
    out, err = proc.communicate(timeout=180)
    successes = int(isinstance(in_process, dict)) + int(proc.returncode == 0)
    assert successes == 1, (in_process, proc.returncode, err)
    if proc.returncode != 0:
        assert proc.returncode == 2 and "Session is at version" in err
    status = service.status("x")
    assert status["cursor"]["version"] == 2 and status["cursor"]["phase"] == "awaiting_change"
    assert status["counts"]["model_calls"] == 4, "the turn's four steps ran exactly once"


# ---------------------------------------------------------------------------------------
# The CLI works with no GUI dependency installed
# ---------------------------------------------------------------------------------------

NO_GUI_SCRIPT = r'''
import importlib.abc, json, sys

attempts, current = [], [None]

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("fastapi", "uvicorn", "starlette"):
            attempts.append((current[0], name))
            raise ModuleNotFoundError("blocked for this test: " + name)

sys.meta_path.insert(0, Blocker())
sys.modules["fastapi"] = None
sys.modules["uvicorn"] = None

from workshop.cli import main

root = sys.argv[1]
codes = {}

def run(label, *argv):
    current[0] = label
    codes[label] = main(["--root", root, *argv])

run("run-state", "run", "--implementation", "state", "--strategy", "reference", "--out", "a")
run("run-transcript", "run", "--implementation", "transcript", "--out", "b")
run("run-one-shot", "run", "--implementation", "one-shot", "--scenario", "initial", "--out", "o")
run("start", "session", "start", "c", "--implementation", "state", "--strategy", "reference")
run("apply-too-early", "session", "apply-change", "c")
run("step", "session", "step", "c")
run("run-turn", "session", "run-turn", "c")
run("apply", "session", "apply-change", "c")
run("session-run", "session", "run", "c")
run("show-state", "session", "show", "c", "state")
run("show-diff", "session", "show", "c", "diff")
run("show-model-input", "session", "show", "c", "model-input")
run("export", "session", "export", "c")
run("reset", "session", "reset", "c")
run("list", "session", "list")
run("check", "check", "--strategy", "reference", "--output", root + "/check.json")
run("compare", "compare", "--runs", "a", "b", "--output", root + "/cmp.html")
run("doctor", "doctor")
run("gui", "gui")
loaded = sorted(m for m, v in sys.modules.items() if v is not None and m.split(".")[0] in ("fastapi", "uvicorn", "starlette"))
print("RESULT " + json.dumps({"codes": codes, "attempts": attempts, "loaded": loaded,
                              "gui_modules": sorted(m for m in sys.modules if m.startswith("workshop.gui"))}))
'''


def test_the_full_cli_exercise_runs_with_fastapi_and_uvicorn_unavailable(tmp_path, subprocess_env):
    root = tmp_path / "runs"
    proc = subprocess.run([sys.executable, "-c", NO_GUI_SCRIPT, str(root)], capture_output=True, text=True, env=subprocess_env, cwd=tmp_path, timeout=300)
    assert proc.returncode == 0, proc.stderr[-3000:]
    result = json.loads(next(line for line in proc.stdout.splitlines() if line.startswith("RESULT "))[len("RESULT "):])
    expected_failures = {"apply-too-early": 2, "gui": 2}
    assert result["codes"] == {label: expected_failures.get(label, 0) for label in result["codes"]}, result["codes"]
    assert len(result["codes"]) == 19
    assert result["loaded"] == [], "no GUI package was ever imported"
    assert {label for label, _ in result["attempts"]} <= {"doctor", "gui"}, "only `doctor` (to report the extra) and `gui` even look for the GUI packages"
    assert result["gui_modules"] == [] or result["gui_modules"] == ["workshop.gui"], result["gui_modules"]
    assert "GUI not installed" in proc.stdout and "Ready." in proc.stdout
    assert "uv run --extra gui workshop gui" in proc.stdout
    assert (root / "check.json").exists() and (root / "cmp.html").exists() and (root / "a" / "report.html").exists()


# ---------------------------------------------------------------------------------------
# Equivalent results whichever way a session is driven
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("implementation", ["state", "transcript", "one-shot"])
def test_step_by_step_and_run_to_completion_reach_the_same_result(service, implementation):
    extra = {"restated_brief": True} if implementation == "one-shot" else {}
    run_session(service, "whole", implementation, **extra)
    service.create("steps", implementation=implementation, strategy="reference", **extra)
    while service.status("steps")["cursor"]["phase"] != "complete":
        st = service.status("steps")
        service.apply_change("steps") if st["can_apply_change"] else service.step("steps")
    assert [plan_summary(p) for p in displayed_plans(service, "steps")] == [plan_summary(p) for p in displayed_plans(service, "whole")]
    assert json.loads((service.path("steps") / "checks.json").read_text()) == json.loads((service.path("whole") / "checks.json").read_text())
    assert service.records("steps", "model_calls") and [
        (c["action"], c["input"]) for c in service.records("steps", "model_calls")
    ] == [(c["action"], c["input"]) for c in service.records("whole", "model_calls")]
