"""Application services shared by the CLI and the GUI.

The CLI calls these functions directly; the GUI's FastAPI routes call the same functions.
Every mutating operation:

* runs under a per-session lock (a thread lock plus an OS file lock, so the CLI and the GUI
  serialize against each other),
* carries an operation ID; a repeated ID returns the stored result without re-executing,
* may carry ``expected_version``; a stale version is refused instead of executed twice,
* commits engine state, records, and the operation result in one SQLite transaction,
* re-exports the session's artifacts afterwards.

Read operations never take the lock and never change anything.
"""

from __future__ import annotations

import fcntl
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .context import quote_view
from .models import ObservedQuote, QuoteStatus, dollars
from .planners import live_provider_status, make_planner
from .report import compare_runs, export_artifacts, json_diff, run_summary
import traceback

from .runner import Cursor, Engine, EngineError, SessionConfig, list_scenarios, load_scenario, load_strategy
from .store import RunStore

DEFAULT_ROOT = Path("runs")
SESSION_DB = "session.sqlite"


class ServiceError(Exception):
    status = 400


class SessionNotFound(ServiceError):
    status = 404


class SessionExists(ServiceError):
    status = 409


class VersionConflict(ServiceError):
    status = 409


class InvalidOperation(ServiceError):
    status = 409


class StudentCodeError(ServiceError):
    """The exercise functions raised. Nothing was committed; the session is unchanged."""

    status = 422


STUDENT_FILES = ("exercises.py", "reference.py")

# Files a session directory owns. Overwrite and reset delete only these, never anything else.
SESSION_FILES = (
    SESSION_DB, f"{SESSION_DB}-wal", f"{SESSION_DB}-shm", "manifest.json", "changes.jsonl", "state_snapshots.jsonl",
    "transcript.json", "model_calls.jsonl", "tool_calls.jsonl", "plans.jsonl", "checks.json", "report.html",
)


def student_location(exc: BaseException) -> str | None:
    """Where in the exercise file an exception came from, or None if it did not."""
    if isinstance(exc, SyntaxError) and (exc.filename or "").endswith(STUDENT_FILES):
        return f"{Path(exc.filename or '').name}:{exc.lineno}"
    frames = traceback.extract_tb(exc.__traceback__)
    student = [f for f in frames if f.filename.endswith(STUDENT_FILES)]
    if student:
        return f"{Path(student[-1].filename).name}:{student[-1].lineno} in {student[-1].name}"
    return None


def _raise_from_student(exc: BaseException) -> None:
    where = student_location(exc)
    if where:
        raise StudentCodeError(f"Your exercise code raised {type(exc).__name__}: {exc} ({where}). Nothing was saved; fix it and retry.") from exc


def _load_strategy(name: str) -> Any:
    try:
        return load_strategy(name)
    except Exception as exc:
        _raise_from_student(exc)
        raise


