"""SQLite persistence: the append-only event history plus derived data.

The `events` table is the authoritative record. Everything else here
(snapshots, the baseline's narrative memory, run records, deployment
receipts) is derived or operational data that can be discarded and rebuilt
or re-created without losing business facts.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Event, WorkingState, canonical_json

STORE_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    session_id TEXT    NOT NULL,
    sequence   INTEGER NOT NULL,
    event_id   TEXT    NOT NULL,
    type       TEXT    NOT NULL,
    body       TEXT    NOT NULL,
    PRIMARY KEY (session_id, event_id),
    UNIQUE (session_id, sequence)
);
CREATE TABLE IF NOT EXISTS snapshots (
    session_id         TEXT    NOT NULL,
    strategy           TEXT    NOT NULL,
    projection_version INTEGER NOT NULL,
    through_sequence   INTEGER NOT NULL,
    state_hash         TEXT    NOT NULL,
    state_json         TEXT    NOT NULL,
    saved_at           TEXT    NOT NULL,
    PRIMARY KEY (session_id, strategy)
);
CREATE TABLE IF NOT EXISTS memories (
    session_id       TEXT    NOT NULL,
    kind             TEXT    NOT NULL,
    through_sequence INTEGER NOT NULL,
    body             TEXT    NOT NULL,
    writer_pid       INTEGER NOT NULL,
    saved_at         TEXT    NOT NULL,
    PRIMARY KEY (session_id, kind)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    run_dir            TEXT NOT NULL,
    strategy           TEXT NOT NULL,
    planner_mode       TEXT NOT NULL,
    provider           TEXT,
    target_environment TEXT NOT NULL,
    trace_id           TEXT NOT NULL,
    root_span_id       TEXT NOT NULL,
    status             TEXT NOT NULL,
    next_stage         TEXT,
    started_at         TEXT NOT NULL,
    finished_at        TEXT
);
CREATE TABLE IF NOT EXISTS receipts (
    session_id  TEXT NOT NULL,
    commit_id   TEXT NOT NULL,
    environment TEXT NOT NULL,
    body        TEXT NOT NULL,
    PRIMARY KEY (session_id, commit_id, environment)
);
"""


class EventConflictError(ValueError):
    """An event ID already exists with different contents."""


class SequenceError(ValueError):
    """An event's sequence number is not the next one the store expects."""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class AppendResult:
    event_id: str
    sequence: int
    appended: bool  # False when an identical event was already present


@dataclass(frozen=True)
class SnapshotRow:
    strategy: str
    projection_version: int
    through_sequence: int
    state_hash: str
    state: dict[str, Any]
    saved_at: str


