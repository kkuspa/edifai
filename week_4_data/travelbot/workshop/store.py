"""SQLite persistence for one session: changes, state, transcript, and execution records.

Each session directory owns one ``session.sqlite``. The CLI and the GUI open the same file,
so either interface can inspect or continue a session the other started. Writes happen
inside the session lock (see ``service.py``); reads are safe at any time because the
database runs in WAL mode.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel

from .models import Change, TaskState, TranscriptEntry


class ConflictingChangeError(Exception):
    """A change ID was reused with a different payload."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS changes (
    seq INTEGER PRIMARY KEY,
    change_id TEXT UNIQUE NOT NULL,
    fingerprint TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    idx INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id TEXT NOT NULL,
    before TEXT,
    after TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcript (idx INTEGER PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL,
    idx INTEGER NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (kind, idx)
);
CREATE TABLE IF NOT EXISTS operations (
    op_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    result TEXT NOT NULL
);
"""


def _dump(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        return obj.model_dump_json()
    return json.dumps(obj, default=str)


class RunStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._depth = 0

    def close(self) -> None:
        self.conn.close()

    # -- transactions -----------------------------------------------------------------

    def __enter__(self) -> "RunStore":
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._depth -= 1
        if self._depth == 0:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")

    def read(self) -> "_ReadSnapshot":
        """A read transaction: every query inside it sees the same committed snapshot."""
        return _ReadSnapshot(self)

    # -- key/value --------------------------------------------------------------------

    def put(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, _dump(value)))

    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # -- changes ----------------------------------------------------------------------

    def record_change(self, change: Change) -> bool:
        """Append a change. Returns False for an identical duplicate; raises on a conflicting one."""
        row = self.conn.execute("SELECT fingerprint FROM changes WHERE change_id = ?", (change.change_id,)).fetchone()
        if row:
            if row[0] == change.fingerprint():
                return False
            raise ConflictingChangeError(f"Change {change.change_id} already recorded with a different payload")
        self.conn.execute(
            "INSERT INTO changes (seq, change_id, fingerprint, body) VALUES (?, ?, ?, ?)",
            (change.seq, change.change_id, change.fingerprint(), change.model_dump_json()),
        )
        return True

    def changes(self) -> list[Change]:
        rows = self.conn.execute("SELECT body FROM changes ORDER BY seq").fetchall()
        return [Change.model_validate_json(r[0]) for r in rows]

    # -- state ------------------------------------------------------------------------

    def save_state(self, state: TaskState) -> None:
        self.put("state", state.model_dump(mode="json"))

    def load_state(self) -> TaskState | None:
        data = self.get("state")
        return TaskState.model_validate(data) if data else None

    def save_snapshot(self, change_id: str, before: TaskState | None, after: TaskState) -> None:
        self.conn.execute(
            "INSERT INTO snapshots (change_id, before, after) VALUES (?, ?, ?)",
            (change_id, before.model_dump_json() if before else None, after.model_dump_json()),
        )

    def snapshots(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT idx, change_id, before, after FROM snapshots ORDER BY idx").fetchall()
        return [
            {"index": r[0], "change_id": r[1], "before": json.loads(r[2]) if r[2] else None, "after": json.loads(r[3])}
            for r in rows
        ]

    # -- transcript -------------------------------------------------------------------

    def append_transcript(self, entry: TranscriptEntry) -> TranscriptEntry:
        idx = self.conn.execute("SELECT COALESCE(MAX(idx), -1) + 1 FROM transcript").fetchone()[0]
        entry = entry.model_copy(update={"index": idx})
        self.conn.execute("INSERT INTO transcript (idx, body) VALUES (?, ?)", (idx, entry.model_dump_json()))
        return entry

    def transcript(self) -> list[TranscriptEntry]:
        rows = self.conn.execute("SELECT body FROM transcript ORDER BY idx").fetchall()
        return [TranscriptEntry.model_validate_json(r[0]) for r in rows]

    # -- execution records ------------------------------------------------------------

    def append_record(self, kind: str, body: Any) -> int:
        idx = self.conn.execute("SELECT COALESCE(MAX(idx), -1) + 1 FROM records WHERE kind = ?", (kind,)).fetchone()[0]
        self.conn.execute("INSERT INTO records (kind, idx, body) VALUES (?, ?, ?)", (kind, idx, _dump(body)))
        return idx

    def records(self, kind: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT body FROM records WHERE kind = ? ORDER BY idx", (kind,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def iter_records(self, kind: str) -> Iterator[dict[str, Any]]:
        yield from self.records(kind)

    # -- idempotent operations ----------------------------------------------------------

    def operation(self, op_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT kind, result FROM operations WHERE op_id = ?", (op_id,)).fetchone()
        return {"kind": row[0], "result": json.loads(row[1])} if row else None

    def save_operation(self, op_id: str, kind: str, result: Any) -> None:
        self.conn.execute("INSERT INTO operations (op_id, kind, result) VALUES (?, ?, ?)", (op_id, kind, _dump(result)))


class _ReadSnapshot:
    def __init__(self, store: RunStore):
        self.store = store

    def __enter__(self) -> RunStore:
        if self.store._depth == 0:
            self.store.conn.execute("BEGIN DEFERRED")
            self.store.conn.execute("SELECT 1 FROM kv LIMIT 1").fetchall()  # pin the snapshot now
        self.store._depth += 1
        return self.store

    def __exit__(self, exc_type, exc, tb) -> None:
        self.store._depth -= 1
        if self.store._depth == 0:
            self.store.conn.execute("COMMIT")
