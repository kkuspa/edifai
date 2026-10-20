"""The optional GUI backend and its equivalence with the CLI.

These tests need the ``gui`` extra (FastAPI and httpx for the test client) and are skipped
without it. The front end is plain JavaScript; here we check the contract it depends on:
read-only GETs, explicit POSTs with operation IDs and versions, local-only access, and the
same domain results as the CLI.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from tests.helpers import (  # noqa: E402
    Gui,
    artifact_mtimes,
    db_fingerprint,
    domain_result,
    install_planner,
    normalize,
    read_jsonl,
    record_counts,
    student_strategy,
    REFERENCE_IMPORTS,
)
from workshop.cli import main  # noqa: E402
from workshop.gui import app as gui_app  # noqa: E402
from workshop.gui.app import STATIC, create_app  # noqa: E402
from workshop.planners import SimulatedPlanner  # noqa: E402
from workshop.runner import load_strategy  # noqa: E402
from workshop.service import WorkshopService  # noqa: E402

GUI_RUN = "state"


@pytest.fixture
def gui(tmp_path) -> Gui:
    return Gui(tmp_path / "gui-runs")


def cli_run(capsys, root: Path, name: str, *extra: str) -> None:
    assert main(["--root", str(root), "run", "--out", name, *extra]) == 0
    capsys.readouterr()


# ---------------------------------------------------------------------------------------
# Local only, and no cross-site requests
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example.com", "127.0.0.1.evil.com", "localhost.evil.com:8765", "0.0.0.0:8765", "192.168.1.20:8765",
                                  "127.0.0.1:9999", "[::1]:8765", ""])
def test_requests_addressed_to_a_non_local_host_are_refused(gui, host):
    for path in ("/api/options", "/api/sessions", "/"):
        r = gui.get(path, headers={"Host": host})
        assert r.status_code == 403 and "localhost" in r.json()["error"], (host, path)
    r = gui.post("/api/sessions", {"session": "x", "implementation": "transcript"}, headers={"Host": host})
    assert r.status_code == 403
    assert gui.get_json("/api/sessions") == [], "and nothing was created"


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "127.0.0.1", "localhost", "testserver"])
def test_local_hosts_are_accepted(gui, host):
    assert gui.get("/api/options", headers={"Host": host}).status_code == 200


def test_the_allowed_port_follows_the_configured_one(tmp_path):
    other = Gui(tmp_path / "r", port=9001)
    assert other.get("/api/options", headers={"Host": "127.0.0.1:9001"}).status_code == 200
    assert other.get("/api/options", headers={"Host": "127.0.0.1:8765"}).status_code == 403


@pytest.mark.parametrize("path, body", [
    ("/api/sessions", {"session": "x", "implementation": "transcript", "op_id": "a"}),
    ("/api/sessions/x/step", {"op_id": "b"}),
    ("/api/sessions/x/apply-change", {"op_id": "c"}),
    ("/api/sessions/x/run-turn", {"op_id": "d"}),
    ("/api/sessions/x/run", {"op_id": "e"}),
    ("/api/sessions/x/reset", {"op_id": "f"}),
    ("/api/sessions/x/export", {"op_id": "g"}),
    ("/api/compare", {"sessions": ["x", "x"], "op_id": "h"}),
    ("/api/check", {"strategy": "reference", "op_id": "i"}),
])
def test_every_post_without_the_client_header_is_refused_and_changes_nothing(gui, path, body):
    gui.create("x", "transcript")
    before = db_fingerprint(gui.root / "x")
    for headers in ({}, {"X-Workshop-Client": "browser"}, {"X-Workshop-Client": ""}):
        r = gui.client.post(path, json=body, headers=headers)
        assert r.status_code == 403 and "x-workshop-client" in r.json()["error"].lower(), headers
    assert db_fingerprint(gui.root / "x") == before


def test_cross_origin_requests_get_no_permission_headers(gui):
    r = gui.get("/api/options", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200 and not [h for h in r.headers if h.lower().startswith("access-control-")]
    preflight = gui.client.options("/api/sessions", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST",
                                                            "Access-Control-Request-Headers": "x-workshop-client, content-type"})
    assert preflight.status_code in (403, 405) and not [h for h in preflight.headers if h.lower().startswith("access-control-")]


def test_the_server_binds_to_loopback_only(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))
    gui_app.serve(root=tmp_path / "r", port=9123)
    assert len(calls) == 1 and calls[0][1]["host"] == "127.0.0.1" and calls[0][1]["port"] == 9123
    assert main(["--root", str(tmp_path / "r2"), "gui", "--port", "9124"]) == 0
    assert calls[1][1]["host"] == "127.0.0.1" and calls[1][1]["port"] == 9124
    assert "0.0.0.0" not in Path(gui_app.__file__).read_text()


def test_credentials_stay_on_the_backend(gui, monkeypatch):
    secret = "sk-ant-api03-GUI-TEST-SECRET-VALUE"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("OPENAI_API_KEY", secret + "-openai")
    options = gui.get("/api/options")
    assert options.json()["live_providers_configured"] == {"anthropic": True, "openai": True}
    gui.create("x", "transcript")
    for path in ("/api/options", "/api/sessions", "/api/sessions/x", "/api/sessions/x/records/model_calls", "/api/sessions/x/transcript"):
        assert secret not in gui.get(path).text, path


@pytest.mark.parametrize("name", [".hidden", "-dash", "has space", "a" * 65, "semi;colon", "ünïcode", "dot..dot/slash"])
def test_session_names_are_validated(gui, name):
    r = gui.post("/api/sessions", {"session": name, "implementation": "transcript"})
    assert r.status_code in (400, 404, 422), (name, r.status_code)
    assert gui.get_json("/api/sessions") == []
    if "/" not in name:
        assert gui.get(f"/api/sessions/{name}").status_code in (400, 404)


# ---------------------------------------------------------------------------------------
# Reads are read-only
# ---------------------------------------------------------------------------------------


READ_PATHS = [
    "/", "/api/options", "/api/sessions", "/api/sessions/s", "/api/sessions/s/state", "/api/sessions/s/diff", "/api/sessions/s/transcript",
    "/api/sessions/s/records/model_calls", "/api/sessions/s/records/tool_calls", "/api/sessions/s/records/plans",
    "/api/sessions/s/records/validations", "/api/sessions/s/records/outcomes", "/api/sessions/s/report", "/api/compare?sessions=s&sessions=t",
    "/static/app.js", "/static/style.css",
]


@pytest.mark.parametrize("implementation", ["state", "transcript"])
def test_opening_panels_refreshing_and_polling_change_nothing(gui, implementation):
    gui.create("s", implementation)
    gui.create("t", "transcript")
    gui.ok("/api/sessions/s/run-turn")
    gui.ok("/api/sessions/s/apply-change")
    gui.ok("/api/sessions/s/step")
    root = gui.root
    before = {n: (db_fingerprint(root / n), artifact_mtimes(root / n)) for n in ("s", "t")}
    status = gui.status("s")
    for _ in range(3):
        for path in READ_PATHS:
            r = gui.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text[:200])
        for snapshot in range(1, 6):
            gui.get(f"/api/sessions/s/state?snapshot={snapshot}"), gui.get(f"/api/sessions/s/diff?snapshot={snapshot}")
    assert {n: (db_fingerprint(root / n), artifact_mtimes(root / n)) for n in ("s", "t")} == before
    assert gui.status("s") == status, "the same status comes back every time"
    assert gui.status("s")["cursor"]["version"] == 4


def test_polling_during_a_running_action_sees_only_committed_states_and_never_blocks(tmp_path, monkeypatch):
    class Slow(SimulatedPlanner):
        def next_action(self, model_input, call_id):
            time.sleep(0.04)
            return super().next_action(model_input, call_id)

    install_planner(monkeypatch, Slow())
    writer, poller = Gui(tmp_path / "r"), Gui(tmp_path / "r")
    writer.create("slow", "transcript")
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.update(r=writer.post("/api/sessions/slow/run", expected=1)))
    started = time.perf_counter()
    thread.start()
    seen = []
    while thread.is_alive():
        r = poller.get("/api/sessions/slow")
        assert r.status_code == 200, r.text
        body = r.json()
        seen.append((body["cursor"]["version"], body["cursor"]["phase"], body["counts"]["model_calls"]))
        poller.get_json("/api/sessions/slow/records/model_calls")
        time.sleep(0.01)
    thread.join()
    assert outcome["r"].status_code == 200
    final = poller.status("slow")
    committed = {(1, "ready", 0), (2, "complete", final["counts"]["model_calls"])}
    assert set(seen) <= committed, f"a poll saw a half-applied state: {set(seen) - committed}"
    during = [s for s in seen if s[0] == 1]
    assert len(during) >= 3, f"polls were blocked while the action ran ({len(seen)} polls in {time.perf_counter() - started:.2f}s)"


# ---------------------------------------------------------------------------------------
# Explicit actions, operation IDs, versions
# ---------------------------------------------------------------------------------------


def test_actions_require_an_operation_id(gui):
    gui.create("s", "transcript")
    root = gui.root
    before = db_fingerprint(root / "s")
    for path in ("/api/sessions/s/step", "/api/sessions/s/run-turn", "/api/sessions/s/apply-change", "/api/sessions/s/run", "/api/sessions/s/reset"):
        r = gui.client.post(path, json={}, headers={"X-Workshop-Client": "gui"})
        assert r.status_code == 422 and "op_id" in json.dumps(r.json()), path
    assert gui.client.post("/api/sessions", json={"session": "n", "implementation": "state"}, headers={"X-Workshop-Client": "gui"}).status_code == 422
    assert db_fingerprint(root / "s") == before


def test_creating_a_session_from_each_choice_on_the_form(gui):
    options = gui.get_json("/api/options")
    assert options["implementations"] == ["one-shot", "transcript", "state"] and options["planners"] == ["simulated", "live"]
    assert options["strategies"] == ["student", "reference"]
    assert {s["name"] for s in options["scenarios"]} >= {"initial", "changing-trip", "no-compliant"}
    assert all(set(s) == {"name", "description", "turns"} for s in options["scenarios"])
    assert set(options["live_providers_configured"]) == {"anthropic", "openai"} and all(isinstance(v, bool) for v in options["live_providers_configured"].values())
    for i, impl in enumerate(options["implementations"]):
        scenario = "initial" if impl == "one-shot" else "changing-trip"
        body = gui.create(f"s{i}", impl, scenario=scenario, planner="simulated")
        assert body["ok"] and body["version"] == 1 and body["phase"] == "ready" and body["replayed"] is False
        cfg = gui.status(f"s{i}")["config"]
        assert (cfg["implementation"], cfg["scenario"], cfg["planner"]) == (impl, scenario, "simulated")
    assert [s["session"] for s in gui.get_json("/api/sessions")] == ["s0", "s1", "s2"]
    restated = gui.create("restated", "one-shot", restated_brief=True)
    assert gui.status("restated")["config"]["restated_brief"] is True and restated["ok"]


def test_a_live_session_without_credentials_is_refused_with_a_message(gui):
    r = gui.post("/api/sessions", {"session": "live", "implementation": "transcript", "planner": "live"})
    assert r.status_code == 400 and "No live provider configured" in r.json()["error"]
    assert gui.get_json("/api/sessions") == []


def test_bad_scenarios_unknown_sessions_and_double_creation_get_clear_statuses(gui):
    r = gui.post("/api/sessions", {"session": "x", "implementation": "transcript", "scenario": "nope"})
    assert r.status_code == 400 and "Unknown scenario" in r.json()["error"]
    assert gui.get("/api/sessions/ghost").status_code == 404
    assert gui.post("/api/sessions/ghost/step").status_code == 404
    gui.create("x", "transcript")
    twice = gui.post("/api/sessions", {"session": "x", "implementation": "state", "strategy": "reference"})
    assert twice.status_code == 409 and twice.json()["type"] == "SessionExists"
    assert gui.status("x")["config"]["implementation"] == "transcript"
    assert gui.ok("/api/sessions", {"session": "x", "implementation": "state", "strategy": "reference", "overwrite": True})["replayed"] is False
    assert gui.status("x")["config"]["implementation"] == "state"


def test_a_retried_post_with_the_same_operation_id_does_not_duplicate_work(gui):
    created = gui.ok("/api/sessions", {"session": "s", "implementation": "state", "strategy": "reference"}, op_id="create-1")
    assert gui.ok("/api/sessions", {"session": "s", "implementation": "state", "strategy": "reference"}, op_id="create-1")["replayed"] is True
    root = gui.root
    service = WorkshopService(root)
    first = gui.ok("/api/sessions/s/run-turn", op_id="turn-1", expected=created["version"])
    counts, fingerprint = record_counts(service, "s"), db_fingerprint(root / "s")
    for _ in range(3):
        again = gui.ok("/api/sessions/s/run-turn", op_id="turn-1", expected=created["version"])
        assert again["replayed"] is True and again["detail"] == first["detail"] and again["version"] == first["version"]
    assert record_counts(service, "s") == counts and db_fingerprint(root / "s") == fingerprint
    assert gui.status("s")["cursor"]["version"] == 2
    change = gui.ok("/api/sessions/s/apply-change", op_id="change-1", expected=2)
    assert gui.ok("/api/sessions/s/apply-change", op_id="change-1", expected=2)["detail"] == change["detail"], "a lost response is safe to retry"
    assert gui.status("s")["cursor"]["version"] == 3


def test_a_stale_expected_version_gets_409_and_changes_nothing(gui):
    gui.create("s", "state")
    root = gui.root
    gui.ok("/api/sessions/s/step", expected=1)
    before = (db_fingerprint(root / "s"), artifact_mtimes(root / "s"))
    for path in ("step", "run-turn", "run", "apply-change"):
        r = gui.post(f"/api/sessions/s/{path}", expected=1)
        assert r.status_code == 409 and r.json()["type"] == "VersionConflict" and "version 2" in r.json()["error"], path
    assert (db_fingerprint(root / "s"), artifact_mtimes(root / "s")) == before
    assert gui.ok("/api/sessions/s/step", expected=2)["version"] == 3


def test_an_action_that_does_not_fit_the_phase_gets_409_and_changes_nothing(gui):
    gui.create("s", "transcript")
    before = db_fingerprint(gui.root / "s")
    r = gui.post("/api/sessions/s/apply-change", expected=1)
    assert r.status_code == 409 and r.json()["type"] == "InvalidOperation" and "'ready'" in r.json()["error"]
    assert db_fingerprint(gui.root / "s") == before, "the refusal changed nothing"
    gui.ok("/api/sessions/s/run-turn")
    r = gui.post("/api/sessions/s/step")
    assert r.status_code == 409 and "awaiting_change" in r.json()["error"]
    gui.ok("/api/sessions/s/run")
    assert gui.post("/api/sessions/s/step").status_code == 409
    assert gui.status("s")["cursor"]["phase"] == "complete"


def test_a_stale_reset_is_refused_instead_of_destroying_progress(gui):
    """Reset is the most destructive action. A page that has not seen the latest version must not run it."""
    gui.create("s", "state")
    gui.ok("/api/sessions/s/run", expected=1)
    root = gui.root
    before = (db_fingerprint(root / "s"), gui.status("s")["cursor"]["version"])
    r = gui.post("/api/sessions/s/reset", expected=1)
    assert r.status_code == 409, "the page last saw version 1; the session is at version 2"
    assert (db_fingerprint(root / "s"), gui.status("s")["cursor"]["version"]) == before
    assert gui.ok("/api/sessions/s/reset", expected=before[1])["phase"] == "ready"


def test_reset_is_a_deliberate_post_and_restores_the_starting_point(gui):
    gui.create("s", "state", scenario="no-compliant")
    gui.ok("/api/sessions/s/run")
    gui.create("other", "transcript")
    gui.ok("/api/sessions/other/run-turn")
    other = gui.root / "other"
    other_before = db_fingerprint(other)
    result = gui.ok("/api/sessions/s/reset", op_id="reset-1")
    assert (result["kind"], result["version"], result["phase"]) == ("reset", 1, "ready")
    st = gui.status("s")
    assert st["counts"]["model_calls"] == 0 and st["displayed_proposal"] is None and st["clock"] == "2027-03-15T14:00:00+00:00"
    assert st["config"]["scenario"] == "no-compliant" and st["config"]["implementation"] == "state"
    assert gui.ok("/api/sessions/s/reset", op_id="reset-1")["replayed"] is True
    assert db_fingerprint(other) == other_before, "only the chosen session was reset"
    gui.ok("/api/sessions/s/run")
    assert gui.status("s")["cursor"]["phase"] == "complete"


def test_two_requests_with_one_operation_id_run_once_over_http(tmp_path):
    a, b = Gui(tmp_path / "r"), Gui(tmp_path / "r")
    a.create("s", "state")
    a.create("twin", "state")
    baseline = a.ok("/api/sessions/twin/run-turn")["detail"]
    barrier = threading.Barrier(2)
    responses = []

    def fire(client: Gui) -> None:
        barrier.wait()
        responses.append(client.post("/api/sessions/s/run-turn", op_id="op-same", expected=1))

    threads = [threading.Thread(target=fire, args=(c,)) for c in (a, b)]
    [t.start() for t in threads]
    [t.join(timeout=120) for t in threads]
    assert [r.status_code for r in responses] == [200, 200]
    bodies = [r.json() for r in responses]
    assert sorted(x["replayed"] for x in bodies) == [False, True] and bodies[0]["detail"] == bodies[1]["detail"] == baseline
    service = WorkshopService(tmp_path / "r")
    assert record_counts(service, "s") == record_counts(service, "twin") and a.status("s")["cursor"]["version"] == 2


def test_student_code_errors_come_back_as_422_with_a_fixable_message(tmp_path, monkeypatch):
    source = REFERENCE_IMPORTS + (
        "def apply_change(state, change):\n    raise KeyError('typo')\n\ndef build_context(state, step):\n    return _context(state, step)\n"
    )
    current = {"strategy": student_strategy(tmp_path, source)}
    monkeypatch.setattr("workshop.service.load_strategy", lambda name: current["strategy"])
    gui = Gui(tmp_path / "r")
    gui.create("s", "state", strategy="student")
    root = tmp_path / "r"
    before = (db_fingerprint(root / "s"), gui.status("s")["cursor"]["version"])
    r = gui.post("/api/sessions/s/step", op_id="step-1", expected=1)
    assert r.status_code == 422 and r.json()["type"] == "StudentCodeError"
    assert "exercises.py" in r.json()["error"] and "KeyError" in r.json()["error"] and "Nothing was saved" in r.json()["error"]
    assert (db_fingerprint(root / "s"), gui.status("s")["cursor"]["version"]) == before
    current["strategy"] = load_strategy("reference")
    again = gui.post("/api/sessions/s/step", op_id="step-1", expected=1)
    assert again.status_code == 200 and again.json()["replayed"] is False


# ---------------------------------------------------------------------------------------
# What the inspector panels read
# ---------------------------------------------------------------------------------------


def test_the_inspector_endpoints_expose_state_diffs_inputs_tools_proposals_and_validation(gui):
    gui.create("s", "state")
    gui.ok("/api/sessions/s/run")
    snapshots = gui.get_json("/api/sessions/s/state")["snapshots"]
    by_change = {s["change_id"]: s["index"] for s in snapshots}
    party = gui.get_json(f"/api/sessions/s/state?snapshot={by_change['chg-t1-0']}")
    assert party["state"]["requirements"]["party"] == {"adults": 2, "children": 1} and party["state"]["decisions"]["hotel"] is None
    diff = gui.get_json(f"/api/sessions/s/diff?snapshot={by_change['chg-t1-0']}")
    paths = {d["path"]: d for d in diff["changes"]}
    assert paths["requirements.party.children"]["before"] == 0 and paths["requirements.party.children"]["after"] == 1
    assert not [p for p in paths if p.startswith(("requirements.origin", "requirements.destination", "requirements.budget", "requirements.depart_date"))]
    assert any(p.endswith(".status") and d["after"] == "stale" for p, d in paths.items()), "old-party quotes became stale"
    assert "unsuitable_items.h11" in " ".join(paths)
    last = gui.get_json("/api/sessions/s/state")
    assert last["index"] == snapshots[-1]["index"] and last["state"]["decisions"]["hotel"]["status"] == "needs_reconfirmation"

    calls = gui.records("s", "model_calls")
    assert calls[0]["input"]["system"] and calls[0]["input"]["tools"] and calls[-1]["input"]["context"]["sections"]
    assert all(c["planner_mode"] == "simulated" and c["usage"] is None for c in calls)
    tools = gui.records("s", "tool_calls")
    assert {t["name"] for t in tools} >= {"search_hotels", "search_flights", "refresh_quote", "validate_plan"} and all("result" in t and "arguments" in t for t in tools)
    plans = gui.records("s", "plans")
    assert [p["proposal"]["proposal_id"] for p in plans if p["displayed"]] == ["plan-001", "plan-002", "plan-003", "plan-004"]
    validations = gui.records("s", "validations")
    assert any(v["result"]["computed_total_cents"] == 186_000 and not v["result"]["hard_constraints_ok"] for v in validations)
    assert [o["kind"] for o in gui.records("s", "outcomes")] == ["plan_presented"] * 4
    assert gui.get("/api/sessions/s/state?snapshot=9999").status_code == 404 and gui.get("/api/sessions/s/records/world").status_code == 400


def test_a_transcript_session_reports_that_it_has_no_state(gui):
    gui.create("t", "transcript")
    gui.ok("/api/sessions/t/run")
    state = gui.get_json("/api/sessions/t/state")
    assert state["available"] is False and "transcript" in state["reason"]
    assert gui.get_json("/api/sessions/t/diff")["available"] is False
    assert gui.status("t")["state_summary"] is None


def test_status_drives_the_page_buttons_and_the_conversation(gui):
    gui.create("s", "state")
    st = gui.status("s")
    assert st["can_step"] and not st["can_apply_change"] and st["scenario"]["turns"][0]["text"].startswith("Actually")
    gui.ok("/api/sessions/s/run-turn")
    st = gui.status("s")
    assert st["can_apply_change"] and not st["can_step"] and st["next_change"]["turn"] == 1 and st["displayed_proposal"]["proposal_id"] == "plan-001"
    assert [c["speaker"] for c in st["conversation"]] == ["user", "assistant"]
    assert st["conversation"][1]["proposal"]["computed_total_cents"] == 120_000
    applied = gui.ok("/api/sessions/s/apply-change")
    assert applied["detail"]["applied"] == ["chg-t1-0"] and applied["phase"] == "ready"
    assert [q["quote_id"] for q in gui.status("s")["state_summary"]["invalidated_quotes"]], "the invalidated observations are listed for the page"
    assert gui.status("s")["state_summary"]["unsuitable_items"]["h11"]["reason"]
    assert [w["valid_now"] for w in gui.status("s")["world_quotes"]]


def test_the_page_changes_are_the_three_scenario_turns_in_order(gui):
    gui.create("s", "state")
    gui.ok("/api/sessions/s/run-turn")
    applied = []
    while gui.status("s")["can_apply_change"]:
        applied.append(gui.ok("/api/sessions/s/apply-change")["detail"]["applied"])
        gui.ok("/api/sessions/s/run-turn")
    assert applied == [["chg-t1-0"], ["chg-t2-0", "chg-t2-1"], ["chg-t3-0", "chg-t3-1"]]


def test_the_report_endpoint_serves_the_generated_html(gui):
    gui.create("s", "state")
    assert gui.get("/api/sessions/s/report").status_code == 200, "creating a session already writes its first report"
    gui.ok("/api/sessions/s/run")
    r = gui.get("/api/sessions/s/report")
    assert r.headers["content-type"].startswith("text/html") and r.headers["cache-control"] == "no-store"
    assert "plan-004" in r.text and "planner_mode=simulated" in r.text


def test_compare_check_and_export_endpoints(gui, tmp_path, monkeypatch):
    gui.create("a", "transcript")
    gui.create("b", "state")
    gui.ok("/api/sessions/a/run")
    gui.ok("/api/sessions/b/run")
    summaries = gui.get_json("/api/compare?sessions=a&sessions=b")
    assert [s["config"]["implementation"] for s in summaries] == ["transcript", "state"]
    assert [t["proposal"]["proposal_id"] for s in summaries for t in s["turns"] if t["proposal"]] == ["plan-001", "plan-002", "plan-003", "plan-004"] * 2
    out = tmp_path / "evidence" / "cmp.html"
    first = gui.ok("/api/compare", {"sessions": ["a", "b"], "output": str(out)}, op_id="cmp-1")
    assert first["replayed"] is False and out.exists() and "Run comparison" in out.read_text()
    out.unlink()
    assert gui.ok("/api/compare", {"sessions": ["a", "b"], "output": str(out)}, op_id="cmp-1")["replayed"] is True and not out.exists()
    assert gui.post("/api/compare", {"sessions": ["a", "ghost"], "output": str(out)}).status_code == 404

    runs_of_check = []
    import workshop.checks as checks

    original = checks.run_checks
    monkeypatch.setattr(checks, "run_checks", lambda *a, **k: runs_of_check.append(a) or original(*a, **k))
    report = gui.ok("/api/check", {"strategy": "reference"}, op_id="check-1")
    assert report["report"]["overall"] == "pass" and report["report"]["strategy"] == "reference"
    assert gui.ok("/api/check", {"strategy": "reference"}, op_id="check-1")["replayed"] is True
    assert len(runs_of_check) == 1, "a repeated operation ID did not run the checks again"
    assert (Path.cwd() / "submissions" / "check-results.json").exists(), "evidence lands in the working directory, which the tests keep temporary"

    exported = gui.ok("/api/sessions/b/export")
    assert "report.html" in exported["files"] and "state_snapshots.jsonl" in exported["files"]


def test_a_live_session_can_be_created_from_the_page_when_a_provider_is_configured(gui, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-GUI-TEST")
    monkeypatch.setattr("openai.OpenAI", lambda *a, **k: object())
    body = gui.create("live", "transcript", planner="live", provider="openai")
    cfg = gui.status("live")["config"]
    assert body["ok"] and (cfg["planner"], cfg["provider"]) == ("live", "openai")
    assert cfg["planner_settings"]["planner_mode"] == "live" and "sk-openai" not in json.dumps(gui.status("live"))


# ---------------------------------------------------------------------------------------
# CLI and GUI produce the same domain results
# ---------------------------------------------------------------------------------------


def gui_read(gui: Gui, name: str):
    return lambda kind: gui.records(name, kind)


def artifact_set(root: Path, name: str) -> dict[str, object]:
    out: dict[str, object] = {}
    for p in sorted((root / name).iterdir()):
        if p.name.startswith(("session.sqlite", ".")):
            continue
        if p.suffix == ".jsonl":
            out[p.name] = normalize(read_jsonl(p))
        elif p.suffix == ".json":
            out[p.name] = normalize(json.loads(p.read_text()))
        else:
            out[p.name] = p.read_text()
    return out


EQUIVALENT = [
    ("state", "changing-trip", []),
    ("transcript", "changing-trip", []),
    ("one-shot", "changing-trip", ["--restated-brief"]),
    ("state", "no-compliant", []),
]


@pytest.mark.parametrize("mode", ["run", "turn", "step"])
@pytest.mark.parametrize("implementation, scenario, extra", EQUIVALENT, ids=["state", "transcript", "one-shot-restated", "state-no-compliant"])
def test_cli_and_gui_produce_equivalent_domain_results(capsys, tmp_path, implementation, scenario, extra, mode):
    cli_root, gui_root = tmp_path / "cli", tmp_path / "gui"
    cli_run(capsys, cli_root, "a", "--implementation", implementation, "--scenario", scenario, "--strategy", "reference", *extra)
    gui = Gui(gui_root)
    gui.create("b", implementation, scenario=scenario, restated_brief="--restated-brief" in extra)
    gui.finish("b", mode)
    cli_service = WorkshopService(cli_root)

    cli_checks = json.loads((cli_root / "a" / "checks.json").read_text())
    gui_checks = json.loads((gui_root / "b" / "checks.json").read_text())
    cli_view = domain_result(lambda k: cli_service.records("a", k), cli_checks)
    gui_view = domain_result(gui_read(gui, "b"), gui_checks)
    assert gui_view == cli_view
    assert cli_view["displayed"]
    assert normalize(cli_checks) == normalize(gui_checks), "checks.json is equivalent"

    # Equivalent submission evidence: the same files with the same normalized contents.
    cli_files, gui_files = artifact_set(cli_root, "a"), artifact_set(gui_root, "b")
    assert set(cli_files) == set(gui_files)
    for name in cli_files:
        if name in ("report.html", "manifest.json"):
            continue
        assert gui_files[name] == cli_files[name], name
    assert normalize(json.loads((cli_root / "a" / "manifest.json").read_text())) == normalize(json.loads((gui_root / "b" / "manifest.json").read_text()))
    cli_cfg = cli_service.status("a")["config"]
    gui_cfg = gui.status("b")["config"]
    assert normalize({k: cli_cfg[k] for k in cli_cfg if k != "session_id"}) == normalize({k: gui_cfg[k] for k in gui_cfg if k != "session_id"})


def test_the_reports_from_both_interfaces_differ_only_in_names_and_paths(capsys, tmp_path):
    cli_root, gui_root = tmp_path / "cli", tmp_path / "gui"
    cli_run(capsys, cli_root, "alpha", "--implementation", "state", "--strategy", "reference")
    gui = Gui(gui_root)
    gui.create("beta", "state")
    gui.finish("beta", "run")

    def scrub(root: Path, name: str) -> str:
        text = (root / name / "report.html").read_text()
        run_id = json.loads((root / name / "manifest.json").read_text())["run_id"]
        return text.replace(str(root / name), "PATH").replace(run_id, "RUN").replace(name, "SESSION")

    assert scrub(cli_root, "alpha") == scrub(gui_root, "beta")


def test_equivalent_runs_make_equal_tool_calls_with_equal_arguments(capsys, tmp_path):
    cli_run(capsys, tmp_path / "cli", "a", "--implementation", "transcript")
    gui = Gui(tmp_path / "gui")
    gui.create("b", "transcript")
    gui.finish("b", "step")
    cli_calls = WorkshopService(tmp_path / "cli").records("a", "tool_calls")
    gui_calls = gui.records("b", "tool_calls")
    assert [(t["name"], t["arguments"], t["tool_call_id"]) for t in cli_calls] == [(t["name"], t["arguments"], t["tool_call_id"]) for t in gui_calls]
    assert [t["result"] for t in cli_calls] == [t["result"] for t in gui_calls]


# ---------------------------------------------------------------------------------------
# Switching interfaces mid-session
# ---------------------------------------------------------------------------------------


def baseline_domain(capsys, tmp_path: Path, implementation: str = "state"):
    cli_run(capsys, tmp_path / "baseline", "whole", "--implementation", implementation, "--strategy", "reference")
    service = WorkshopService(tmp_path / "baseline")
    return domain_result(lambda k: service.records("whole", k), json.loads((tmp_path / "baseline" / "whole" / "checks.json").read_text()))


def test_start_and_step_in_the_cli_continue_in_the_gui_and_finish_in_the_cli(capsys, tmp_path):
    expected = baseline_domain(capsys, tmp_path)
    root = tmp_path / "shared"
    argv = ["--root", str(root)]
    assert main([*argv, "session", "start", "s", "--implementation", "state", "--strategy", "reference"]) == 0
    assert main([*argv, "session", "step", "s", "--count", "2", "--op-id", "cli-step-1"]) == 0
    capsys.readouterr()

    gui = Gui(root)
    st = gui.status("s")
    assert st["cursor"]["phase"] == "ready" and st["counts"]["model_calls"] == 2 and st["cursor"]["version"] == 3, "the page sees the CLI's progress"
    assert [o["name"] for o in gui.records("s", "tool_calls")][:2] == ["search_hotels", "search_flights"]
    assert gui.ok("/api/sessions/s/step", op_id="cli-step-1")["replayed"] is True, "operation IDs are shared between interfaces"
    gui.ok("/api/sessions/s/run-turn", expected=3)
    gui.ok("/api/sessions/s/apply-change", expected=4)
    gui.ok("/api/sessions/s/step", expected=5)
    assert gui.status("s")["cursor"]["version"] == 6 and gui.status("s")["cursor"]["turn"] == 1

    assert main([*argv, "session", "status", "s"]) == 0
    assert "turn=1" in capsys.readouterr().out
    assert main([*argv, "session", "run", "s", "--expected-version", "6"]) == 0
    capsys.readouterr()
    service = WorkshopService(root)
    final = domain_result(lambda k: service.records("s", k), json.loads((root / "s" / "checks.json").read_text()))
    assert final == expected, "switching interfaces did not change the outcome"
    assert [t["name"] for t in service.records("s", "tool_calls")] == [t["name"] for t in gui.records("s", "tool_calls")]
    assert service.status("s")["cursor"]["version"] == 7


def test_start_in_the_gui_continue_in_the_cli_finish_in_the_gui(capsys, tmp_path):
    expected = baseline_domain(capsys, tmp_path, "transcript")
    root = tmp_path / "shared"
    gui = Gui(root)
    gui.create("s", "transcript")
    gui.ok("/api/sessions/s/run-turn", expected=1)
    assert main(["--root", str(root), "session", "apply-change", "s", "--expected-version", "2"]) == 0
    assert main(["--root", str(root), "session", "run-turn", "s", "--expected-version", "3"]) == 0
    capsys.readouterr()
    st = gui.status("s")
    assert st["cursor"]["version"] == 4 and st["cursor"]["turn"] == 1 and st["can_apply_change"]
    stale = gui.post("/api/sessions/s/run", expected=2)
    assert stale.status_code == 409, "a page that has not seen the CLI's work is refused"
    gui.ok("/api/sessions/s/run", expected=4)
    service = WorkshopService(root)
    final = domain_result(lambda k: service.records("s", k), json.loads((root / "s" / "checks.json").read_text()))
    assert final == expected


# ---------------------------------------------------------------------------------------
# The static page
# ---------------------------------------------------------------------------------------


def test_the_page_offers_every_control_the_addendum_names(gui):
    r = gui.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and r.headers["cache-control"] == "no-store"
    page = r.text
    for implementation in ("one-shot", "transcript", "state"):
        assert f'name="impl" value="{implementation}"' in page
    for needed in ('id="f-planner"', 'value="simulated"', 'value="live"', 'id="f-scenario"', 'id="f-strategy"', 'id="change-buttons"',
                   'id="btn-step"', 'id="btn-turn"', 'id="btn-run"', 'id="btn-reset"', 'id="btn-export"', 'id="btn-check"',
                   'id="btn-compare"', 'id="btn-compare-export"', 'id="state-view"', 'id="model-view"', 'id="tools-view"',
                   'id="validation-view"', 'id="plan-card"', 'id="conversation"', 'id="compare-view"', 'id="check-view"'):
        assert needed in page, needed
    for tab in ("state", "model", "tools", "validation", "conversation", "compare", "checks"):
        assert f'data-tab="{tab}"' in page
    assert not re.findall(r'(?:src|href)="(?:https?:)?//', page), "no external assets"
    assert gui.get("/static/app.js").status_code == 200 and gui.get("/static/style.css").status_code == 200
    assert gui.get("/static/missing.js").status_code == 404


def test_the_script_only_uses_routes_that_exist_and_sends_the_safety_fields():
    path = STATIC / "app.js"
    if not path.exists():
        pytest.skip("app.js is not present")
    js = path.read_text()
    assert "X-Workshop-Client" in js and "op_id" in js and "expected_version" in js
    assert not re.search(r"\.innerHTML\s*=|insertAdjacentHTML|document\.write|\beval\(|new Function\(", js), "server text must only reach the page as text"
    assert not re.findall(r"""(?:fetch|src\s*=|href\s*=)\s*\(?\s*["']https?://""", js), "no calls to other hosts"

    app = create_app(Path("unused"))
    routes = [r for r in app.routes if hasattr(r, "path_regex")]

    def served(method: str, concrete: str) -> bool:
        return any(method in (r.methods or ()) and r.path_regex.match(concrete) for r in routes)

    suffixes = set(re.findall(r'sessionUrl\("(/[A-Za-z0-9_/-]*)"', js))
    posts = {"/apply-change", "/step", "/run-turn", "/run", "/reset", "/export"}
    gets = {"/state", "/diff", "/records/model_calls", "/records/tool_calls", "/records/validations"}
    assert (posts | gets) <= suffixes, f"the page no longer calls: {sorted((posts | gets) - suffixes)}"
    for suffix in suffixes:
        method = "POST" if suffix in posts else "GET"
        assert served(method, f"/api/sessions/some-session{suffix}"), (method, suffix)
    for literal, method in (("/api/options", "GET"), ("/api/sessions", "GET"), ("/api/sessions", "POST"), ("/api/compare", "GET"),
                            ("/api/compare", "POST"), ("/api/check", "POST")):
        assert f'"{literal}' in js and served(method, literal), (method, literal)
