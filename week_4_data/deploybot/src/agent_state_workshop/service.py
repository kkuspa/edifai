"""Application services shared by the CLI and the GUI.

The CLI calls these functions directly. The optional GUI server calls the same
functions from its request handlers. Neither interface owns any domain logic.

Two kinds of entry point:

* Read-only queries (`session_status`, `list_runs`, `inspect_run`, ...) never
  modify a database, a run directory, or the operation log.
* Operations (`Workshop.execute`) change state. Each carries an operation ID.
  Submitting an ID that already exists returns the recorded operation instead
  of running it again, so double-clicks and request retries cannot duplicate
  work. Operations on one session database are serialized with a file lock,
  which also covers a CLI and a GUI acting on the same session at once.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import baseline, checks, evidence, runner
from .scenarios import SESSION_ID, followup, scenario_parts, FOLLOWUP_FILES, SCENARIO_FILES
from .store import EventStore, utc_now
from .telemetry import jaeger_trace_url, otlp_endpoint_from_env

SESSION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


class ServiceError(RuntimeError):
    """A user-facing error: bad arguments or a session in the wrong state."""


# ---------------------------------------------------------------- file lock


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """An exclusive, cross-process lock held for the duration of the block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        if os.name == "nt":  # pragma: no cover - exercised on Windows only
            import msvcrt
            import time

            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ----------------------------------------------------------- operation log


class OperationLog:
    """Operation records in .workshop/operations.sqlite (outside the browser)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        conn = self._connect()
        conn.execute(
            """CREATE TABLE IF NOT EXISTS operations (
                op_id TEXT PRIMARY KEY, kind TEXT NOT NULL, params TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, error TEXT, interface TEXT,
                pid INTEGER, created_at TEXT NOT NULL, finished_at TEXT)"""
        )
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        d["params"] = json.loads(d["params"])
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    def get(self, op_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            return self._row(conn.execute("SELECT * FROM operations WHERE op_id = ?", (op_id,)).fetchone())
        finally:
            conn.close()

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM operations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._row(r) for r in rows]  # type: ignore[misc]
        finally:
            conn.close()

    def submit(self, op_id: str, kind: str, params: dict[str, Any], interface: str) -> tuple[dict[str, Any], bool]:
        """Record a new operation as queued. Returns (record, created)."""
        conn = self._connect()
        try:
            try:
                conn.execute(
                    "INSERT INTO operations(op_id, kind, params, status, interface, pid, created_at) "
                    "VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                    (op_id, kind, json.dumps(params, sort_keys=True), interface, os.getpid(), utc_now()),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            record = self._row(conn.execute("SELECT * FROM operations WHERE op_id = ?", (op_id,)).fetchone())
        finally:
            conn.close()
        assert record is not None
        if not created and (record["kind"] != kind or record["params"] != json.loads(json.dumps(params, sort_keys=True))):
            raise ServiceError(f"operation {op_id} already exists with a different request")
        return record, created

    def claim(self, op_id: str) -> bool:
        """Move queued -> running. Only one caller can win."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE operations SET status = 'running', pid = ? WHERE op_id = ? AND status = 'queued'",
                (os.getpid(), op_id),
            )
            return cur.rowcount == 1
        finally:
            conn.close()

    def finish(self, op_id: str, status: str, result: Any = None, error: str | None = None) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE operations SET status = ?, result = ?, error = ?, finished_at = ? WHERE op_id = ?",
                (status, json.dumps(result, default=str) if result is not None else None, error, utc_now(), op_id),
            )
        finally:
            conn.close()

    def mark_interrupted(self) -> int:
        """Operations whose process has gone away are marked interrupted."""
        conn = self._connect()
        try:
            count = 0
            for row in conn.execute("SELECT op_id, pid FROM operations WHERE status IN ('queued', 'running')").fetchall():
                if row["pid"] == os.getpid() or not _pid_alive(row["pid"]):
                    conn.execute("UPDATE operations SET status = 'interrupted', finished_at = ? WHERE op_id = ?",
                                 (utc_now(), row["op_id"]))
                    count += 1
            return count
        finally:
            conn.close()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --------------------------------------------------------- session helpers


