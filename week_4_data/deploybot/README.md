# Agent memory, state, and traces

This exercise is about what an AI agent knows when it resumes work it set aside. You will work with a small deployment assistant that paused partway through a release. While it was stopped, the code changed and the tests failed. When it resumes, its saved memory says the opposite, and it proposes to deploy code that should not ship.

You will learn to:

- distinguish an authoritative event log from the state derived from it and from a prose summary of it;
- keep each fact attached to what it describes, so a test result stays with its commit and an approval stays with its commit and environment;
- rebuild an agent's working state after a restart instead of trusting a stale copy;
- read OpenTelemetry traces to see what data an agent received, what it proposed, and what actually ran.

You will run the flawed assistant, repair three functions in one Python file, and compare traces of the old and new behavior. Then you will feed the assistant new events and watch its decision change.

## The story

The assistant tested commit `a1`, and the required tests passed. The release lead approved `a1` for staging. The assistant then paused to wait for a deploy window, saved a short summary of where things stood, and exited.

While it was stopped, a teammate pushed commit `b2`, and the required tests failed on it.

The user returns and asks, "Can you finish the deployment?" The assistant reads its summary ("tests passed and deployment was approved"), sees that the current commit is `b2`, and proposes to deploy `b2` to staging. A separate policy gate reads the event history directly and blocks the deployment.

The gate prevents the damage, but the assistant still believes something false. Your work is to give it accurate data so that it proposes the right action itself. The policy is explicit. A request to finish the deployment resumes the task. It grants no new approval and changes neither the approved commit nor the approved environment.

Everything runs offline once installed. You need no API keys, Docker, or network access. The command line covers every step, and an optional local web interface offers the same controls.

## Setup

