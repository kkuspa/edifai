"""Shared helpers for the workshop test suite.

Nothing here touches the repository's own ``runs/`` or ``submissions/`` directories. Every
test gets a temporary working directory (see conftest.py) and builds sessions under it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable

from workshop.models import (
    Change,
    ModelInput,
    PlannerAction,
    PlannerResponse,
    Quote,
    TranscriptEntry,
    ValidationResult,
)
from workshop.planners import SimulatedPlanner, action_to_tool_call
from workshop.runner import load_scenario
from workshop.tools import ToolBox, World
from workshop.validation import Authority, validate_plan

REPO = Path(__file__).resolve().parent.parent
BUDGET_CENTS = 180_000
TRIP_DATES = {"depart_date": "2027-04-12", "return_date": "2027-04-15"}
GENERATED_KEYS = {"run_id", "session", "path", "op_id", "version"}


# ======================================================================================
# Reading recorded sessions
# ======================================================================================


def read_jsonl(path: Path) -> list[Any]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def displayed_plans(service: Any, name: str) -> list[dict[str, Any]]:
    return [p["proposal"] for p in service.records(name, "plans") if p.get("displayed")]


def agent_tool_sequence(service: Any, name: str) -> list[tuple[str, dict[str, Any]]]:
    return [(t["name"], t["arguments"]) for t in service.records(name, "tool_calls") if t["caller"] == "agent"]


def outcome_kinds(service: Any, name: str) -> list[str]:
    return [o["kind"] for o in service.records(name, "outcomes")]


def plan_summary(proposal: dict[str, Any]) -> dict[str, Any]:
    v = proposal["validation"]
    return {
        "proposal_id": proposal["proposal_id"],
        "turn": proposal["turn"],
        "flight_id": proposal["flight_id"],
        "hotel_id": proposal["hotel_id"],
        "flight_quote_id": proposal["flight_quote_id"],
        "hotel_quote_id": proposal["hotel_quote_id"],
        "total_cents": proposal["computed_total_cents"],
        "claimed_total_cents": proposal["claimed_total_cents"],
        "confirmation_status": proposal["confirmation_status"],
        "requirement_revision": proposal["requirement_revision"],
        "hard_constraints_ok": v["hard_constraints_ok"],
        "quotes_fresh": v["quotes_fresh"],
        "preferences_met": v["preferences_met"],
        "confirmation_required": v["confirmation_required"],
        "codes": [r["code"] for r in v["reasons"]],
    }


def normalize(obj: Any) -> Any:
    """Drop generated IDs and the operation counter so two runs can be compared."""
    if isinstance(obj, dict):
        return {k: normalize(v) for k, v in obj.items() if k not in GENERATED_KEYS}
    if isinstance(obj, list):
        return [normalize(v) for v in obj]
    return obj


def domain_result(read: Callable[[str], list[Any]], checks: list[dict[str, Any]]) -> dict[str, Any]:
    """The domain-level outcome of a run, independent of interface, IDs, and timing."""
    plans = read("plans")
    tools = [t for t in read("tool_calls") if t["caller"] == "agent"]
    return {
        "displayed": [plan_summary(p["proposal"]) for p in plans if p.get("displayed")],
        "rejected": [[r["code"] for r in p["validation"]["reasons"]] for p in plans if not p.get("displayed")],
        "outcomes": [{k: o.get(k) for k in ("turn", "kind", "proposal_id", "question", "unresolved_constraint", "reason", "steps", "tool_calls")}
                     for o in read("outcomes")],
        "tool_sequence": [(t["name"], t["arguments"]) for t in tools],
        "validations": [(v["source"], v["result"]["computed_total_cents"], [r["code"] for r in v["result"]["reasons"]]) for v in read("validations")],
        "checks": checks,
    }


def db_fingerprint(session_dir: Path) -> str:
    """Hash of every table in the session database. Equal hashes mean nothing was written."""
    con = sqlite3.connect(session_dir / "session.sqlite")
    try:
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
        dump = {t: con.execute(f"select * from {t} order by 1, 2").fetchall() for t in tables}
    finally:
        con.close()
    return hashlib.sha256(json.dumps(dump, sort_keys=True, default=str).encode()).hexdigest()


def artifact_mtimes(session_dir: Path) -> dict[str, int]:
    return {p.name: p.stat().st_mtime_ns for p in session_dir.iterdir() if p.is_file() and not p.name.startswith(("session.sqlite", "."))}


def record_counts(service: Any, name: str) -> dict[str, int]:
    return {
        "model_calls": len(service.records(name, "model_calls")),
        "tool_calls": len(service.records(name, "tool_calls")),
        "plans": len(service.records(name, "plans")),
        "outcomes": len(service.records(name, "outcomes")),
        "transcript": len(service.transcript(name)),
        "changes": len(service.changes(name)),
    }


def run_session(service: Any, name: str, implementation: str, *, scenario: str = "changing-trip",
                strategy: str = "reference", planner: str = "simulated", **kwargs: Any) -> str:
    service.create(name, implementation=implementation, scenario=scenario, strategy=strategy, planner=planner, **kwargs)
    service.run_to_completion(name)
    return name


# ======================================================================================
# Stub planners
# ======================================================================================


def assistant_entry(action: PlannerAction, call_id: str, note: str = "[test stub]") -> TranscriptEntry:
    return TranscriptEntry(turn=-1, role="assistant", content=note, tool_call=action_to_tool_call(action, call_id))


def respond(action: PlannerAction | None, call_id: str, *, errors: list[str] | None = None, usage: Any = None,
            latency_ms: float | None = None) -> PlannerResponse:
    entries = [assistant_entry(action, call_id)] if action is not None else []
    return PlannerResponse(action=action, action_call_id=call_id, usage=usage, latency_ms=latency_ms,
                           errors=errors or [], transcript_additions=entries)


class StubBase:
    mode = "simulated"

    def __init__(self) -> None:
        self.inputs: list[ModelInput] = []

    def settings(self) -> dict[str, Any]:
        return {"planner_mode": self.mode, "model": None, "note": "test stub; not a model"}

    def _capture(self, model_input: ModelInput) -> None:
        self.inputs.append(model_input.model_copy(deep=True))


class ScriptedPlanner(StubBase):
    """Returns queued actions in order, then ``then`` forever. A queued callable gets the ModelInput."""

    def __init__(self, actions: list[Any], then: PlannerAction | None = None, fail_with: list[str] | None = None):
        super().__init__()
        self.queue = list(actions)
        self.then = then
        self.fail_with = fail_with

    def next_action(self, model_input: ModelInput, call_id: str) -> PlannerResponse:
        self._capture(model_input)
        item = self.queue.pop(0) if self.queue else self.then
        if callable(item):
            item = item(model_input)
        if item is None:
            return respond(None, call_id, errors=self.fail_with or ["stub planner returned no action"])
        return respond(item, call_id)


class RecordingPlanner(StubBase):
    """Wraps the simulator, keeps the exact ModelInput it was handed and what it returned."""

    def __init__(self, inner: Any | None = None):
        super().__init__()
        self.inner = inner or SimulatedPlanner()
        self.mode = self.inner.mode
        self.responses: list[PlannerResponse] = []

    def settings(self) -> dict[str, Any]:
        return self.inner.settings()

    def next_action(self, model_input: ModelInput, call_id: str) -> PlannerResponse:
        self._capture(model_input)
        response = self.inner.next_action(model_input, call_id)
        self.responses.append(response.model_copy(deep=True))
        return response


class RewritePlanner(RecordingPlanner):
    """Runs the simulator but lets a test replace chosen actions.

    ``rewrite(index, model_input, response)`` returns a replacement action or None to keep the
    simulator's choice. ``index`` counts planner calls from zero.
    """

    def __init__(self, rewrite: Callable[[int, ModelInput, PlannerResponse], PlannerAction | None]):
        super().__init__()
        self.rewrite = rewrite

    def next_action(self, model_input: ModelInput, call_id: str) -> PlannerResponse:
        index = len(self.inputs)
        self._capture(model_input)
        response = self.inner.next_action(model_input, call_id)
        replacement = self.rewrite(index, model_input, response)
        if replacement is not None:
            response = respond(replacement, call_id)
        self.responses.append(response.model_copy(deep=True))
        return response


def install_planner(monkeypatch: Any, planner: Any) -> Any:
    """Make every WorkshopService operation use ``planner`` instead of the factory's choice."""
    monkeypatch.setattr("workshop.service.make_planner", lambda mode, provider=None: planner)
    return planner


