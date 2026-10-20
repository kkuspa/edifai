"""Helpers shared by the exercises, the reference solution, and the runner."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .models import (
    REQUIRED_SUITE,
    ApprovalRecord,
    Event,
    TestRecord,
    WorkingState,
)
from .store import EventConflictError


class ExerciseIncomplete(NotImplementedError):
    """Raised by a TODO in exercises.py. This is expected in the starter code."""


def todo(message: str) -> Any:
    raise ExerciseIncomplete(message)


# ------------------------------------------------------------------ iteration


def ordered_unique(events: Iterable[Event]) -> list[Event]:
    """Events sorted by sequence, with identical duplicates removed.

    Two different events that share an event_id raise EventConflictError,
    exactly as the store would on append.
    """
    by_id: dict[str, Event] = {}
    for event in events:
        seen = by_id.get(event.event_id)
        if seen is None:
            by_id[event.event_id] = event
        elif seen.canonical() != event.canonical():
            raise EventConflictError(f"conflicting contents for {event.event_id}")
    return sorted(by_id.values(), key=lambda e: e.sequence)


def session_of(events: Sequence[Event], default: str = "release-demo") -> str:
    return events[0].session_id if events else default


# ------------------------------------------------------------------- lookups


def find_test(state: WorkingState, commit: str | None, suite: str = REQUIRED_SUITE) -> TestRecord | None:
    """The stored result for exactly this commit and suite, or None."""
    if commit is None:
        return None
    return next((t for t in state.tests if t.commit == commit and t.suite == suite), None)


def find_approval(state: WorkingState, commit: str | None, environment: str) -> ApprovalRecord | None:
    """An approval for exactly this commit and environment, or None."""
    if commit is None:
        return None
    return next((a for a in state.approvals if a.commit == commit and a.environment == environment), None)


# ------------------------------------------------------------------- tracing


def set_attr(span: Any, key: str, value: Any) -> None:
    """Set a span attribute, skipping None (OTel rejects None values)."""
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        value = [str(v) for v in value]
    span.set_attribute(key, value)


EVIDENCE_EVENT = "evidence.selected"
EVIDENCE_IDS_KEY = "workshop.evidence.event_ids"

# Keys that context.build must always carry once exercise 3 is complete.
REQUIRED_CONTEXT_KEYS = (
    "workshop.target.commit",
    "workshop.target.environment",
    "workshop.test.status",
    "workshop.approval.status",
    "workshop.blockers",
)


def annotation_gaps(attributes: Mapping[str, Any], events: Iterable[Any], context: Any) -> list[str]:
    """What the context.build annotation is missing, given the context it describes.

    Source-event keys are required only when the context has a source to cite.
    `events` are SDK span events (objects with .name and .attributes) or dicts.
    """
    missing = [k for k in REQUIRED_CONTEXT_KEYS if k not in attributes]
    if context.target.commit is None and "workshop.target.commit" in missing:
        missing.remove("workshop.target.commit")
    if context.current_test.source_event_id and "workshop.test.source_event_id" not in attributes:
        missing.append("workshop.test.source_event_id")
    if context.matching_approval.source_event_id and "workshop.approval.source_event_id" not in attributes:
        missing.append("workshop.approval.source_event_id")

    evidence_ok = False
    for ev in events:
        name = ev["name"] if isinstance(ev, Mapping) else ev.name
        attrs = ev["attributes"] if isinstance(ev, Mapping) else ev.attributes
        if name == EVIDENCE_EVENT and attrs and EVIDENCE_IDS_KEY in attrs:
            evidence_ok = True
    if not evidence_ok:
        missing.append(f"event:{EVIDENCE_EVENT}")
    return missing
