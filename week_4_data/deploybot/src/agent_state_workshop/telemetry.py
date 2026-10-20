"""OpenTelemetry SDK setup, the local JSONL exporter, and optional OTLP export.

A run may execute in one process (`workshop run`) or across several
(`workshop step`, the GUI). Each run gets a trace ID and a root span ID when
it starts. Stage spans are created as children of that planned root, and the
root `agent.resume` span itself is emitted once, when the run finishes, with
the run's recorded start time. Every stage therefore lands in one trace with
one root, whichever process executed it.

spans.jsonl holds one JSON object per completed span. It is this workshop's
serialization of SDK spans, NOT OTLP JSON. See docs/telemetry-schema.md.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Link, NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags

from . import __version__

SERVICE_NAME = "agent-state-workshop"
SPAN_FORMAT = "workshop-span-jsonl/1"
DEFAULT_JAEGER_UI = "http://localhost:16686"
OTLP_TIMEOUT_SECONDS = 3.0


def new_trace_id() -> str:
    return f"{secrets.randbits(128) or 1:032x}"


def new_span_id() -> str:
    return f"{secrets.randbits(64) or 1:016x}"


def otlp_endpoint_from_env() -> str | None:
    return os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or None


def jaeger_trace_url(trace_id: str) -> str:
    base = os.environ.get("JAEGER_UI_URL", DEFAULT_JAEGER_UI).rstrip("/")
    return f"{base}/trace/{trace_id}"


# ------------------------------------------------------------ local exporter


def span_to_dict(span: ReadableSpan) -> dict[str, Any]:
    ctx = span.get_span_context()
    parent = span.parent
    return {
        "format": SPAN_FORMAT,
        "name": span.name,
        "trace_id": f"{ctx.trace_id:032x}",
        "span_id": f"{ctx.span_id:016x}",
        "parent_span_id": f"{parent.span_id:016x}" if parent else None,
        "kind": span.kind.name,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
        "status": {"code": span.status.status_code.name, "description": span.status.description},
        "attributes": dict(span.attributes or {}),
        "events": [
            {"name": e.name, "time_unix_nano": e.timestamp, "attributes": dict(e.attributes or {})}
            for e in span.events
        ],
        "links": [
            {
                "trace_id": f"{link.context.trace_id:032x}",
                "span_id": f"{link.context.span_id:016x}",
                "attributes": dict(link.attributes or {}),
            }
            for link in span.links
        ],
        "resource": dict(span.resource.attributes),
        "scope": {
            "name": span.instrumentation_scope.name if span.instrumentation_scope else None,
            "version": span.instrumentation_scope.version if span.instrumentation_scope else None,
        },
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    return str(value)


class JsonlSpanExporter(SpanExporter):
    """Appends one JSON object per completed span to a local file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        lines = [json.dumps(span_to_dict(s), default=_json_default, sort_keys=True) for s in spans]
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def read_spans(path: Path) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class _FailureRecordingExporter(SpanExporter):
    """Wraps the OTLP exporter so the run can report export failures."""

    def __init__(self, inner: SpanExporter, sink: list[str]):
        self._inner = inner
        self._sink = sink

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._inner.export(spans)
        except Exception as exc:
            self._sink.append(f"{type(exc).__name__}: {exc}")
            return SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            self._sink.append("OTLP export returned FAILURE")
        return result

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


# ------------------------------------------------------------- planned IDs


class _PlannedRootIds(RandomIdGenerator):
    """Random IDs, except the run's root span gets its pre-assigned IDs."""

    def __init__(self, trace_id: int, root_span_id: int):
        self._trace_id = trace_id
        self._root_span_id = root_span_id
        self.emit_root = False

    def generate_trace_id(self) -> int:
        return self._trace_id

    def generate_span_id(self) -> int:
        if self.emit_root:
            self.emit_root = False
            return self._root_span_id
        return super().generate_span_id()


class RunTelemetry:
    """Tracing for one process's share of one run."""

    def __init__(self, run_dir: Path, trace_id: str, root_span_id: str, otlp_endpoint: str | None):
        self.trace_id = trace_id
        self.root_span_id = root_span_id
        self.otlp_endpoint = otlp_endpoint
        self._ids = _PlannedRootIds(int(trace_id, 16), int(root_span_id, 16))
        resource = Resource.create(
            {
                "service.name": SERVICE_NAME,
                "service.version": __version__,
                "process.pid": os.getpid(),
            }
        )
        self.provider = TracerProvider(resource=resource, sampler=ALWAYS_ON, id_generator=self._ids)
        self.provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(run_dir / "spans.jsonl")))
        self.otlp_errors: list[str] = []
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                exporter = OTLPSpanExporter(endpoint=otlp_endpoint, timeout=OTLP_TIMEOUT_SECONDS)
                self.provider.add_span_processor(
                    BatchSpanProcessor(_FailureRecordingExporter(exporter, self.otlp_errors), export_timeout_millis=5000)
                )
            except Exception as exc:  # OTLP problems must never stop the run
                self.otlp_errors.append(f"{type(exc).__name__}: {exc}")
        self.tracer = self.provider.get_tracer("agent_state_workshop", __version__)
        self._root_ctx = trace.set_span_in_context(
            NonRecordingSpan(
                SpanContext(
                    trace_id=int(trace_id, 16),
                    span_id=int(root_span_id, 16),
                    is_remote=True,
                    trace_flags=TraceFlags(TraceFlags.SAMPLED),
                )
            )
        )

    @contextmanager
    def stage_span(self, name: str) -> Iterator[Any]:
        """A child of the run's root. Runtime exceptions are recorded, then re-raised."""
        with self.tracer.start_as_current_span(
            name, context=self._root_ctx, record_exception=True, set_status_on_exception=True
        ) as span:
            yield span

    def emit_root(
        self,
        start_time_ns: int,
        attributes: dict[str, Any],
        links: list[tuple[str, str, dict[str, Any]]],
        error: str | None = None,
    ) -> None:
        otel_links = [
            Link(
                SpanContext(int(t, 16), int(s, 16), is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED)),
                attrs,
            )
            for t, s, attrs in links
        ]
        self._ids.emit_root = True
        span = self.tracer.start_span(
            "agent.resume", context=trace.Context(), links=otel_links, start_time=start_time_ns
        )
        self._ids.emit_root = False
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        if error:
            span.set_status(Status(StatusCode.ERROR, error))
        span.end()

    def shutdown(self) -> None:
        """Flush local and OTLP exporters. OTLP failures are logged, never raised."""
        otlp_logger = logging.getLogger("opentelemetry.exporter.otlp.proto.http")
        previous = otlp_logger.level
        otlp_logger.setLevel(logging.CRITICAL)
        try:
            self.provider.force_flush(timeout_millis=6000)
            self.provider.shutdown()
        except Exception as exc:
            self.otlp_errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            otlp_logger.setLevel(previous)