def last_user_text(model_input: ModelInput) -> str:
    return model_input.messages[-1].content


# ======================================================================================
# Validator harness: a world, a change log, and tool access with no student code involved
# ======================================================================================


class Env:
    def __init__(self, scenario: str = "changing-trip") -> None:
        self.requirements = load_scenario(scenario)["initial_request"]["requirements"]
        self.world = World()
        self.changes: list[Change] = []
        self._seq = 0
        self._calls = 0
        self.toolbox = ToolBox(self.world, self._validate, lambda args: [])

    # -- authority and validation ---------------------------------------------------------
    def authority(self) -> Authority:
        return Authority.from_changes(self.requirements, self.changes, self.world)

    def _validate(self, fq: str, hq: str, claimed: int | None) -> dict[str, Any]:
        return validate_plan(self.world, self.authority(), fq, hq, claimed).model_dump(mode="json")

    def validate(self, fq: str, hq: str, claimed: int | None = None) -> ValidationResult:
        return validate_plan(self.world, self.authority(), fq, hq, claimed)

    # -- changes --------------------------------------------------------------------------
    def change(self, event: dict[str, Any], *, turn: int = 1, source: str = "user", change_id: str | None = None) -> Change:
        self._seq += 1
        c = Change.model_validate({"change_id": change_id or f"chg-{self._seq}", "seq": self._seq, "turn": turn, "source": source,
                                   "at": self.world.clock.now, "event": event})
        self.changes.append(c)
        return c

    def add_child(self) -> Change:
        return self.change({"type": "PartySizeChanged", "adults": 2, "children": 1})

    def accept_hotel(self, hotel_id: str, quote_id: str, proposal_id: str = "plan-002") -> Change:
        return self.change({"type": "HotelAccepted", "hotel_id": hotel_id, "proposal_id": proposal_id, "quote_id": quote_id})

    def ask_earlier(self, reference_arrival: str = "18:00", flight_id: str = "f1", proposal_id: str = "plan-002") -> Change:
        return self.change({"type": "EarlierFlightRequested", "reference_proposal_id": proposal_id, "reference_flight_id": flight_id,
                            "reference_arrival_local": reference_arrival})

    # -- tools ----------------------------------------------------------------------------
    def tool(self, name: str, **args: Any) -> dict[str, Any]:
        self._calls += 1
        return self.toolbox.execute(name, args, f"tc-{self._calls:04d}")

    def flights(self, adults: int, children: int, arrive_before: str | None = None) -> dict[str, str]:
        out = self.tool("search_flights", origin="RDU", destination="CHI", adults=adults, children=children,
                        arrive_before=arrive_before, **TRIP_DATES)
        assert out["ok"], out
        return {q["item_id"]: q["quote_id"] for q in out["quotes"]}

    def hotels(self, adults: int, children: int) -> dict[str, str]:
        out = self.tool("search_hotels", check_in=TRIP_DATES["depart_date"], check_out=TRIP_DATES["return_date"], adults=adults, children=children)
        assert out["ok"], out
        return {q["item_id"]: q["quote_id"] for q in out["quotes"]}

    def refresh(self, item_id: str, adults: int, children: int) -> str:
        out = self.tool("refresh_quote", item_id=item_id, adults=adults, children=children, **TRIP_DATES)
        assert out["ok"] and out["available"], out
        return out["quotes"][0]["quote_id"]

    def reprice(self, item_id: str, revision: int) -> list[str]:
        return self.world.apply_event({"type": "reprice", "item_id": item_id, "new_revision": revision})

    def quote(self, quote_id: str) -> Quote:
        return self.world.quotes[quote_id]


