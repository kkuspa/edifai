# From Prompt Context to Agent State

Workshop title: The user changed their mind. What should change?

A language model is stateless in every implementation in this repository. The application provides continuity by sending context, persisting data, and updating that data as the task moves along. This workshop makes that concrete with a travel planner whose requirements, user choices, and inventory prices change mid-task.

You investigate three implementations of the same planner, write the two small functions that give an agent explicit task state, and compare a transcript-driven agent with your state-driven agent. Persistent state alone does not make an application an agent, and explicit state does not always produce better answers. The exercise is about deciding what each change affects, what to preserve, and what to reconsider.

The timed exercise takes 20 minutes after setup. Everything runs offline with a deterministic simulated planner. A live model is optional.

Start with [docs/workshop.md](docs/workshop.md) if you are a student. Instructors should read [docs/instructor.md](docs/instructor.md). Your written answers go in a copy of [docs/findings-template.md](docs/findings-template.md).

## Requirements

- macOS or Linux. The per-session lock uses `fcntl`, so native Windows needs WSL.
- [uv](https://docs.astral.sh/uv/). It installs Python 3.12 (the version this repository is tested on and pins in `.python-version`) and every dependency.
- No database server. Sessions live in SQLite files under `runs/`.
- No network access after installation, unless you choose live mode.
- A browser, only if you want the optional GUI.

## Before class

Run these once, with a network connection:

```bash
uv sync --frozen
uv run workshop doctor
```

`uv.lock` pins every dependency, and `uv sync --frozen` installs exactly what the lock file lists.

`doctor` runs offline and makes no network call. It checks that the imports and fixtures load, that a simulated reference run persists to SQLite and completes, that the report files are written, and that your `workshop/exercises.py` imports. It then reports, without contacting any provider, whether live credentials and the GUI extra are present. A healthy machine prints this (the `info` values depend on your environment):

```text
Workshop doctor (offline; no network calls)
  ok   imports and fixtures: inventory-2027-04-v1; scenarios: changing-trip, initial, no-compliant
  ok   persistence and simulated run: reference state agent completed changing-trip
  ok   report generation: changes.jsonl, checks.json, manifest.json, model_calls.jsonl, plans.jsonl, report.html, session.sqlite, state_snapshots.jsonl, tool_calls.jsonl, transcript.json
  ok   student exercise module imports: workshop/exercises.py
  info live credentials configured: anthropic=False openai=False (optional; not contacted)
  info GUI extra not installed (optional): `uv sync --extra gui` to enable `workshop gui`
Ready.
```

`doctor` exits with 0 after printing `Ready.` and with 1 after printing `Problems found; see FAIL lines.`

Installation, provider setup, and dependency work are outside the timed exercise. Finish them before class starts.

### Optional: live mode credentials

Live mode sends the same task to a real model. Copy `.env.example` to `.env` (git ignores it), fill in one provider, and leave the rest alone. The application reads `.env` from the repository root. A variable already set in your shell wins over the file. Credentials stay in the backend process; the GUI only learns whether a provider is configured.

| Variable | Meaning |
|---|---|
| `LLM_PROVIDER` | `anthropic` or `openai`. If blank, the provider whose key is present wins, and Anthropic wins when both are present. `--provider` on the command line overrides it. |
| `ANTHROPIC_API_KEY` | Anthropic key. Leave blank if you ran `ant auth login`; the SDK reads that profile. |
| `ANTHROPIC_MODEL` | Default `claude-opus-5-5`. `claude-sonnet-5-5` is the cheaper choice for a classroom. |
| `ANTHROPIC_EFFORT` | Optional reasoning effort: `low`, `medium`, `high`, `xhigh`, or `max`. Blank uses the API default. |
| `ANTHROPIC_SERVER_FALLBACKS` | `default` enables server-side fallback when a safety classifier declines a request. Blank turns it off. `.env.example` sets it to `default`. |
| `OPENAI_API_KEY` | OpenAI key. |
| `OPENAI_MODEL` | Default `gpt-5.5`. Check it against the models your account can access before class. |
| `OPENAI_REASONING_EFFORT` | Optional: `minimal`, `low`, `medium`, or `high`. Blank uses the API default. |
| `OPENAI_TEMPERATURE` | Leave blank. Many current OpenAI reasoning models accept only the default temperature. |
| `LLM_MAX_TOKENS` | Output token cap per model call, thinking plus reply. Default 16000. |
| `LLM_MAX_RETRIES` | Retries when a reply is not one valid tool call. Default 2, so a step makes at most 3 attempts. |

## The three implementations

All three share one inventory, one set of task rules (`workshop/prompts.py`), and one tool interface. They differ in what they send to the model.

| Implementation | What the model receives | Tools | Memory between turns |
|---|---|---|---|
| One-shot | The task rules and one message: the current user text plus an inventory snapshot of quotes for the current party and dates, prepared by the application. With `--restated-brief`, the calling application also rebuilds the full current brief (requirements, earlier-arrival anchor, accepted hotel) in that message. | None. It returns one plan or one question. | None. Nothing carries over unless the caller restates it. |
| Transcript | The task rules and the complete conversation so far: every user message, every inventory notification, every tool call and result, and every plan shown. Nothing is truncated, and the whole history is replayed on every step. | `search_flights`, `search_hotels`, `refresh_quote`, `validate_plan`, `read_history`, then `present_plan` or `ask_user` | The persisted transcript. No structured task state. |
| State | The task rules and one message rendered from your `build_context(state, step)`: requirements, unknowns, decisions, current quotes, unsuitable items, current proposal, pending work, validator feedback, historical items, and a pointer to `read_history`. | The same as the transcript agent. | Structured task state that your `apply_change` updates. The full transcript is still stored and reachable through `read_history`. |

Four components stay separate in every implementation. Task state holds requirements, choices, observations, the proposal, and pending work. World state (inventory and quote revisions) belongs to the mock tools. The planner chooses a tool call, presents a plan, or asks a question. A deterministic validator checks every candidate against the authoritative fixture and the current requirements before the runner shows a plan as compliant. The validator derives its own copy of the requirements from the change log, so it does not depend on your functions, and editing state cannot make a proposal pass.

A turn ends when the planner presents a validated plan, asks a question, or hits a limit of 8 tool calls or 12 steps. Hitting a limit produces an explicit `incomplete` outcome. The runner never invents a plan.

## Classroom commands

Each command writes a session directory under `runs/`, the default session root. The commands refuse to write over an existing session unless you add `--overwrite`. Even then they delete only the files a session owns (`session.sqlite` and the artifacts listed below), and they refuse any non-empty directory that is not a session.

Initial investigation:

```bash
uv run workshop run --implementation one-shot --scenario initial --planner simulated --out runs/one-shot
uv run workshop run --implementation transcript --scenario changing-trip --planner simulated --out runs/transcript
uv run workshop run --implementation state --strategy student --scenario changing-trip --planner simulated --out runs/state
```

The one-shot run presents plan-001 at $1,200. The transcript run presents four plans, ending with plan-004 at $1,740 and a reconfirmation flag. The state run uses the starter `workshop/exercises.py`, which is naive on purpose. It presents plan-001 and then ends turns 1, 2, and 3 as `INCOMPLETE: tool limit`, because it keeps offering stale two-person quotes as current.

After editing `workshop/exercises.py`:

```bash
uv run workshop check --strategy student
uv run workshop run --implementation state --strategy student --scenario changing-trip --planner simulated --out runs/state-fixed
uv run workshop compare --runs runs/transcript runs/state-fixed --output submissions/comparison.html
```

`check` prints one line per check, writes `submissions/check-results.json`, and exits with 0 only when everything passes. `compare` writes a self-contained HTML file that opens offline. A correct solution reaches the same four plans as the transcript agent. `--strategy reference` runs the reference functions in `solutions/reference.py` instead of yours. That option exists for instructors.

`run` accepts `--step` to pause after each scenario change, print the state changes it caused, and wait for Enter. It needs an interactive terminal. In a script it falls back to running to completion. The `session` commands below give the same inspection without a prompt.

## Step-by-step sessions

`workshop run` plays a whole scenario. The `session` commands move one stage at a time, and they work on the same directories under `runs/`. Session `state` is the directory `runs/state`.

```bash
uv run workshop session start my-state --implementation state --strategy student
uv run workshop session run-turn my-state       # the agent answers the initial request
uv run workshop session apply-change my-state   # the child joins; no planning happens yet
uv run workshop session show my-state diff      # what that change did to the state
uv run workshop session step my-state           # one planner decision and its effect
uv run workshop session show my-state model-input
uv run workshop session run-turn my-state       # finish the turn
```

`start` only applies the initial request. The agent has not answered it until you run a step or a turn, and `apply-change` refuses to run before that.

| Command | Effect |
|---|---|
| `session start NAME` | Create a session. Takes the options of `run` except `--out` and `--step`, plus `--overwrite`. |
| `session list` | List sessions under the root. |
| `session status NAME` | Show phase, conversation, current plan, and a state summary. Reads only. |
| `session apply-change NAME` | Apply the next scenario change: add the child, then accept the hotel and ask for an earlier flight, then expire the hotel quote. |
| `session step NAME` | One planner decision and its effect. `--count N` takes N steps. |
| `session run-turn NAME` | Step until the current turn ends. |
| `session run NAME` | Apply changes and step until the scenario is complete. |
| `session reset NAME` | Discard progress and restart with the same settings. |
| `session export NAME` | Rewrite the session's artifact files. Every operation already does this. |
| `session show NAME VIEW` | Inspect recorded data. `VIEW` is `state`, `diff`, `model-input`, `tools`, `proposals`, `validation`, `transcript`, or `changes`. `--index N` picks a state snapshot or a model call (the latest by default). |

`apply-change`, `step`, `run-turn`, `run`, and `reset` accept `--op-id`; repeating an ID returns the stored result without running anything again. All of them except `reset` also accept `--expected-version`, which refuses the command if the session has moved on. `status`, the mutating commands, and `show` accept `--json`. The global `--root DIR` option sets a different session root, and it goes before the subcommand: `uv run workshop --root other-runs session list`.

A session is always in one of three phases. In `ready`, the agent may take a step. In `awaiting_change`, the turn has ended and the next scenario change may be applied. In `complete`, every scenario turn has been processed.

## Optional GUI

The GUI is a local web page for the same operations. It is plain HTML, CSS, and JavaScript served by a small FastAPI backend. It needs no build step and loads nothing from the network. You can finish the whole workshop without it.

```bash
uv sync --frozen --extra gui
uv run workshop gui
```

Open http://127.0.0.1:8765. If the port is busy, run `uv run workshop gui --port 8766` and open that address instead. Start it from the repository root so that exports land in `submissions/`.

A plain `uv sync` removes the GUI packages again. Re-run the command above with `--extra gui` if `doctor` stops listing the GUI extra as installed.

You can create a session in either interface and continue it in the other, because both read the same directories under `runs/`. The GUI does not watch the disk. Reload the page after you change a session from the CLI.

The GUI and the CLI call the same Python services in `workshop/service.py`. Opening panels, switching tabs, reloading, and polling send read-only requests. Work happens only when you press an action button. Every action carries an operation ID, so a repeated request returns the stored result instead of running twice, and it carries the session version the page last saw, so a stale page gets an HTTP 409 and must refresh. The server binds to 127.0.0.1, answers only requests addressed to localhost, and requires a custom header on every action request. [docs/workshop.md](docs/workshop.md) maps each exercise step to its CLI command and its GUI controls.

## Live mode

Add `--planner live` to `run` or `session start`, and choose a provider with `--provider anthropic` or `--provider openai`. Without `--provider`, the choice follows `LLM_PROVIDER` and then whichever key is present.

```bash
uv run workshop run --implementation transcript --scenario changing-trip --planner live --provider anthropic --out runs/transcript-live
```

Both agents use the same planner interface, task rules, tool definitions, and settings. The provider, model, effort, token cap, and retry limit are recorded in `manifest.json` and with every model call, and the GUI and report show them.

- The Anthropic adapter sends `tool_choice` set to `auto` with parallel tool use disabled, so each reply carries one tool call. It does not force a tool, because current Claude models reject forced tool choice.
- The OpenAI adapter sends `tool_choice` set to `required` with parallel tool calls disabled.
- A reply that is not one valid tool call is retried up to `LLM_MAX_RETRIES` times with the error fed back. After that, the turn ends as `incomplete` and the errors stay in `model_calls.jsonl`.
- A provider error (network, authentication, rate limit, unknown model) also ends the turn as `incomplete`, with the error recorded. The session cannot retry that turn, so reset the session and run it again.
- Token usage and latency appear only for live runs. The simulator reports `usage: null` and does not invent either number.

Simulated runs carry the label `planner_mode=simulated` everywhere they appear. The simulator reads the context a planner would read and applies fixed rules, so its results show how the data design behaves. They do not measure what any language model can do. A live run is one sample from a stochastic system, and a single run does not establish that one implementation is more reliable than another.

## Run artifacts

Every run directory holds these files, and each one comes from the recorded execution.

| File | Contents |
|---|---|
| `manifest.json` | Implementation, planner mode, provider and model settings, strategy, scenario, fixture revision, fixture clock, run ID, and turn limits. |
| `changes.jsonl` | The user changes and world notifications that were applied, plus the world's repricing event. |
| `state_snapshots.jsonl` | State before and after every transition. Present for the state implementation only. |
| `transcript.json` | Every retained message and tool interaction. |
| `model_calls.jsonl` | The exact model input of each call (system text, messages, tools, and the built context), the returned action, attempts, errors, latency, and reported token usage. |
| `tool_calls.jsonl` | Tool arguments and responses with their IDs. |
| `plans.jsonl` | Presented proposals and rejected drafts, with totals, validation, and confirmation status. |
| `checks.json` | Independent checks of the recorded run against the fixture and the change log. |
| `report.html` | A self-contained, offline report of the run. |
| `session.sqlite` | The database behind the session. The CLI and the GUI resume from it. |

`checks.json` checks one recorded run. It is not the same as `workshop check`, which exercises your two functions and then replays two simulated scenarios.

`workshop compare` writes one HTML file with a turn-by-turn timeline, the runs side by side, expandable exact model inputs and tool results, the preserved decision, every state transition, tool-call counts, input sizes, and the independent checks. It keeps hard constraints, quote freshness, preferences, and user confirmation as separate lines. When a provider reports no usage, the report says so.

## Repository layout

| Path | Responsibility |
|---|---|
| `README.md` | Setup, overview, and classroom quick start. |
| `pyproject.toml`, `uv.lock` | Dependencies, the optional `gui` extra, and the `workshop` command. |
| `.env.example` | Optional live-model configuration. |
| `examples/one_shot.py` | The bounded prompt and context example. Add `--restated-brief` to see a caller rebuild the brief. |
| `examples/transcript_agent.py` | Full-history agent entry point. |
| `examples/state_agent.py` | Explicit-state agent entry point. |
| `workshop/models.py` | Shared typed contracts. |
| `workshop/exercises.py` | Your two functions: `apply_change` and `build_context`. |
| `workshop/state.py` | Supplied helpers that update state (mark a quote, add a task, store an observation). |
| `workshop/context.py` | Supplied context sections and the text renderer. |
| `workshop/prompts.py` | Task instructions shared by every implementation. |
| `workshop/runner.py` | The resumable action and observation loop, its phases, and the turn limits. |
| `workshop/store.py` | SQLite persistence for state, transcript, and records. |
| `workshop/tools.py` | The synthetic world, the fixture clock, and the read-only tools. |
| `workshop/validation.py` | The independent arithmetic and constraint checker. |
| `workshop/planners.py` | The deterministic simulator and the Anthropic and OpenAI adapters. |
| `workshop/service.py` | Shared services for the CLI and the GUI: sessions, locks, operation IDs, versions, exports. |
| `workshop/report.py` | Artifact export and HTML reports. |
| `workshop/checks.py` | Exercise checks, scenario checks, and failure classification. |
| `workshop/cli.py` | The `workshop` command. |
| `workshop/gui/` | The optional GUI: `app.py` (FastAPI routes) and `static/` (`index.html`, `style.css`, `app.js`). |
| `fixtures/` | Inventory, the fixture clock, and typed scenarios in `fixtures/scenarios/`. |
| `tests/` | Scenario, update, isolation, and reporting tests. Run them with `uv run pytest`. |
| `solutions/` | Reference versions of the two student functions. |
| `scripts/` | `regenerate_reference_runs.sh`. |
| `reference_runs/` | Small pre-generated runs for inspection (see the last section). |
| `docs/` | `workshop.md` (timed instructions), `instructor.md` (reference behavior and troubleshooting), `findings-template.md` (your responses). |
| `runs/`, `submissions/` | Generated sessions and your submission files. Git ignores both. |

## The fixed scenario

The inventory is synthetic. It is not travel advice and not a source of real prices.

- Dates: depart 2027-04-12 and return 2027-04-15, which is three nights. The destination time zone is America/Chicago, and "Chicago" covers ORD and MDW.
- The fixture clock starts at 2027-03-15 14:00 UTC, independent of your computer's time. The scenario sets it to 10, 20, and 40 minutes after the start at turns 1, 2, and 3. Nothing sleeps. A quote lives 120 minutes of fixture time.
- Budget: the flights and the hotel together must not exceed $1,800. A total of exactly $1,800 complies. Prices include all modeled taxes and fees, and the budget covers nothing else.
- Walking distance to the conference venue (15 minutes or less) is a preference. Budget, occupancy, and dates are hard constraints.
- Every flight product is round trip. Every hotel quote covers all three nights. A child needs a flight seat and counts toward hotel occupancy.
- "Earlier flight" means an outbound arrival in Chicago strictly before the arrival of the flight in the proposal the user was looking at when they asked. That reference is 18:00 here, and it never moves, even after the agent proposes a different flight.
- A quote is valid only for the party and dates it was issued for. A two-adult quote never prices two adults and a child, even for the same item.
- The assistant proposes plans only. The tools are read-only, and nothing books, charges, or reserves.
- The scenario ships its changes already resolved. "That hotel looks good" resolves to the hotel in the proposal the run displayed most recently. You do not parse natural language.

There are three scenarios. `initial` holds only the first request and feeds the one-shot example. `changing-trip` is the canonical sequence: the child joins, the user accepts the hotel and asks for an earlier flight, and the hotel's quote expires and reprices. `no-compliant` is the same sequence with a bigger price jump (the refreshed hotel costs $1,200), so no combination fits the budget and a correct agent asks a targeted question.

### Reference inventory

| Item | Fixture properties | Price |
|---|---|---|
| Flight f1 | Outbound arrives 18:00, nonstop | $300 per traveler round trip |
| Flight f2 | Outbound arrives 12:00, nonstop | $320 per traveler round trip |
| Flight f3 | Outbound arrives 15:00, one stop (CLT, 55-minute layover) | $280 per traveler round trip |
| Hotel h11 | Sleeps 2, 8-minute walk | $600 for the stay |
| Hotel h17 | Sleeps 3, 10-minute walk | $660 for two adults, $720 for two adults and a child |
| Hotel h17, refreshed | Same room, new quote revision | $900 for two adults and a child |

The inventory also holds distractors: f4 arrives early but costs $540 per traveler, f5 has two seats left, f6 flies on the wrong day, h22 is a 25-minute walk, h30 costs $1,150, and h41 is sold out.

### Reference arithmetic

| Stage | Combination | Total | Meaning |
|---|---|---:|---|
| Initial | f1 x 2 + h11 | $1,200 | A valid first proposal (plan-001). |
| Child added | f1 x 3 + h17 | $1,620 | h11 sleeps 2, so h17 replaces it, and the flight now prices three travelers (plan-002). |
| Earlier flight requested | f2 x 3 + h17 | $1,680 | Keeps the hotel and arrives at 12:00, before 18:00 (plan-003). |
| Hotel refreshed | f2 x 3 + refreshed h17 | $1,860 | Over budget. The validator returns `over_budget`, so this combination never reaches the user. |
| Revised alternative | f3 x 3 + refreshed h17 | $1,740 | Arrives at 15:00 with a connection, and the hotel now costs $900 instead of $720. Presented as plan-004 with reconfirmation required. |

These are reproducible reference choices, not the only correct answers. A different combination passes if it meets the requirements and discloses its tradeoffs.

## Regenerating the reference runs

`reference_runs/` holds a one-shot run, a transcript run, two reference state runs (`changing-trip` and `no-compliant`), and a comparison page. They use the simulated planner and the reference functions. Regenerate them with:

```bash
bash scripts/regenerate_reference_runs.sh
```

The output is deterministic except for generated run IDs. The `*.sqlite` files are git-ignored, so a fresh clone has the JSON, JSONL, and HTML artifacts but not the databases. To open a reference run in the GUI or pass it to `workshop compare`, regenerate it first. You still generate your own comparison from your own runs; the reference runs exist for inspection only.