def prepare_database(db_path: Path, scenario: str = "stale-resume", reset: bool = False) -> dict[str, Any]:
    """Create a session database with a real process boundary at the pause.

    1. A child process appends events 1-20, saves its memory, and exits.
    2. This process waits for it, then appends events 21-40 (the world
       changing while the agent is stopped).
    """
    if scenario not in SCENARIO_FILES:
        raise ServiceError(f"unknown scenario {scenario!r}; choose from {', '.join(SCENARIO_FILES)}")
    db_path = Path(db_path)
    if db_path.exists():
        if not reset:
            raise ServiceError(f"{db_path} already exists. Pass --reset to discard it and start over.")
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [sys.executable, "-m", "agent_state_workshop.cli", "checkpoint-child", "--db", str(db_path),
         "--scenario", scenario],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise ServiceError(f"checkpoint child process failed:\n{proc.stdout}{proc.stderr}")
    child = json.loads(proc.stdout.strip().splitlines()[-1])

    _, during = scenario_parts(scenario)
    with EventStore(db_path) as store:
        store.append_many(during)
        store.set_meta("prepared", "1")
        store.set_meta("prepared_at", utc_now())
        store.set_meta("resumer_pid", str(os.getpid()))
        last = store.last_sequence(SESSION_ID)
    return {
        "db_path": str(db_path),
        "scenario": scenario,
        "checkpoint_pid": child["pid"],
        "paused_at_sequence": child["through_sequence"],
        "appended_while_paused": len(during),
        "through_sequence": last,
        "this_pid": os.getpid(),
    }


def checkpoint_child(db_path: Path, scenario: str) -> dict[str, Any]:
    """Runs in the child process: the agent's session up to and including the pause."""
    before, _ = scenario_parts(scenario)
    with EventStore(db_path) as store:
        store.set_meta("session_id", SESSION_ID)
        store.set_meta("scenario", scenario)
        store.append_many(before)  # events 1-20, ending with session.paused
        summary = baseline.summarize_at_pause(store.events(SESSION_ID))
        store.save_memory(SESSION_ID, baseline.SUMMARY_KIND, summary["through_sequence"], summary)
        store.set_meta("checkpoint_pid", str(os.getpid()))
    return {"pid": os.getpid(), "through_sequence": summary["through_sequence"]}


def append_fixture(db_path: Path, name: str) -> dict[str, Any]:
    if name not in FOLLOWUP_FILES:
        raise ServiceError(f"unknown fixture {name!r}; choose from {', '.join(FOLLOWUP_FILES)}")
    db_path = _require_db(db_path)
    try:
        with EventStore(db_path) as store:
            results = store.append_many(followup(name))
            last = store.last_sequence(SESSION_ID)
    except ValueError as exc:  # sequence or conflict errors
        raise ServiceError(str(exc)) from exc
    return {
        "fixture": name,
        "appended": [r.event_id for r in results if r.appended],
        "already_present": [r.event_id for r in results if not r.appended],
        "through_sequence": last,
    }


def _require_db(db_path: Path) -> Path:
    db_path = Path(db_path)
    if not db_path.exists():
        raise ServiceError(f"{db_path} does not exist. Create it with `workshop prepare --db {db_path}`.")
    return db_path


def session_status(db_path: Path) -> dict[str, Any]:
    """Read-only summary of one session database."""
    db_path = _require_db(db_path)
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        events = [json.loads(r["body"]) for r in conn.execute("SELECT body FROM events ORDER BY sequence")]
        runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY started_at")]
        memory = conn.execute("SELECT kind, through_sequence, writer_pid, saved_at FROM memories").fetchall()
        snapshots = conn.execute(
            "SELECT strategy, projection_version, through_sequence, state_hash, saved_at FROM snapshots"
        ).fetchall()
        receipts = [json.loads(r["body"]) for r in conn.execute("SELECT body FROM receipts")]
    finally:
        conn.close()
    ids = {e["event_id"] for e in events}
    applied = [n for n, _ in FOLLOWUP_FILES.items() if {e.event_id for e in followup(n)} <= ids]
    active = next((r for r in reversed(runs) if r["status"] == "active"), None)
    return {
        "db_path": str(db_path),
        "name": db_path.stem,
        "meta": meta,
        "through_sequence": events[-1]["sequence"] if events else 0,
        "event_count": len(events),
        "followups_applied": applied,
        "followups_available": [n for n in FOLLOWUP_FILES if n not in applied],
        "active_run": active,
        "runs": runs,
        "memories": [dict(m) for m in memory],
        "snapshots": [dict(s) for s in snapshots],
        "receipts": receipts,
    }


def session_events(db_path: Path, since: int = 0) -> list[dict[str, Any]]:
    status_db = _require_db(db_path)
    conn = sqlite3.connect(f"file:{status_db}?mode=ro", uri=True)
    try:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT body FROM events WHERE sequence > ? ORDER BY sequence", (since,))]
    finally:
        conn.close()