_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _thread_lock(key: str) -> threading.Lock:
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.Lock())


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.mkdir(parents=True, exist_ok=True)
    tlock = _thread_lock(str(path.resolve()))
    with tlock:
        with open(path / ".lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def new_op_id() -> str:
    return f"op-{uuid.uuid4().hex[:12]}"


class WorkshopService:
    def __init__(self, root: Path | str = DEFAULT_ROOT):
        self.root = Path(root)

    # -- addressing -------------------------------------------------------------------

    def path(self, session: str | Path) -> Path:
        p = Path(session)
        if p.is_absolute() or len(p.parts) > 1:
            return p
        return self.root / p

    def exists(self, session: str | Path) -> bool:
        return (self.path(session) / SESSION_DB).exists()

    def _store(self, session: str | Path) -> RunStore:
        p = self.path(session)
        if not (p / SESSION_DB).exists():
            raise SessionNotFound(f"No session at {p}")
        return RunStore(p / SESSION_DB)

    def _config(self, store: RunStore) -> SessionConfig:
        return SessionConfig.model_validate(store.get("config"))

    # -- lookups ----------------------------------------------------------------------

    def options(self) -> dict[str, Any]:
        return {
            "implementations": ["one-shot", "transcript", "state"],
            "planners": ["simulated", "live"],
            "strategies": ["student", "reference"],
            "scenarios": list_scenarios(),
            "live_providers_configured": live_provider_status(),
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        out = []
        for p in sorted(self.root.iterdir()):
            if (p / SESSION_DB).exists():
                store = RunStore(p / SESSION_DB)
                try:
                    cfg, cur = store.get("config"), store.get("cursor")
                    out.append({"session": p.name, "path": str(p), "config": cfg, "phase": (cur or {}).get("phase"), "version": (cur or {}).get("version")})
                finally:
                    store.close()
        return out

    # -- mutating operations --------------------------------------------------------------

    def _run_op(
        self,
        session: str | Path,
        kind: str,
        op_id: str | None,
        expected_version: int | None,
        body: Callable[[Engine], Any],
        needs_strategy: bool = True,
    ) -> dict[str, Any]:
        op_id = op_id or new_op_id()
        p = self.path(session)
        with _locked(p):
            store = self._store(p)
            try:
                prior = store.operation(op_id)
                if prior is not None:
                    return {**prior["result"], "op_id": op_id, "replayed": True}
                config = self._config(store)
                cursor = Cursor.model_validate(store.get("cursor"))
                if expected_version is not None and expected_version != cursor.version:
                    raise VersionConflict(
                        f"Session is at version {cursor.version}, request expected {expected_version}. Refresh and retry."
                    )
                planner = make_planner(config.planner, config.provider)
                strategy = _load_strategy(config.strategy) if (needs_strategy and config.implementation == "state") else None
                with store:
                    engine = Engine(store, config, planner, strategy)
                    try:
                        detail = body(engine)
                    except EngineError as exc:
                        raise InvalidOperation(str(exc)) from exc
                    except Exception as exc:
                        _raise_from_student(exc)
                        raise
                    engine.cursor.version += 1
                    engine.save()
                    result = {"ok": True, "kind": kind, "session": p.name, "version": engine.cursor.version,
                              "phase": engine.cursor.phase, "detail": detail}
                    store.save_operation(op_id, kind, result)
                export_artifacts(p)
                return {**result, "op_id": op_id, "replayed": False}
            finally:
                store.close()

    def create(
        self,
        session: str | Path,
        implementation: str,
        scenario: str = "changing-trip",
        planner: str = "simulated",
        provider: str | None = None,
        strategy: str = "student",
        restated_brief: bool = False,
        op_id: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        op_id = op_id or new_op_id()
        p = self.path(session)
        load_scenario(scenario)  # fail fast on a bad name
        if p.exists() and not (p / SESSION_DB).exists() and any(x for x in p.iterdir() if x.name != ".lock"):
            raise SessionExists(f"{p} exists, is not empty, and is not a session directory. Choose another name or --out.")
        if (p / SESSION_DB).exists() and not overwrite:
            store = RunStore(p / SESSION_DB)
            try:
                prior = store.operation(op_id)
            finally:
                store.close()
            if prior is not None:
                return {**prior["result"], "op_id": op_id, "replayed": True}
            raise SessionExists(f"{p} already holds a session. Use reset, choose another name, or pass --overwrite.")
        # Build the planner and import the exercise code before touching the directory.
        planner_obj = make_planner(planner, provider)
        strat = _load_strategy(strategy) if implementation == "state" else None
        with _locked(p):
            if (p / SESSION_DB).exists():
                store = RunStore(p / SESSION_DB)
                try:
                    prior = store.operation(op_id)
                finally:
                    store.close()
                if prior is not None:
                    return {**prior["result"], "op_id": op_id, "replayed": True}
                if not overwrite:
                    raise SessionExists(f"{p} already holds a session. Use reset, choose another name, or pass --overwrite.")
            return self._create_locked(p, implementation, scenario, planner, strategy, restated_brief, op_id, "create", planner_obj, strat)

    @staticmethod
    def _wipe_session_files(p: Path) -> None:
        for name in SESSION_FILES:
            target = p / name
            if target.exists():
                target.unlink()

    def _create_locked(self, p: Path, implementation: str, scenario: str, planner: str, strategy: str, restated_brief: bool,
                       op_id: str, kind: str, planner_obj: Any, strat: Any) -> dict[str, Any]:
        self._wipe_session_files(p)
        config = SessionConfig(
            session_id=p.name, run_id=f"run-{uuid.uuid4().hex[:8]}", implementation=implementation,  # type: ignore[arg-type]
            scenario=scenario, planner=planner, provider=getattr(planner_obj, "provider", None) if planner == "live" else None,  # type: ignore[arg-type]
            strategy=strategy, restated_brief=restated_brief, planner_settings=planner_obj.settings(),  # type: ignore[arg-type]
        )
        store = RunStore(p / SESSION_DB)
        try:
            with store:
                engine = Engine(store, config, planner_obj, strat)
                try:
                    detail = engine.start()
                except Exception as exc:
                    _raise_from_student(exc)
                    raise
                engine.cursor.version = 1
                engine.save()
                result = {"ok": True, "kind": kind, "session": p.name, "version": 1, "phase": engine.cursor.phase, "detail": detail}
                store.save_operation(op_id, kind, result)
        except BaseException:
            store.close()
            self._wipe_session_files(p)  # never leave a half-created session behind
            raise
        store.close()
        export_artifacts(p)
        return {**result, "op_id": op_id, "replayed": False}

    def reset(self, session: str | Path, op_id: str | None = None, expected_version: int | None = None) -> dict[str, Any]:
        """Discard the session's progress and start it again with the same settings."""
        op_id = op_id or new_op_id()
        p = self.path(session)
        with _locked(p):
            store = self._store(p)
            try:
                prior = store.operation(op_id)
                config = self._config(store)
                version = Cursor.model_validate(store.get("cursor")).version
            finally:
                store.close()
            if prior is not None:
                return {**prior["result"], "op_id": op_id, "replayed": True}
            if expected_version is not None and expected_version != version:
                raise VersionConflict(f"Session is at version {version}, request expected {expected_version}. Refresh before resetting.")
            planner_obj = make_planner(config.planner, config.provider)
            strat = _load_strategy(config.strategy) if config.implementation == "state" else None
            return self._create_locked(p, config.implementation, config.scenario, config.planner, config.strategy,
                                       config.restated_brief, op_id, "reset", planner_obj, strat)

    def apply_change(self, session: str | Path, op_id: str | None = None, expected_version: int | None = None) -> dict[str, Any]:
        return self._run_op(session, "apply_change", op_id, expected_version, lambda e: e.apply_next_change())

    def step(self, session: str | Path, op_id: str | None = None, expected_version: int | None = None) -> dict[str, Any]:
        return self._run_op(session, "step", op_id, expected_version, lambda e: e.step())

    def run_turn(self, session: str | Path, op_id: str | None = None, expected_version: int | None = None) -> dict[str, Any]:
        return self._run_op(session, "run_turn", op_id, expected_version, lambda e: e.run_turn())

    def run_to_completion(self, session: str | Path, op_id: str | None = None, expected_version: int | None = None) -> dict[str, Any]:
        return self._run_op(session, "run_to_completion", op_id, expected_version, lambda e: e.run_to_completion())

    def advance(self, session: str | Path, op_id: str | None = None, expected_version: int | None = None,
                pause: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Apply the next change if one is due, then run that turn (used by CLI step mode)."""
        def body(e: Engine) -> Any:
            out = []
            if e.cursor.phase == "awaiting_change":
                applied = e.apply_next_change()
                out.append({"result": "change_applied", **applied})
                if pause:
                    pause(applied)
            out.extend(e.run_turn())
            return out
        return self._run_op(session, "advance", op_id, expected_version, body)

    # -- read-only views ------------------------------------------------------------------

    def status(self, session: str | Path) -> dict[str, Any]:
        store = self._store(session)
        try:
            with store.read():
                return self._status(store)
        finally:
            store.close()

    def _status(self, store: RunStore) -> dict[str, Any]:
        config = self._config(store)
        cursor = Cursor.model_validate(store.get("cursor"))
        scenario = load_scenario(config.scenario)
        outcomes = store.records("outcomes")
        plans = store.records("plans")
        state = store.load_state()
        from .tools import World

        world = World.from_json(store.get("world"))
        next_change = scenario["turns"][cursor.next_turn_index] if cursor.next_turn_index < len(scenario["turns"]) else None
        conversation = self._conversation(store, outcomes, plans)
        world_quotes = [
            {"quote_id": q.quote_id, "item_id": q.item_id, "kind": q.item_kind, "party": f"{q.scope.adults}a{q.scope.children}c",
             "total_cents": q.total_cents, "total": dollars(q.total_cents), "revision": q.inventory_revision,
             "valid_now": not world.quote_expired(q)}
            for q in world.quotes.values()
        ]
        return {
            "session": Path(store.path).parent.name,
            "path": str(Path(store.path).parent),
            "config": config.model_dump(mode="json"),
            "cursor": cursor.model_dump(mode="json", exclude={"displayed_proposal"}),
            "clock": world.clock.now.isoformat(),
            "scenario": {
                "name": scenario["name"], "description": scenario["description"],
                "turns": [{"turn": t["turn"], "source": t["source"], "text": t["text"],
                           "change_types": [c["type"] for c in t["changes"]], "applied": i < cursor.next_turn_index,
                           "next": i == cursor.next_turn_index and cursor.phase == "awaiting_change"}
                          for i, t in enumerate(scenario["turns"])],
            },
            "next_change": next_change,
            "can_apply_change": cursor.phase == "awaiting_change",
            "can_step": cursor.phase == "ready",
            "outcomes": outcomes,
            "conversation": conversation,
            "displayed_proposal": cursor.displayed_proposal.model_dump(mode="json") if cursor.displayed_proposal else None,
            "state_summary": self._state_summary(state) if state else None,
            "world_quotes": world_quotes,
            "counts": {"model_calls": len(store.records("model_calls")), "tool_calls": len(store.records("tool_calls")),
                       "snapshots": len(store.snapshots()), "changes": len(store.changes())},
        }

    @staticmethod
    def _conversation(store: RunStore, outcomes: list[dict[str, Any]], plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        shown_inputs: set[tuple[int, str]] = set()
        by_turn: dict[int, list[dict[str, Any]]] = {}
        for o in outcomes:
            by_turn.setdefault(o["turn"], []).append(o)
        displayed = {p["proposal"]["proposal_id"]: p["proposal"] for p in plans if p.get("displayed")}
        transcript = store.transcript()
        turns = sorted({e.turn for e in transcript} | set(by_turn))
        for turn in turns:
            for e in transcript:
                if e.turn != turn or e.role not in ("user", "notification"):
                    continue
                n = e.normalized or {}
                if "display" not in n or (turn, n["display"]) in shown_inputs:
                    continue
                shown_inputs.add((turn, n["display"]))
                items.append({"turn": turn, "speaker": "user" if e.role == "user" else "notification", "text": n["display"]})
            for o in by_turn.get(turn, []):
                item = {"turn": turn, "speaker": "assistant", "outcome": o["kind"]}
                if o["kind"] == "plan_presented":
                    item["proposal"] = displayed.get(o["proposal_id"])
                elif o["kind"] == "question_asked":
                    item["text"] = o["question"]
                else:
                    item["text"] = o.get("reason")
                items.append(item)
        return items

    @staticmethod
    def _state_summary(state: Any) -> dict[str, Any]:
        def qv(oq: ObservedQuote) -> dict[str, Any]:
            return quote_view(oq)
        return {
            "revision": state.revision,
            "requirements_revision": state.requirements.revision,
            "party": state.requirements.party.label() if state.requirements.party else None,
            "earlier_arrival": state.requirements.earlier_arrival.model_dump(mode="json") if state.requirements.earlier_arrival else None,
            "decisions": state.decisions.model_dump(mode="json"),
            "current_quotes": [qv(oq) for oq in state.quotes.values() if oq.status == QuoteStatus.CURRENT],
            "invalidated_quotes": [qv(oq) for oq in state.quotes.values() if oq.status != QuoteStatus.CURRENT],
            "unsuitable_items": {k: v.model_dump(mode="json") for k, v in state.unsuitable_items.items()},
            "proposal": state.proposal.model_dump(mode="json") if state.proposal else None,
            "pending": [t.model_dump(mode="json") for t in state.pending],
            "open_question": state.open_question,
        }

    def state(self, session: str | Path, snapshot: int | None = None) -> dict[str, Any]:
        store = self._store(session)
        try:
            snaps = store.snapshots()
            if not snaps:
                return {"available": False, "reason": "This implementation keeps no structured task state. Inspect the transcript instead."}
            if snapshot is None:
                return {"available": True, "index": snaps[-1]["index"], "change_id": snaps[-1]["change_id"], "state": snaps[-1]["after"],
                        "snapshots": [{"index": s["index"], "change_id": s["change_id"]} for s in snaps]}
            match = next((s for s in snaps if s["index"] == snapshot), None)
            if match is None:
                raise SessionNotFound(f"No snapshot {snapshot}")
            return {"available": True, "index": match["index"], "change_id": match["change_id"], "state": match["after"],
                    "snapshots": [{"index": s["index"], "change_id": s["change_id"]} for s in snaps]}
        finally:
            store.close()

    def diff(self, session: str | Path, snapshot: int | None = None) -> dict[str, Any]:
        store = self._store(session)
        try:
            snaps = store.snapshots()
            if not snaps:
                return {"available": False, "reason": "No structured state in this implementation."}
            match = snaps[-1] if snapshot is None else next((s for s in snaps if s["index"] == snapshot), None)
            if match is None:
                raise SessionNotFound(f"No snapshot {snapshot}")
            return {"available": True, "index": match["index"], "change_id": match["change_id"],
                    "changes": json_diff(match["before"] or {}, match["after"])}
        finally:
            store.close()

    def records(self, session: str | Path, kind: str) -> list[dict[str, Any]]:
        allowed = {"model_calls", "tool_calls", "plans", "validations", "outcomes", "notes"}
        if kind not in allowed:
            raise ServiceError(f"Unknown record kind {kind!r}")
        store = self._store(session)
        try:
            return store.records(kind)
        finally:
            store.close()

    def transcript(self, session: str | Path) -> list[dict[str, Any]]:
        store = self._store(session)
        try:
            return [e.model_dump(mode="json") for e in store.transcript()]
        finally:
            store.close()

    def changes(self, session: str | Path) -> list[dict[str, Any]]:
        store = self._store(session)
        try:
            return [c.model_dump(mode="json") for c in store.changes()]
        finally:
            store.close()

    def summary(self, session: str | Path) -> dict[str, Any]:
        return run_summary(self.path(session))

    # -- exports, comparison, checks ------------------------------------------------------

    def export(self, session: str | Path) -> dict[str, Any]:
        p = self.path(session)
        with _locked(p):
            self._store(p).close()
            files = export_artifacts(p)
        return {"ok": True, "session": p.name, "files": files}

    def _global_op(self, op_id: str | None, kind: str, body: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        op_id = op_id or new_op_id()
        self.root.mkdir(parents=True, exist_ok=True)
        with _locked(self.root / ".service"):
            store = RunStore(self.root / ".service" / "operations.sqlite")
            try:
                prior = store.operation(op_id)
                if prior is not None:
                    return {**prior["result"], "op_id": op_id, "replayed": True}
                result = body()
                with store:
                    store.save_operation(op_id, kind, result)
                return {**result, "op_id": op_id, "replayed": False}
            finally:
                store.close()

    def compare(self, sessions: list[str | Path], output: str | Path, op_id: str | None = None) -> dict[str, Any]:
        paths = [self.path(s) for s in sessions]
        for p in paths:
            if not (p / SESSION_DB).exists():
                raise SessionNotFound(f"No session at {p}")

        def body() -> dict[str, Any]:
            out = compare_runs(paths, Path(output))
            return {"ok": True, "kind": "compare", "output": str(out), "runs": [str(p) for p in paths]}
        return self._global_op(op_id, "compare", body)

    def compare_data(self, sessions: list[str | Path]) -> list[dict[str, Any]]:
        """Run summaries for the GUI comparison, without the bulky per-step inputs.

        Exact model inputs stay one click away in the inspector and in report.html.
        """
        out = []
        for s in sessions:
            summary = run_summary(self.path(s))
            for turn in summary["turns"]:
                for step in turn["steps"]:
                    step.pop("input", None)
                    step.pop("tool_result", None)
            summary["state_timeline"] = [{"index": t["index"], "change_id": t["change_id"], "leaf_changes": len(t["diff"])}
                                         for t in summary["state_timeline"]]
            out.append(summary)
        return out

    def check(self, strategy: str = "student", output: str | Path | None = "submissions/check-results.json", op_id: str | None = None) -> dict[str, Any]:
        from .checks import run_checks

        def body() -> dict[str, Any]:
            return {"ok": True, "kind": "check", "report": run_checks(strategy, Path(output) if output else None)}
        return self._global_op(op_id, "check", body)
