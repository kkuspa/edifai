"""Agent tools: read-only repository inspection and a mock deployment.

The mock deployment has no network side effects. It writes a receipt row to
SQLite, and only after the gate has allowed the exact proposal.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Event, GateResult, Proposal, Receipt, sha256_of
from .store import EventStore, utc_now


class DeploymentRefused(RuntimeError):
    """Raised when mock_deploy is called without a matching allowed gate result."""


@dataclass(frozen=True)
class HeadObservation:
    commit: str
    source_event_id: str
    sequence: int


def inspect_head(store: EventStore, session_id: str) -> HeadObservation | None:
    """Read the repository's current head commit (read-only)."""
    rows = store.query(
        "SELECT event_id, sequence, json_extract(body, '$.payload.commit') AS commit_id "
        "FROM events WHERE session_id = ? AND type = 'head.changed' ORDER BY sequence DESC LIMIT 1",
        (session_id,),
    )
    if not rows:
        return None
    return HeadObservation(rows[0]["commit_id"], rows[0]["event_id"], rows[0]["sequence"])


@dataclass(frozen=True)
class RequestObservation:
    message: str
    source_event_id: str


def latest_user_request(store: EventStore, session_id: str) -> RequestObservation | None:
    """The most recent user message that triggered this resume (read-only)."""
    rows = store.query(
        "SELECT event_id, json_extract(body, '$.payload.message') AS message FROM events "
        "WHERE session_id = ? AND type = 'session.resume_requested' ORDER BY sequence DESC LIMIT 1",
        (session_id,),
    )
    return RequestObservation(rows[0]["message"], rows[0]["event_id"]) if rows else None


def recent_activity(store: EventStore, session_id: str, limit: int = 10) -> list[Event]:
    """The last few events, for display (read-only)."""
    return store.events(session_id)[-limit:]


def mock_deploy(store: EventStore, session_id: str, proposal: Proposal, gate: GateResult, run_id: str) -> Receipt:
    """Record a mock deployment. Idempotent per session/commit/environment."""
    if (
        not gate.allowed
        or proposal.action != "deploy"
        or gate.action != "deploy"
        or gate.commit != proposal.commit
        or gate.environment != proposal.environment
        or proposal.commit is None
    ):
        raise DeploymentRefused("mock_deploy requires an allowed gate result for this exact proposal")

    existing = store.find_receipt(session_id, proposal.commit, proposal.environment)
    if existing is None:
        receipt = Receipt(
            receipt_id="rcpt-" + sha256_of([session_id, proposal.commit, proposal.environment])[:12],
            session_id=session_id,
            commit=proposal.commit,
            environment=proposal.environment,
            deployed_at=utc_now(),
            run_id=run_id,
            gate_checked_through_sequence=gate.authoritative.checked_through_sequence,
        )
        if store.insert_receipt(session_id, proposal.commit, proposal.environment, receipt.model_dump(mode="json")):
            return receipt
        existing = store.find_receipt(session_id, proposal.commit, proposal.environment)
    assert existing is not None
    return Receipt.model_validate({**existing, "reused_existing": True})