def run_summary(run_dir: Path, stages: list[runner.StageResult] | None = None) -> dict[str, Any]:
    """What both interfaces print after an operation that touched a run."""
    view = evidence.load_run(run_dir)
    progress = view.progress
    out = {
        "run_id": progress.get("run_id"),
        "run_dir": str(run_dir),
        "status": progress.get("status"),
        "strategy": progress.get("strategy"),
        "planner_mode": progress.get("planner_mode"),
        "target_environment": progress.get("target_environment"),
        "trace_id": progress.get("trace_id"),
        "next_stage": progress.get("next_stage"),
        "executed_stages": [asdict_stage(s) for s in stages] if stages else [],
        "proposal": view.artifacts.get("proposal.json"),
        "gate": view.artifacts.get("gate.json"),
        "receipt": view.artifacts.get("receipt.json"),
        "executed_action": "deploy" if view.artifacts.get("receipt.json") else "none",
        "error": progress.get("error"),
        "exercise_incomplete": progress.get("exercise_incomplete", False),
        "annotation": progress.get("annotation"),
    }
    if progress.get("otlp_endpoint") and progress.get("status") in ("completed", "failed"):
        out["jaeger_url"] = jaeger_trace_url(progress["trace_id"])
    return out


def asdict_stage(s: runner.StageResult) -> dict[str, Any]:
    return {"stage": s.stage, "status": s.status, "span_id": s.span_id, "duration_ms": s.duration_ms,
            "summary": s.summary, "error": s.error}


# ------------------------------------------------------------------ facade