Install [uv](https://docs.astral.sh/uv/), then run:

```bash
uv sync --frozen
```

```bash
uv run workshop doctor
```

The project is tested on Python 3.12, which uv installs if needed. `doctor` checks the installation and reports optional extras (the GUI, Jaeger, LLM keys) without requiring them.

## Working through the exercise

You edit one file, [src/agent_state_workshop/exercises.py](src/agent_state_workshop/exercises.py). Each run and each check reloads it, so you never restart anything after an edit.

### 1. Watch the baseline fail

```bash
uv run workshop prepare --scenario stale-resume --db .workshop/base.sqlite
uv run workshop run --db .workshop/base.sqlite --strategy baseline --planner simulated --out runs/before
uv run workshop show --run runs/before --artifact context
```

`prepare` reproduces the pause with a real process boundary. A child process records the first 20 events, saves the assistant's summary, and exits. Only then does the parent process append events 21 through 40, the changes that happened while the assistant was stopped.

In the context, look at `current_test` and `matching_approval`. Which commit does each describe, and which event supports it? Compare the context's `through_sequence` with the number of events in the history. The opening comment in [baseline.py](src/agent_state_workshop/baseline.py) explains how the baseline built this memory.

### 2. Repair the state and the context

The first function in `exercises.py` turns the event history into working state. The second selects the facts that apply to the current decision.

`project_state(events)` replays the history. A head change sets the current commit but leaves earlier facts intact. A test result belongs to one commit and one suite, and the highest run number wins regardless of arrival order. An approval belongs to one commit and one environment. Every record keeps the ID of the event it came from.

`build_context(state, target_environment)` gives the planner the facts that apply to the current commit and target environment. Facts about other commits stay in the history with their scope intact. When a fact is absent, the context reports it as unknown or missing and never invents it. When several blockers apply, the context lists them all.

Check your progress as you go:

```bash
uv run workshop check --strategy student
```

The output names the next unfinished step and explains any mismatch. When the checks for the first two functions pass, run your version against a fresh copy of the same history:

```bash
uv run workshop prepare --scenario stale-resume --db .workshop/fixed.sqlite
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --out runs/after
```

The assistant should now report that the tests failed for `b2`. If `project_state` or `build_context` is unfinished, `run --strategy student` stops and names the unfinished step. An unfinished annotation does not stop the run; the trace viewers flag it instead.

### 3. Record the evidence in the trace

The third function, `annotate_context_span`, attaches the facts behind the decision to the `context.build` span: the target, the test result and its source event, the approval status, and the blockers. [docs/telemetry-schema.md](docs/telemetry-schema.md) lists the attribute names.

The workshop never overwrites a run directory, so write the next run to a new one:

```bash
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --out runs/after-2
uv run workshop trace --run runs/after-2 --span context.build
```

### 4. Compare the two runs

```bash
uv run workshop compare --before runs/before --after runs/after-2
uv run workshop visualize --before runs/before --after runs/after-2 --output submissions/comparison.html
```

`compare` prints the comparison in the terminal. `visualize` writes a page that opens in any browser without a network connection. Click the `context.build`, `planner.propose`, and `policy.validate` spans in each trace.

Some questions to consider:

- What did the baseline assume, and where in its context can you see the assumption?
- Which event establishes the current commit?
- Why does the earlier passing test fail to show that `b2` is ready?
- Why does the earlier approval fail to authorize this deployment?
- Did the deployment tool run in either case? How does the trace show it?

A trace shows what data the application supplied, what the planner proposed, and what the application executed. It does not show how a model reasoned.

### 5. Follow the story forward

The tests pass on a second run for `b2`:

```bash
uv run workshop append --db .workshop/fixed.sqlite --fixture tests-pass-b2
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --out runs/tests-pass
```

Does a passing test authorize the deployment? Now the release lead approves `b2` for staging:

```bash
uv run workshop append --db .workshop/fixed.sqlite --fixture approve-b2-staging
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --out runs/approved
```

Finally, ask for production instead of staging and see what the approval covers:

```bash
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --target-environment production --out runs/production
```

With the exercises complete, the runs behave as follows:

| Run | Proposal | Gate | Executed |
|---|---|---|---|
| `runs/before` (baseline) | deploy `b2` to staging | blocked: tests failed, no approval for `b2` | nothing |
| `runs/after-2` | report the test failure for `b2` | allowed, no side effect | nothing |
| `runs/tests-pass` | request approval for `b2` in staging | allowed, no side effect | nothing |
| `runs/approved` | deploy `b2` to staging | allowed | mock deployment with a receipt |
| `runs/production` | request approval for `b2` in production | allowed, no side effect | nothing |

If you want a record of your runs, `workshop export --before runs/before --after runs/after-2` gathers the comparison page and a text index of runs, spans, and event IDs into `submissions/`.

## Stepping through a run

A run has seven stages:

```
state.load -> memory.project -> context.build -> planner.propose -> policy.validate -> tool.deploy -> state.save
```

`workshop run` executes all of them. `workshop step` executes one per call, and each call can be a separate process. Every stage reads its inputs from the run directory and writes its outputs there, so all the stages of a run still form one trace with one root span. `tool.deploy` runs only when the gate allows a deployment. Otherwise the stage is skipped, and the run has no deployment span and no receipt.

```bash
uv run workshop prepare --db .workshop/stepping.sqlite
uv run workshop step --db .workshop/stepping.sqlite --strategy baseline
uv run workshop step --db .workshop/stepping.sqlite
uv run workshop status --db .workshop/stepping.sqlite
```

## Inspecting from the terminal

| To see | Run |
|---|---|
| The event history | `workshop events --db .workshop/fixed.sqlite` |
| The state a run derived | `workshop show --run runs/after-2 --artifact state` |
| What changed since the last saved state | `workshop show --run runs/after-2 --artifact state-diff` |
| Exactly what the planner received | `workshop show --run runs/after-2 --artifact planner-input` |
| Which tools ran | `workshop show --run runs/after-2 --artifact tools` |
| What the gate checked | `workshop show --run runs/after-2 --artifact gate` |
| Span tree, durations, and attributes | `workshop trace --run runs/after-2 --attributes` |
| Field-level differences between two runs | `workshop diff --before runs/before --after runs/after-2` |

## The web interface (optional)

```bash
uv run --extra gui workshop gui
```

The first launch installs FastAPI and uvicorn. The page opens at `http://127.0.0.1:8765/` and accepts connections only from your machine. It has three areas: controls for the session and the run; the conversation, which shows the proposal, the gate outcome, and the executed action separately; and an inspector with tabs for state, model input, tools, validation, the trace, events, and checks. Below them, you can compare any two runs side by side.

Both interfaces call the same Python code and write the same files, so you can switch between them at any point. A session prepared in the terminal appears in the browser, and a run started in the browser can be finished in the terminal.

| Task | Command line | Web interface |
|---|---|---|
| Choose the implementation and planner | `--strategy`, `--planner`, `--provider` | Implementation, Planner mode, Provider |
| Start a scenario | `prepare --db .workshop/NAME.sqlite` | Session name, then Prepare |
| Apply a new event | `append --fixture NAME` | Fixture changes |
| Advance one stage | `step` | Step one stage |
| Run to the end | `run`, or `step --until-done` | Run to completion, Finish active run |
| Inspect a run | `show`, `trace`, `diff` | Inspector tabs |
| Compare runs | `compare`, `visualize` | Compare runs |
| Check your exercises | `check --strategy student` | Run exercise checks |
| Start a session over | `reset --db ...` | Reset session |

## What the workshop writes

Session databases live in `.workshop/`. Each holds the append-only event history, the derived snapshots, the baseline's saved summary, a record of runs, and mock deployment receipts. Each run writes one directory under `runs/` containing its derived state (`state.json`), the planner's input (`context.json`, `planner-input.json`), the proposal (`proposal.json`), the gate's decision (`gate.json`), the tool calls (`tools.json`), a receipt when a deployment happened (`receipt.json`), and the trace (`spans.jsonl`). The trace file is this workshop's own JSON serialization of OpenTelemetry SDK spans, one span per line. It is not OTLP JSON.

## Going further

### Jaeger

To see the same traces in Jaeger's waterfall view, start Jaeger and export to it:

```bash
docker compose up -d jaeger
```

```bash
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:4318/v1/traces uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --out runs/jaeger
```

```bash
uv run workshop trace-url --run runs/jaeger
```

The Jaeger UI is at `http://localhost:16686`. It receives the same trace and span IDs as the local trace file. If Jaeger is down, the run still completes and keeps its local trace.

### A live language model

The default planner is a small, transparent simulator, so every run is repeatable. To use a real model instead, copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or both. Set the model names in `ANTHROPIC_MODEL` and `OPENAI_MODEL`, and confirm your account can use them. `OPENAI_BASE_URL` points the OpenAI adapter at any compatible server, including a local one.

```bash
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner live --provider anthropic --out runs/live
```

The prompt stays the same for every strategy; only the data changes. Each run records the exact request, the response, token counts when the provider reports them, and a `model.generate` span for each attempt. API keys never leave the process environment. A live model may notice the stale summary and decline to deploy. Compare what you observe with what the simulator shows, and consider why they differ.

## Repository map

| Path | Contents |
|---|---|
| `src/agent_state_workshop/exercises.py` | The three functions you edit |
| `src/agent_state_workshop/baseline.py` | The flawed memory strategy and how it was built |
| `src/agent_state_workshop/policy.py` | The policy gate, which queries the event history directly |
| `src/agent_state_workshop/runner.py` | The seven stages and the files each run writes |
| `src/agent_state_workshop/planners.py` | The simulated planner and the Anthropic and OpenAI adapters |
| `src/agent_state_workshop/telemetry.py` | OpenTelemetry setup and the local trace exporter |
| `src/agent_state_workshop/service.py` | The code both interfaces call |
| `src/agent_state_workshop/gui/` | The optional web interface |
| `fixtures/` | The event history, follow-up events, and the flawed summary |
| `solutions/` | A reference solution and example runs |
| `tests/` | The test suite (`uv run pytest`) |

## Talk it over

When you finish, talk with the people around you about what this exercise means for managing data in AI applications. Some places to start:

- The assistant's summary was a cache of the event history. What is the source of truth in the systems you build, and how would you notice that an agent's copy had gone stale?
- Where in your own systems does a fact lose its scope on the way into a prompt? A test result might lose its version, or a permission its environment.
- What belongs in a prose summary, and what must stay structured so code can query and check it?
- The gate here is ordinary code outside the model. Which decisions in your systems need a check like that?
- If an agent made a bad call last week, what would you need in its traces to explain why?

Then turn to the AI agent you are building for your final project. Talk through what it remembers between sessions, where that memory comes from, how you would rebuild it, which facts must stay tied to their scope, and what you would record so that you can explain its decisions later.
