# Agent memory, state, and traces workshop

A deployment assistant paused after testing commit `a1` and receiving approval to deploy `a1` to staging. While it was stopped, the code moved to `b2` and the tests failed. The user comes back and asks, "Can you finish the deployment?" The assistant remembers "tests passed and deployment was approved" and proposes deploying `b2`. A separate policy gate catches the mistake.

In this 20-minute exercise you repair the data the assistant uses when it resumes, then use OpenTelemetry traces from your own runs to explain why its next action changed. Retrieval, embeddings, and RAG are out of scope.

Everything runs offline after setup: no API keys, Docker, or network. You can work entirely from the command line. An optional local web GUI offers the same operations; it is never required.

## Setup (before the clock starts)

Requires [uv](https://docs.astral.sh/uv/). The tested Python version is 3.12 (pinned in `.python-version`; uv installs it if needed).

```bash
uv sync --frozen
```

```bash
uv run workshop doctor
```

`doctor` checks imports, SQLite, the local span exporter, and the HTML renderer, and reports optional extras (GUI, Jaeger, LLM keys) without requiring them.

## The timed exercise

Full instructions with timing: [docs/workshop.md](docs/workshop.md). The only file you edit is [src/agent_state_workshop/exercises.py](src/agent_state_workshop/exercises.py), which has three functions:

1. `project_state(events)`: keep each fact's commit, environment, ordering, and source event.
2. `build_context(state, target_environment)`: give the planner only the facts that apply to the current commit and target, and report missing facts as missing.
3. `annotate_context_span(span, context)`: record the evidence behind the decision on the `context.build` span.

Every run and check loads the file fresh, so you never restart anything after an edit.

### Command-line path (complete on its own)

```bash
# 1. Baseline: the flawed memory proposes deploying b2; the gate blocks it.
uv run workshop prepare --scenario stale-resume --db .workshop/base.sqlite
uv run workshop run --db .workshop/base.sqlite --strategy baseline --planner simulated --out runs/before

# 2. Your repaired strategy, against an independent copy of the same history.
uv run workshop prepare --scenario stale-resume --db .workshop/fixed.sqlite
uv run workshop check --strategy student
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --out runs/after

# 3. Inspect and compare the traces.
uv run workshop trace --run runs/after --span context.build
uv run workshop compare --before runs/before --after runs/after
uv run workshop visualize --before runs/before --after runs/after --output submissions/comparison.html

# 4. Follow-ups: tests pass for b2, then b2 is approved for staging.
uv run workshop append --db .workshop/fixed.sqlite --fixture tests-pass-b2
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --out runs/tests-pass
uv run workshop append --db .workshop/fixed.sqlite --fixture approve-b2-staging
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --out runs/approved

# 5. Submission evidence and the final check.
uv run workshop export --before runs/before --after runs/after --followup runs/tests-pass --followup runs/approved
uv run workshop check --strategy student
```

Until you finish the exercises, `run --strategy student` stops at the first unfinished step with exit code 2 and says so. That is expected; the runtime is working.

Expected results with a finished student strategy:

| Run | Proposal | Gate | Executed |
|---|---|---|---|
| `runs/before` (baseline) | deploy b2 to staging | blocked: tests_failed, approval_missing_for_target | nothing |
| `runs/after` | report_test_failure for b2 | allowed (no side effect) | nothing |
| `runs/tests-pass` | request_approval for b2 / staging | allowed (no side effect) | nothing |
| `runs/approved` | deploy b2 to staging | allowed | mock deploy, receipt written |

### GUI path (optional)

```bash
uv run --extra gui workshop gui
```

This installs FastAPI and uvicorn on first use and opens `http://127.0.0.1:8765/` (localhost only). The page has three areas: session and execution controls, the conversation with the proposed action, gate outcome, and executed action shown separately, and an inspector with State, Model input, Tools, Validation, Trace, Events, and Checks tabs. Below them, a side-by-side comparison renders any two recorded runs.

Both interfaces call the same Python services and write the same files, so you can switch at any time. A session prepared or a run started in the terminal appears in the GUI, and the reverse. Grading uses the same checks and the same evidence files either way.

| Task | CLI | GUI control |
|---|---|---|
| Choose implementation and mode | `--strategy`, `--planner`, `--provider` | Implementation, Planner mode, Provider |
| Start a scenario | `prepare --db .workshop/NAME.sqlite` | Session name + Prepare |
| Apply a fixture change | `append --fixture NAME` | Fixture changes buttons |
| Advance one stage | `step --db ... [--strategy ...]` | Step one stage |
| Run to completion | `run ...` or `step --until-done` | Run to completion / Finish active run |
| Inspect state, diffs, model input, tools, gate | `show --run DIR --artifact state\|state-diff\|planner-input\|tools\|proposal\|gate` and `diff` | Inspector tabs |
| Inspect the trace | `trace --run DIR [--attributes] [--span NAME]` | Trace tab |
| Compare runs | `compare`, `visualize` | Compare runs |
| Export evidence | `export` | Export evidence |
| Run exercise checks | `check --strategy student` | Run exercise checks |
| Reset the session | `reset --db ...` | Reset session |
| See what either interface did | `ops` | Operations list |

## Stages

One run of the resumed agent is seven stages. `workshop step` executes one per call, in a new process each time if you like; `workshop run` executes them all.

```
state.load -> memory.project -> context.build -> planner.propose -> policy.validate -> tool.deploy -> state.save
```

`tool.deploy` runs only when the gate allows a deploy proposal; otherwise it is skipped and no span or receipt exists. Each stage reads its inputs from the run directory and writes its outputs there, which is what lets a run continue across processes and interfaces. All stages of one run share one trace with one `agent.resume` root.

## What gets written

| Location | Contents |
|---|---|
| `.workshop/*.sqlite` | Session databases: the append-only event history plus derived snapshots, the baseline's saved summary, run records, and mock deployment receipts. |
| `.workshop/operations.sqlite` | Every state-changing operation from either interface, keyed by operation ID. |
| `runs/<name>/` | One immutable directory per run: `manifest.json`, `events.json`, `state.json`, `context.json`, `planner-input.json`, `proposal.json`, `gate.json`, `tools.json`, `receipt.json` (only after a deployment), and `spans.jsonl`. |
| `submissions/` | `comparison.html`, `evidence.json`, `evidence.md`, and `findings.md` from `workshop export`. |

`spans.jsonl` is this workshop's serialization of OpenTelemetry SDK spans, one JSON object per line. It is not OTLP JSON. See [docs/telemetry-schema.md](docs/telemetry-schema.md).

## Submission

Submit `submissions/comparison.html`, `submissions/evidence.md`, and your completed `submissions/findings.md` (copied from [docs/findings-template.md](docs/findings-template.md) by `export`). Every answer must cite span IDs and event IDs from your own runs. Screenshots are never required.

## Optional extensions

### Jaeger

```bash
docker compose up -d jaeger
```

```bash
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:4318/v1/traces uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --out runs/jaeger
```

```bash
uv run workshop trace-url --run runs/jaeger
```

The image is pinned to `jaegertracing/jaeger:2.22.0` and bound to localhost. The UI is at `http://localhost:16686`. Jaeger receives the same trace and span IDs as the local `spans.jsonl`. If Jaeger is down, the run still completes, writes its local trace, and records the export error in `manifest.json`.

### Live LLM planner

Copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` (or both, choosing with `LLM_PROVIDER` or `--provider`). Model names come from `ANTHROPIC_MODEL` and `OPENAI_MODEL`; the defaults in `.env.example` are suggestions, so confirm the model is available to your account. `OPENAI_BASE_URL` points the OpenAI adapter at any compatible server.

```bash
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner live --provider anthropic --out runs/live
```

The prompt is fixed across strategies; only the context changes. Requests, responses, token counts (when the provider returns them), and a `model.generate` span per attempt are recorded. Keys stay in the process environment and never reach artifacts or the GUI. A live model may not reproduce the baseline mistake; record what you observed rather than what the simulator predicts.

More: approval revocation, delayed CI results, and other extensions are listed in [docs/instructor.md](docs/instructor.md).

## Repository map

| Path | Role |
|---|---|
| `src/agent_state_workshop/exercises.py` | The three student functions. |
| `src/agent_state_workshop/service.py` | Application services shared by CLI and GUI, operation log, locking. |
| `src/agent_state_workshop/runner.py` | The seven stages, artifacts, and run lifecycle. |
| `src/agent_state_workshop/baseline.py` | The intentionally flawed memory strategy and how it was built. |
| `src/agent_state_workshop/policy.py` | The supplied execution gate (direct SQL over the event table). |
| `src/agent_state_workshop/planners.py` | Simulated planner; Anthropic and OpenAI adapters. |
| `src/agent_state_workshop/telemetry.py` | SDK setup, local JSONL exporter, OTLP export. |
| `src/agent_state_workshop/visualize.py`, `static/` | Offline HTML comparison and the shared trace viewer. |
| `src/agent_state_workshop/gui/` | Optional FastAPI server and plain HTML/CSS/JS client. |
| `fixtures/` | Event history, follow-ups, expected state, the flawed summary. |
| `solutions/` | Reference solution and example runs (instructors). |
| `tests/` | Domain, process-resume, trace, visualization, and interface tests: `uv run pytest`. |
