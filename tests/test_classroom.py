"""Classroom readiness: the repository works before anyone edits it, and a student's mistake never
looks like broken infrastructure.

The starter ``workshop/exercises.py`` is naive on purpose. These tests run it as shipped. Tests
that need the starter's specific failures skip themselves once the file has been edited, so the
suite stays green for a student who has finished the exercise.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import REFERENCE_IMPORTS, REPO, outcome_kinds, student_strategy
from workshop.checks import _classify, format_report, run_checks
from workshop.cli import main

EXERCISES = REPO / "workshop" / "exercises.py"
PRISTINE = "Starter behavior: overwrite the party and nothing else." in EXERCISES.read_text()
needs_starter = pytest.mark.skipif(not PRISTINE, reason="workshop/exercises.py has been edited; the starter's failures no longer apply")


def run_example(name: str, cwd: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO / "examples" / name), *args], capture_output=True, text=True, env=env, cwd=cwd, timeout=180)


# ---------------------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------------------


def test_doctor_exits_zero_and_reports_each_check(capsys):
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 0 and "Ready." in out and "FAIL" not in out
    for label in ("imports and fixtures", "persistence and simulated run", "report generation", "student exercise module imports"):
        assert f"ok   {label}" in out, label
    assert "optional; not contacted" in out


def test_doctor_in_a_fresh_process_exits_zero(isolated_cwd, subprocess_env):
    proc = subprocess.run([sys.executable, "-m", "workshop.cli", "doctor"], capture_output=True, text=True, env=subprocess_env, cwd=isolated_cwd, timeout=120)
    assert proc.returncode == 0 and "Ready." in proc.stdout, proc.stdout + proc.stderr
    assert list(isolated_cwd.iterdir()) == [], "doctor works in its own temporary directory and leaves nothing behind"


def test_doctor_and_a_full_simulated_run_need_no_network(capsys, monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("a network connection was attempted")

    monkeypatch.setattr("socket.socket.connect", refuse)
    monkeypatch.setattr("socket.socket.connect_ex", refuse)
    monkeypatch.setattr("socket.create_connection", refuse)
    assert main(["doctor"]) == 0
    assert main(["--root", str(tmp_path), "run", "--implementation", "state", "--strategy", "reference", "--out", "offline"]) == 0
    assert main(["--root", str(tmp_path), "check", "--strategy", "reference", "--output", str(tmp_path / "c.json")]) == 0


def test_doctor_reports_a_broken_student_file_without_failing_the_other_checks(capsys, monkeypatch):
    def broken(name):
        raise SyntaxError("invalid syntax (exercises.py, line 2)")

    monkeypatch.setattr("workshop.runner.load_strategy", broken)
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL student exercise module imports" in out and "ok   persistence and simulated run" in out


# ---------------------------------------------------------------------------------------
# The shipped examples
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("script, out_dir", [("one_shot.py", "example-one-shot"), ("transcript_agent.py", "example-transcript"), ("state_agent.py", "example-state")])
def test_the_starter_examples_run_before_students_edit_anything(tmp_path, subprocess_env, script, out_dir):
    proc = run_example(script, tmp_path, subprocess_env)
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:]
    session = tmp_path / "runs" / out_dir
    assert (session / "report.html").exists() and (session / "manifest.json").exists() and (session / "checks.json").exists()
    manifest = json.loads((session / "manifest.json").read_text())
    assert manifest["planner_mode"] == "simulated" and manifest["phase"] == "complete"
    assert f"runs/{out_dir}/report.html" in proc.stdout
    assert "Traceback" not in proc.stderr


def test_the_one_shot_example_also_runs_with_the_restated_brief(tmp_path, subprocess_env):
    proc = run_example("one_shot.py", tmp_path, subprocess_env, "--restated-brief")
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert json.loads((tmp_path / "runs" / "example-one-shot-restated" / "manifest.json").read_text())["restated_brief"] is True


def test_the_state_example_forwards_instructor_options(tmp_path, subprocess_env):
    proc = run_example("state_agent.py", tmp_path, subprocess_env, "--strategy", "reference")
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "plan-004 flight f3 + hotel h17, total $1,740" in proc.stdout


def test_the_examples_can_be_rerun_over_their_own_output(tmp_path, subprocess_env):
    for _ in range(2):
        assert run_example("transcript_agent.py", tmp_path, subprocess_env).returncode == 0


# ---------------------------------------------------------------------------------------
# The starter exercise file
# ---------------------------------------------------------------------------------------


def test_the_starter_state_agent_runs_to_completion_without_crashing(service):
    service.create("starter", implementation="state", strategy="student")
    service.run_to_completion("starter")
    st = service.status("starter")
    assert st["cursor"]["phase"] == "complete"
    assert set(outcome_kinds(service, "starter")) <= {"plan_presented", "question_asked", "incomplete"}
    assert len(outcome_kinds(service, "starter")) == 4


@needs_starter
def test_the_naive_starter_fails_visibly_rather_than_silently(service):
    """With nothing implemented the agent cannot finish its turns, and no non-compliant plan is shown."""
    service.create("starter", implementation="state", strategy="student")
    service.run_to_completion("starter")
    kinds = outcome_kinds(service, "starter")
    assert kinds[0] == "plan_presented" and kinds[1:] == ["incomplete"] * 3
    from tests.helpers import displayed_plans

    assert len(displayed_plans(service, "starter")) == 1, "the validator stopped every later proposal"
    checks = {c["id"]: c["passed"] for c in json.loads((service.path("starter") / "checks.json").read_text())}
    assert checks["displayed_plans_compliant"] is True and checks["turns_finished"] is False


def test_check_on_the_student_file_never_reports_an_infrastructure_error_for_the_starter():
    report = run_checks("student", None)
    statuses = {c["status"] for c in report["exercise_checks"]}
    assert report["overall"] != "infrastructure_error" and "infrastructure_error" not in statuses
    assert [r["status"] for r in report["scenario_runs"]] == ["ran", "ran"]


@needs_starter
def test_check_classifies_every_starter_failure_as_a_failure(tmp_path):
    report = run_checks("student", tmp_path / "out" / "check.json")
    by_id = {c["id"]: c for c in report["exercise_checks"]}
    statuses = {c["status"] for c in by_id.values()}
    assert statuses == {"pass", "fail"}, statuses
    assert report["overall"] == "fail" and report["counts"]["fail"] >= 10
    assert by_id["child_preserves_requirements"]["status"] == "pass", "the starter does overwrite the party"
    assert by_id["duplicate_change_is_noop"]["status"] == "pass" and by_id["two_person_quote_cannot_validate"]["status"] == "pass"
    for c in by_id.values():
        if c["status"] == "fail":
            assert c["detail"] and "Traceback" not in c["detail"], c["id"]
            assert c["detail"].startswith("AssertionError:"), (c["id"], c["detail"])
    assert "still current" in by_id["old_quotes_not_current"]["detail"] and "h11 not in unsuitable_items" in by_id["small_hotel_unsuitable_with_reason"]["detail"]
    assert json.loads((tmp_path / "out" / "check.json").read_text())["overall"] == "fail"


@needs_starter
def test_the_check_command_exit_code_and_message_for_the_starter(capsys, tmp_path):
    code = main(["--root", str(tmp_path), "check", "--strategy", "student", "--output", str(tmp_path / "c.json")])
    out = capsys.readouterr().out
    assert code == 1 and "Overall: FAIL" in out and "FAIL " in out and "ERROR (infrastructure)" not in out and "Tell the instructor" not in out
    assert "Some checks fail. Read the detail lines above" in out


def test_the_reference_implementation_passes_the_complete_suite(capsys, tmp_path):
    report = run_checks("reference", None)
    assert report["overall"] == "pass" and set(report["counts"]) == {"pass"} and report["counts"]["pass"] >= 18
    assert [r["status"] for r in report["scenario_runs"]] == ["ran", "ran"]
    for run in report["scenario_runs"]:
        assert run["checks"] and all(c["passed"] is not False for c in run["checks"]), run["scenario"]
    ids = {c["id"] for r in report["scenario_runs"] for c in r["checks"] if c["passed"] is True}
    assert {"no_compliant_plan_asks", "price_change_flagged", "state_price_rise_reconfirm", "state_expiry_keeps_choice"} <= ids
    assert main(["--root", str(tmp_path), "check", "--strategy", "reference", "--output", str(tmp_path / "c.json")]) == 0
    assert "All checks pass." in capsys.readouterr().out


def meaningful_lines(path: Path) -> list[str]:
    """Code lines inside the two functions: no blanks, no comments."""
    import ast

    source = path.read_text()
    lines = source.splitlines()
    out: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in ("apply_change", "build_context"):
            out += [ln.strip() for ln in lines[node.lineno - 1: node.end_lineno] if ln.strip() and not ln.strip().startswith("#")]
    return out


def test_the_two_student_functions_have_the_specified_signatures():
    import ast

    for path in (EXERCISES, REPO / "solutions" / "reference.py"):
        functions = {n.name: n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef)}
        assert {"apply_change", "build_context"} <= set(functions), path.name
        assert [a.arg for a in functions["apply_change"].args.args] == ["state", "change"]
        assert [a.arg for a in functions["build_context"].args.args] == ["state", "next_step"]


@needs_starter
def test_the_exercise_stays_bounded():
    import difflib

    starter, reference = meaningful_lines(EXERCISES), meaningful_lines(REPO / "solutions" / "reference.py")
    added = [ln for ln in difflib.unified_diff(starter, reference, lineterm="") if ln.startswith("+") and not ln.startswith("+++")]
    assert len(starter) < 60, "the starter skeleton is small"
    assert len(added) <= 120, f"the reference adds {len(added)} meaningful lines; the exercise is meant to fit in minutes"
    assert EXERCISES.read_text().count("# TODO") == 6, "six numbered TODOs, each described where it applies"


# ---------------------------------------------------------------------------------------
# Student errors are not infrastructure errors
# ---------------------------------------------------------------------------------------


def synthetic(exception: BaseException, filename: str) -> BaseException:
    """Raise ``exception`` from code that claims to live in ``filename`` and return it with its traceback."""
    namespace: dict = {}
    exec(compile(f"def boom():\n    raise exc\n", filename, "exec"), {"exc": exception}, namespace)
    try:
        namespace["boom"]()
    except BaseException as caught:  # noqa: BLE001
        return caught
    raise AssertionError("unreachable")


@pytest.mark.parametrize(
    "exception, filename, expected",
    [
        (AssertionError("total is wrong"), "/repo/tests/check.py", "fail"),
        (AssertionError("total is wrong"), "/repo/workshop/exercises.py", "fail"),
        (NotImplementedError("TODO"), "/repo/workshop/exercises.py", "todo"),
        (KeyError("typo"), "/repo/workshop/exercises.py", "student_error"),
        (ZeroDivisionError(), "/home/student/work/exercises.py", "student_error"),
        (KeyError("typo"), "/repo/solutions/reference.py", "student_error"),
        (RuntimeError("disk on fire"), "/repo/workshop/tools.py", "infrastructure_error"),
        (ValueError("bad fixture"), "/repo/workshop/store.py", "infrastructure_error"),
    ],
)
def test_classify_distinguishes_assertion_todo_student_and_infrastructure(exception, filename, expected):
    assert _classify(synthetic(exception, filename)) == expected


def test_an_exception_raised_by_supplied_code_that_the_students_function_called_is_still_the_students():
    supplied: dict = {}
    exec(compile("def inner():\n    raise KeyError('x')\n", "/repo/workshop/tools.py", "exec"), supplied)
    student: dict = {"inner": supplied["inner"]}
    exec(compile("def outer():\n    inner()\n", "/home/student/exercises.py", "exec"), student)
    with pytest.raises(KeyError) as caught:
        student["outer"]()
    assert _classify(caught.value) == "student_error"


def test_an_infrastructure_failure_outside_the_student_file_is_reported_as_infrastructure(monkeypatch, capsys, tmp_path):
    def broken(self, *args, **kwargs):
        raise RuntimeError("fixture store unavailable")

    monkeypatch.setattr("workshop.tools.World.issue_quote", broken)
    report = run_checks("reference", None)
    statuses = {c["status"] for c in report["exercise_checks"]}
    assert "infrastructure_error" in statuses and "student_error" not in statuses
    assert report["overall"] == "infrastructure_error"
    assert {r["status"] for r in report["scenario_runs"]} == {"infrastructure_error"}
    text = format_report(report)
    assert "ERROR (infrastructure)" in text and "Tell the instructor" in text
    assert main(["--root", str(tmp_path), "check", "--strategy", "reference", "--output", str(tmp_path / "c.json")]) == 1
    assert "Infrastructure error" in capsys.readouterr().out


ALWAYS_RAISES = REFERENCE_IMPORTS + '''
def apply_change(state, change):
    raise ZeroDivisionError("student bug")

def build_context(state, step):
    return _context(state, step)
'''
RAISES_ON_PARTY = REFERENCE_IMPORTS + '''
def apply_change(state, change):
    if change.event.type == "PartySizeChanged":
        raise KeyError("pary")
    return _apply(state, change)

def build_context(state, step):
    return _context(state, step)
'''
BAD_CONTEXT = REFERENCE_IMPORTS + '''
def apply_change(state, change):
    return _apply(state, change)

def build_context(state, step):
    return state.requirements.budgett
'''
UNFINISHED = REFERENCE_IMPORTS + '''
def apply_change(state, change):
    if change.event.type == "PartySizeChanged":
        raise NotImplementedError("TODO 1")
    return _apply(state, change)

def build_context(state, step):
    return _context(state, step)
'''


def patch_student(monkeypatch, strategy) -> None:
    monkeypatch.setattr("workshop.checks.load_strategy", lambda name: strategy)
    monkeypatch.setattr("workshop.service.load_strategy", lambda name: strategy)


@pytest.mark.parametrize("source, expected_status", [(ALWAYS_RAISES, "student_error"), (RAISES_ON_PARTY, "student_error"), (BAD_CONTEXT, "student_error")],
                         ids=["raises-always", "raises-on-party-change", "bad-context"])
def test_an_exception_in_the_students_functions_is_a_student_error_in_every_check(monkeypatch, tmp_path, source, expected_status):
    patch_student(monkeypatch, student_strategy(tmp_path, source))
    report = run_checks("student", None)
    exercise = {c["status"] for c in report["exercise_checks"]}
    assert "infrastructure_error" not in exercise, "the exercise checks already blame the student's file"
    assert expected_status in exercise
    runs = {r["scenario"]: r["status"] for r in report["scenario_runs"]}
    assert set(runs.values()) == {"student_error"}, f"the simulated runs hit the same exception: {runs}"
    assert report["overall"] != "infrastructure_error", "a student's exception must not tell them to call the instructor"
    text = format_report(report)
    assert "Tell the instructor" not in text and "exercises.py" in text


def test_unfinished_todo_stubs_are_reported_as_todo_not_as_an_infrastructure_error(monkeypatch, tmp_path):
    patch_student(monkeypatch, student_strategy(tmp_path, UNFINISHED))
    report = run_checks("student", None)
    assert "todo" in {c["status"] for c in report["exercise_checks"]}
    assert report["overall"] == "todo", report["overall"]


def test_a_syntax_error_in_the_students_file_is_a_student_error(monkeypatch, tmp_path, capsys):
    import importlib.util

    broken = tmp_path / "student" / "exercises.py"
    broken.parent.mkdir()
    broken.write_text("def apply_change(state, change):\n    return state +\n")

    def load(name):
        spec = importlib.util.spec_from_file_location("student_exercises_broken", broken)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    monkeypatch.setattr("workshop.checks.load_strategy", load)
    report = run_checks("student", None)
    assert "SyntaxError" in report["error"] and "exercises.py" in report["error"]
    assert report["overall"] == "student_error", f"got {report['overall']!r}: a typo in the student's file is not broken infrastructure"
    assert "Tell the instructor" not in format_report(report)


def test_creating_a_session_with_a_student_file_that_does_not_import_gives_a_service_error(monkeypatch, service, tmp_path):
    """The GUI turns ServiceError into a readable response; a raw SyntaxError would become an HTTP 500."""
    import importlib.util

    from workshop.service import ServiceError

    broken = tmp_path / "student" / "exercises.py"
    broken.parent.mkdir()
    broken.write_text("def apply_change(state, change):\n    return state +\n")

    def load(name):
        spec = importlib.util.spec_from_file_location("student_exercises_broken2", broken)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    monkeypatch.setattr("workshop.service.load_strategy", load)
    with pytest.raises(ServiceError) as exc:
        service.create("s", implementation="state", strategy="student")
    assert "exercises.py" in str(exc.value)


def test_check_json_output_lists_each_check_with_a_status_and_detail(capsys, tmp_path):
    main(["--root", str(tmp_path), "check", "--strategy", "reference", "--output", str(tmp_path / "c.json"), "--json"])
    report = json.loads(capsys.readouterr().out.rsplit("Saved", 1)[0])
    assert {"strategy", "module", "overall", "counts", "exercise_checks", "scenario_runs"} <= set(report)
    for c in report["exercise_checks"]:
        assert set(c) >= {"id", "description", "status", "detail"} and c["status"] == "pass" and c["detail"]
    assert report["module"].endswith("reference.py")