@dataclass(frozen=True)
class MemoryRow:
    kind: str
    through_sequence: int
    body: dict[str, Any]
    writer_pid: int
    saved_at: str


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Rollback journal (not WAL) so read-only inspectors can open the file
        # without creating side files; writers are serialized by the service lock.
        self._conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('store_schema_version', ?)",
            (str(STORE_SCHEMA_VERSION),),
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> EventStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ meta

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    # ---------------------------------------------------------------- events

    def append(self, event: Event) -> AppendResult:
        """Append one event durably.

        Re-appending an identical event is a no-op. A different event under an
        existing ID is rejected, as is any sequence other than the next one.
        """
        body = event.canonical()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT body FROM events WHERE session_id = ? AND event_id = ?",
                (event.session_id, event.event_id),
            ).fetchone()
            if existing is not None:
                if existing[0] == body:
                    return AppendResult(event.event_id, event.sequence, appended=False)
                raise EventConflictError(
                    f"{event.event_id} already exists in session {event.session_id} "
                    "with different contents; events are immutable."
                )
            last = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE session_id = ?",
                (event.session_id,),
            ).fetchone()[0]
            if event.sequence != last + 1:
                raise SequenceError(
                    f"{event.event_id} has sequence {event.sequence}, but the next sequence "
                    f"for session {event.session_id} is {last + 1}."
                )
            conn.execute(
                "INSERT INTO events(session_id, sequence, event_id, type, body) VALUES (?, ?, ?, ?, ?)",
                (event.session_id, event.sequence, event.event_id, event.type, body),
            )
        return AppendResult(event.event_id, event.sequence, appended=True)

    def append_many(self, events: Iterable[Event]) -> list[AppendResult]:
        return [self.append(e) for e in events]

    def events(self, session_id: str, through_sequence: int | None = None) -> list[Event]:
        sql = "SELECT body FROM events WHERE session_id = ?"
        params: list[Any] = [session_id]
        if through_sequence is not None:
            sql += " AND sequence <= ?"
            params.append(through_sequence)
        sql += " ORDER BY sequence"
        return [Event.model_validate_json(r[0]) for r in self._conn.execute(sql, params)]

    def last_sequence(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row[0])

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """Read-only SQL for the gate and tools."""
        cur = self._conn.cursor()
        cur.row_factory = sqlite3.Row
        return cur.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------- snapshots

    def save_snapshot(self, strategy: str, state: WorkingState) -> SnapshotRow:
        row = SnapshotRow(
            strategy=strategy,
            projection_version=state.projection_version,
            through_sequence=state.through_sequence,
            state_hash=state.state_hash(),
            state=state.canonical_dump(),
            saved_at=utc_now(),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                state.session_id,
                strategy,
                row.projection_version,
                row.through_sequence,
                row.state_hash,
                canonical_json(row.state),
                row.saved_at,
            ),
        )
        return row

    def load_snapshot(self, session_id: str, strategy: str) -> SnapshotRow | None:
        r = self._conn.execute(
            "SELECT strategy, projection_version, through_sequence, state_hash, state_json, saved_at "
            "FROM snapshots WHERE session_id = ? AND strategy = ?",
            (session_id, strategy),
        ).fetchone()
        if r is None:
            return None
        return SnapshotRow(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5])

    # -------------------------------------------------------------- memories

    def save_memory(self, session_id: str, kind: str, through_sequence: int, body: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO memories VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, kind, through_sequence, canonical_json(body), os.getpid(), utc_now()),
        )

    def load_memory(self, session_id: str, kind: str) -> MemoryRow | None:
        r = self._conn.execute(
            "SELECT kind, through_sequence, body, writer_pid, saved_at FROM memories "
            "WHERE session_id = ? AND kind = ?",
            (session_id, kind),
        ).fetchone()
        if r is None:
            return None
        return MemoryRow(r[0], r[1], json.loads(r[2]), r[3], r[4])

    # ------------------------------------------------------------------ runs

    _RUN_COLUMNS = (
        "run_id", "session_id", "run_dir", "strategy", "planner_mode", "provider", "target_environment",
        "trace_id", "root_span_id", "status", "next_stage", "started_at", "finished_at",
    )

    def insert_run(self, **row: Any) -> None:
        cols = [c for c in self._RUN_COLUMNS if c in row]
        self._conn.execute(
            f"INSERT INTO runs({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [row[c] for c in cols],
        )

    def update_run(self, run_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(f"UPDATE runs SET {sets} WHERE run_id = ?", [*fields.values(), run_id])

    def _runs(self, where: str, params: Iterable[Any]) -> list[dict[str, Any]]:
        return [dict(r) for r in self.query(f"SELECT * FROM runs WHERE {where} ORDER BY started_at", params)]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._runs("run_id = ?", (run_id,))
        return rows[0] if rows else None

    def active_run(self, session_id: str) -> dict[str, Any] | None:
        rows = self._runs("session_id = ? AND status = 'active'", (session_id,))
        return rows[-1] if rows else None

    def list_runs(self, session_id: str) -> list[dict[str, Any]]:
        return self._runs("session_id = ?", (session_id,))

    def previous_completed_run(self, session_id: str) -> dict[str, Any] | None:
        rows = self._runs("session_id = ? AND status = 'completed'", (session_id,))
        return rows[-1] if rows else None

    # -------------------------------------------------------------- receipts

    def find_receipt(self, session_id: str, commit: str, environment: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT body FROM receipts WHERE session_id = ? AND commit_id = ? AND environment = ?",
            (session_id, commit, environment),
        ).fetchone()
        return json.loads(r[0]) if r else None

    def insert_receipt(self, session_id: str, commit: str, environment: str, body: dict[str, Any]) -> bool:
        """Insert a receipt; returns False if one already existed for this target."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO receipts VALUES (?, ?, ?, ?)",
            (session_id, commit, environment, canonical_json(body)),
        )
        return cur.rowcount == 1