# ======================================================================================
# GUI client
# ======================================================================================


class Gui:
    """FastAPI TestClient with the browser's habits: a client header and an op ID per action."""

    def __init__(self, root: Path | str, port: int = 8765) -> None:
        from fastapi.testclient import TestClient

        from workshop.gui.app import create_app

        self.root = Path(root)
        self.client = TestClient(create_app(self.root, port))

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.client.get(path, **kwargs)

    def get_json(self, path: str) -> Any:
        r = self.client.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text)
        return r.json()

    def post(self, path: str, body: dict[str, Any] | None = None, *, op_id: str | None = None, expected: int | None = None,
             client_header: bool = True, headers: dict[str, str] | None = None) -> Any:
        payload = {**(body or {}), "op_id": op_id or f"op-{uuid.uuid4().hex[:10]}"}
        if expected is not None:
            payload["expected_version"] = expected
        hdrs = dict(headers or {})
        if client_header:
            hdrs["X-Workshop-Client"] = "gui"
        return self.client.post(path, json=payload, headers=hdrs)

    def ok(self, path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        r = self.post(path, body, **kwargs)
        assert r.status_code == 200, (path, r.status_code, r.text)
        return r.json()

    def create(self, name: str, implementation: str, **body: Any) -> dict[str, Any]:
        return self.ok("/api/sessions", {"session": name, "implementation": implementation, "strategy": "reference", **body})

    def status(self, name: str) -> dict[str, Any]:
        return self.get_json(f"/api/sessions/{name}")

    def records(self, name: str, kind: str) -> list[Any]:
        return self.get_json(f"/api/sessions/{name}/records/{kind}")

    def finish(self, name: str, mode: str = "turn") -> None:
        """Drive a session to completion the way the page's buttons would: run, turn by turn, or step by step."""
        st = self.status(name)
        if mode == "run":
            self.ok(f"/api/sessions/{name}/run", expected=st["cursor"]["version"])
            return
        while st["cursor"]["phase"] != "complete":
            version = st["cursor"]["version"]
            if st["can_apply_change"]:
                self.ok(f"/api/sessions/{name}/apply-change", expected=version)
            elif mode == "step":
                self.ok(f"/api/sessions/{name}/step", expected=version)
            else:
                self.ok(f"/api/sessions/{name}/run-turn", expected=version)
            st = self.status(name)


# ======================================================================================
# Extra scenarios
# ======================================================================================


def add_scenario(tmp_path: Path, monkeypatch: Any, name: str, mutate: Callable[[dict[str, Any]], None],
                 base: str = "initial") -> None:
    """Make an extra scenario file visible to the runner without touching the repo's fixtures."""
    import shutil

    import workshop.runner as runner

    source = Path(runner.FIXTURES) / "scenarios"
    target = tmp_path / "fixtures" / "scenarios"
    target.mkdir(parents=True, exist_ok=True)
    for path in source.glob("*.json"):
        shutil.copy(path, target / path.name)
    data = json.loads((source / f"{base}.json").read_text())
    data["name"] = name
    mutate(data)
    (target / f"{name}.json").write_text(json.dumps(data))
    monkeypatch.setattr(runner, "FIXTURES", tmp_path / "fixtures")


# ======================================================================================
# A fake student file
# ======================================================================================


def student_strategy(tmp_path: Path, source: str, name: str = "student") -> Any:
    """Load ``source`` from a file named exercises.py, so tracebacks look like a student's own."""
    import importlib.util

    from workshop.runner import Strategy

    folder = tmp_path / f"student_code_{abs(hash(source))}"
    folder.mkdir(exist_ok=True)
    path = folder / "exercises.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(f"student_exercises_{abs(hash(source))}", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return Strategy(name, module.apply_change, module.build_context, str(path))


REFERENCE_IMPORTS = "from solutions.reference import apply_change as _apply, build_context as _context\n"
