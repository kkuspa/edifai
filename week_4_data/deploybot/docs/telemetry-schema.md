# Telemetry schema

All custom attributes use the `workshop.` prefix. They are this workshop's own names, not OpenTelemetry GenAI semantic conventions, and they will not change when those conventions do. Resource attributes follow OTel conventions: `service.name=agent-state-workshop`, `service.version`, `process.pid`. Sampling is 100%.

## Span hierarchy

Each run produces one trace with one root.

```
agent.resume                      root; emitted when the run finishes
|- state.load
|- memory.project
|- context.build
|  `- tool.inspect_head           baseline only (read-only head lookup)
|- planner.propose
|  `- model.generate              live mode only, one per request attempt
|- policy.validate
|- tool.deploy                    only when the gate allowed a deploy
`- state.save
```

Stage spans carry `workshop.run_id` and `workshop.stage`. The root is created with the run's recorded start time and pre-assigned IDs, so stages executed by separate `workshop step` processes (or by the GUI) still share one trace and one root. In a stepped run the root's duration includes the time between steps. The root links to the previous completed run's root (`workshop.link.reason=previous_resume_run`). Historical fixture events are never presented as spans; they appear as event IDs in attributes.

Span status reports runtime success. A gate rejection or a failed test is a domain outcome and leaves the span status `UNSET`. Exceptions (including an unfinished exercise) set `ERROR` and add an `exception` span event.

## Attributes by span

`agent.resume`: `workshop.session_id`, `workshop.run_id`, `workshop.scenario_id`, `workshop.planner_mode`, `workshop.memory_strategy`, `workshop.target.environment`, `workshop.run.status`, `workshop.execution.mode` (`continuous` or `stepped`), `workshop.execution.process_count`, `workshop.projection.through_sequence`, `workshop.source.through_sequence`, `workshop.outcome.proposed_action`, `workshop.outcome.proposed_commit`, `workshop.outcome.gate`, `workshop.outcome.executed_action` (`deploy` or `none`), and in live mode `workshop.llm.provider`, `workshop.llm.request_model`.

`state.load`: `workshop.source.first_sequence`, `workshop.source.through_sequence`, `workshop.source.event_count`, `workshop.source.events_sha256`, `workshop.snapshot.status` (`missing`, `stale_position`, `current_position`, `version_mismatch`, or `summary_memory`), `workshop.snapshot.through_sequence`, `workshop.snapshot.projection_version`, `workshop.memory.summary_through_sequence` (baseline).

`memory.project`: `workshop.memory_strategy`, `workshop.projection.kind` (`event-replay` or `narrative-summary`), `workshop.projection.version`, `workshop.projection.input_first_sequence`, `workshop.projection.input_through_sequence`, `workshop.projection.through_sequence`, `workshop.projection.events_not_incorporated` (baseline), `workshop.snapshot.matches_replay`, `workshop.state.hash`, `workshop.artifact.state`.

`context.build`, set by the runner: `workshop.memory_strategy`, `workshop.context.through_sequence`, `workshop.context.hash`, `workshop.artifact.context`, `workshop.annotation.complete`, `workshop.annotation.missing`, `workshop.context.unsourced_facts`.

`context.build`, set by your `annotate_context_span` (exercise 3):

| Attribute | Type | Required |
|---|---|---|
| `workshop.target.commit` | string | when a head is known |
| `workshop.target.environment` | string | always |
| `workshop.target.source_event_id` | string | provided in the skeleton |
| `workshop.test.status` | `passed`, `failed`, `unknown` | always |
| `workshop.test.commit` | string | recommended |
| `workshop.test.source_event_id` | string | when the context has one |
| `workshop.approval.status` | `approved`, `missing` | always |
| `workshop.approval.id` | string | recommended |
| `workshop.approval.source_event_id` | string | when the context has one |
| `workshop.blockers` | string array | always (may be empty) |
| span event `evidence.selected` with `workshop.evidence.event_ids` (string array) | | always |

`planner.propose`: `workshop.planner_mode`, `workshop.planner.name`, `workshop.planner.elapsed_ms`, `workshop.proposal.action`, `workshop.proposal.commit`, `workshop.proposal.environment`, `workshop.proposal.blockers`, `workshop.proposal.evidence_event_ids`, `workshop.proposal.hash`, `workshop.artifact.proposal`, `workshop.artifact.planner_input`; live mode adds `workshop.llm.provider`, `workshop.llm.request_model`, `workshop.llm.attempts`, and token counts when the provider returns them. Simulated runs carry no token or provider attributes.

`model.generate` (live only): `workshop.llm.provider`, `workshop.llm.request_model`, `workshop.llm.response_model`, `workshop.llm.response_id`, `workshop.llm.attempt`, `workshop.llm.latency_ms`, `workshop.llm.input_tokens`, `workshop.llm.output_tokens`, `workshop.llm.output_valid`; event `output.invalid` on a malformed response.

`policy.validate`: `workshop.gate.outcome`, `workshop.gate.reason_codes`, `workshop.gate.authoritative_blockers`, `workshop.gate.checked_through_sequence`, `workshop.gate.authoritative.head_commit`, `.head_source_event_id`, `.test_result`, `.test_source_event_id`, `.approval_id`, `.approval_source_event_id`, `workshop.artifact.gate`; event `proposal.rejected` when blocked.

`tool.deploy`: `workshop.tool.name=mock_deploy`, `workshop.deploy.commit`, `workshop.deploy.environment`, `workshop.deploy.receipt_id`, `workshop.deploy.reused_existing`, `workshop.artifact.receipt`.

`state.save`: `workshop.snapshot.saved`, `workshop.snapshot.projection_version`, `workshop.snapshot.through_sequence`, `workshop.state.hash`, `workshop.deploy.receipt_id`, `workshop.snapshot.skip_reason` (baseline).

Nested objects never go in attributes. Attributes reference artifacts by file name (`workshop.artifact.*`), and the viewers show the linked artifact.

## Local export format: `spans.jsonl`

One JSON object per completed span, appended as each span ends. This is the workshop's serialization of SDK `ReadableSpan` objects, not OTLP JSON.

```json
{
  "format": "workshop-span-jsonl/1",
  "name": "policy.validate",
  "trace_id": "32 hex characters",
  "span_id": "16 hex characters",
  "parent_span_id": "16 hex characters, or null for the root",
  "kind": "INTERNAL",
  "start_time_unix_nano": 1792480034015000000,
  "end_time_unix_nano": 1792480034016000000,
  "status": {"code": "UNSET", "description": null},
  "attributes": {"workshop.gate.outcome": "blocked"},
  "events": [{"name": "proposal.rejected", "time_unix_nano": 0, "attributes": {}}],
  "links": [{"trace_id": "...", "span_id": "...", "attributes": {}}],
  "resource": {"service.name": "agent-state-workshop"},
  "scope": {"name": "agent_state_workshop", "version": "0.1.0"}
}
```

Spans are written in end order, so the root appears last. With `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` set, the same spans also go through a batch processor to the OTLP HTTP exporter, with identical trace and span IDs. OTLP failures are recorded in `manifest.json` under `otlp.errors` and never change the run's outcome or its local file.