class Workshop:
    """Workspace-scoped services. `root` holds .workshop/, runs/, submissions/."""

    def __init__(self, root: Path | None = None, interface: str = "cli"):
        self.root = Path(root or Path.cwd()).resolve()
        self.interface = interface
        self.ops = OperationLog(self.root / ".workshop" / "operations.sqlite")

    # -------------------------------------------------------- paths & lists

    def db_for(self, session: str) -> Path:
        if not SESSION_NAME_RE.match(session):
            raise ServiceError("session names use lowercase letters, digits, '-' and '_' (max 41 chars)")
        return self.root / ".workshop" / f"{session}.sqlite"

    def resolve(self, path: str | Path) -> Path:
        p = Path(path)
        return p if p.is_absolute() else (self.root / p)

    def list_sessions(self) -> list[dict[str, Any]]:
        out = []
        for db in sorted((self.root / ".workshop").glob("*.sqlite")):
            if db.name == "operations.sqlite":
                continue
            try:
                st = session_status(db)
                out.append({"name": db.stem, "db_path": str(db.relative_to(self.root)),
                            "through_sequence": st["through_sequence"], "followups_applied": st["followups_applied"],
                            "active_run": st["active_run"]["run_id"] if st["active_run"] else None,
                            "run_count": len(st["runs"])})
            except (sqlite3.Error, ServiceError) as exc:
                out.append({"name": db.stem, "db_path": str(db), "error": str(exc)})
        return out

    def list_runs(self) -> list[dict[str, Any]]:
        out = []
        runs_dir = self.root / "runs"
        for progress_file in sorted(runs_dir.glob("**/progress.json")):
            p = runner.read_json(progress_file, {}) or {}
            gate = runner.read_json(progress_file.parent / "gate.json") or {}
            proposal = runner.read_json(progress_file.parent / "proposal.json") or {}
            out.append({
                "run_dir": str(progress_file.parent.relative_to(self.root)),
                "run_id": p.get("run_id"), "status": p.get("status"), "strategy": p.get("strategy"),
                "planner_mode": p.get("planner_mode"), "session": Path(p.get("db_path", "")).stem,
                "target_environment": p.get("target_environment"),
                "through_sequence": (p.get("source") or {}).get("through_sequence"),
                "next_stage": p.get("next_stage"), "started_at": p.get("started_at"),
                "proposed_action": proposal.get("action"), "gate": gate.get("outcome"),
                "executed_action": "deploy" if (progress_file.parent / "receipt.json").exists() else "none",
            })
        out.sort(key=lambda r: r.get("started_at") or "")
        return out

    def allocate_run_dir(self, session: str, strategy: str) -> Path:
        base = self.root / "runs"
        n = 1
        while (base / f"{session}-{strategy}-{n:02d}").exists():
            n += 1
        return base / f"{session}-{strategy}-{n:02d}"

    # ---------------------------------------------------------- operations

    def execute(self, kind: str, params: dict[str, Any], op_id: str | None = None) -> dict[str, Any]:
        """Submit and run an operation in this thread. Idempotent per op_id."""
        record = self.submit(kind, params, op_id)
        if record["status"] == "queued":
            self.run_operation(record["op_id"])
            record = self.ops.get(record["op_id"]) or record
        return record

    def submit(self, kind: str, params: dict[str, Any], op_id: str | None = None) -> dict[str, Any]:
        if kind not in OPERATIONS:
            raise ServiceError(f"unknown operation {kind!r}")
        record, _created = self.ops.submit(op_id or f"op-{uuid.uuid4()}", kind, params, self.interface)
        return record

    def run_operation(self, op_id: str) -> None:
        record = self.ops.get(op_id)
        if record is None or not self.ops.claim(op_id):
            return  # another worker already ran or is running it
        handler, lock_key = OPERATIONS[record["kind"]]
        lock_path = self._lock_path(lock_key(self, record["params"]))
        try:
            with file_lock(lock_path):
                result = handler(self, **record["params"])
            self.ops.finish(op_id, "done", result=result)
        except (ServiceError, runner.RunError) as exc:
            self.ops.finish(op_id, "failed", error=str(exc))
        except Exception as exc:  # unexpected: keep the message, never crash the server
            self.ops.finish(op_id, "failed", error=f"{type(exc).__name__}: {exc}")

    def _lock_path(self, key: Path | None) -> Path:
        if key is None:
            return self.root / ".workshop" / "workspace.lock"
        return Path(str(key) + ".lock")

    # ----------------------------------------------------------- handlers

    def op_prepare(self, db: str, scenario: str = "stale-resume", reset: bool = False) -> dict[str, Any]:
        return prepare_database(self.resolve(db), scenario, reset)

    def op_append(self, db: str, fixture: str) -> dict[str, Any]:
        return append_fixture(self.resolve(db), fixture)

    def op_run(self, db: str, strategy: str, planner: str = "simulated", provider: str | None = None,
               model: str | None = None, target_environment: str = "staging", out: str | None = None,
               replace: bool = False, until_done: bool = True, otlp_endpoint: str | None = None) -> dict[str, Any]:
        """Start a new run, then execute one stage or all of them."""
        db_path = _require_db(self.resolve(db))
        run_dir = self.resolve(out) if out else self.allocate_run_dir(db_path.stem, strategy)
        endpoint = otlp_endpoint if otlp_endpoint is not None else otlp_endpoint_from_env()
        spec = runner.RunSpec(db_path, run_dir, strategy, planner, provider, model, target_environment,
                              endpoint, replace)
        progress = runner.start_run(spec)
        stages = runner.advance(db_path, progress["run_id"], until_done=until_done)
        return run_summary(run_dir, stages)

    def op_step(self, db: str, until_done: bool = False, **start: Any) -> dict[str, Any]:
        """Advance the active run; if there is none, start one with `start` options."""
        db_path = _require_db(self.resolve(db))
        with EventStore(db_path) as store:
            active = store.active_run(store.get_meta("session_id") or SESSION_ID)
        if active is None:
            if not start.get("strategy"):
                raise ServiceError("no run in progress. Pass --strategy (and --planner) to start one.")
            return self.op_run(db, until_done=until_done, **start)
        stages = runner.advance(db_path, active["run_id"], until_done=until_done)
        return run_summary(Path(active["run_dir"]), stages)

    def op_abandon(self, db: str) -> dict[str, Any]:
        result = runner.abandon(_require_db(self.resolve(db)))
        return result or {"abandoned": None, "message": "no run was in progress"}

    def op_check(self, strategy: str = "student") -> dict[str, Any]:
        results = checks.run_checks(strategy)
        return {"strategy": strategy, "summary": checks.summarize(results),
                "results": [r.to_dict() for r in results]}

    def op_export(self, before: str, after: str, followups: list[str] | None = None,
                  output: str = "submissions") -> dict[str, Any]:
        return evidence.export_evidence(self.resolve(before), self.resolve(after),
                                        [self.resolve(f) for f in (followups or [])], self.resolve(output),
                                        root=self.root)

    def op_visualize(self, before: str, after: str, output: str = "submissions/comparison.html") -> dict[str, Any]:
        from .visualize import render_comparison

        out = self.resolve(output)
        render_comparison(self.resolve(before), self.resolve(after), out)
        return {"output": str(out)}


def _db_key(ws: Workshop, params: dict[str, Any]) -> Path:
    return ws.resolve(params["db"])


def _workspace_key(ws: Workshop, params: dict[str, Any]) -> None:
    return None


OPERATIONS: dict[str, tuple[Callable[..., Any], Callable[[Workshop, dict[str, Any]], Path | None]]] = {
    "prepare": (Workshop.op_prepare, _db_key),
    "reset": (lambda ws, db, scenario="stale-resume": ws.op_prepare(db, scenario, reset=True), _db_key),
    "append": (Workshop.op_append, _db_key),
    "run": (Workshop.op_run, _db_key),
    "step": (Workshop.op_step, _db_key),
    "abandon": (Workshop.op_abandon, _db_key),
    "check": (Workshop.op_check, _workspace_key),
    "export": (Workshop.op_export, _workspace_key),
    "visualize": (Workshop.op_visualize, _workspace_key),
}
