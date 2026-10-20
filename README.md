# From prompt context to agent state

This exercise is about what an AI application must keep track of when a task runs longer than one request. A language model is stateless. Every call starts from nothing, so whatever continuity the user experiences comes from the application: what it sends the model, what it stores, and how it updates that data as the task changes.

You will work with a travel-planning assistant. Midway through planning, the user changes the party, accepts one part of the plan, and asks for another revision, while a hotel price changes underneath them. You will see what a single well-built request can do, how an agent that replays the whole conversation copes, and what an agent with explicit task state needs from you to keep up.

You will learn to:

- distinguish requirements, user decisions, observations from tools, proposed plans, and remaining work;
- decide which data a change affects, keep what it leaves alone, and mark the rest for reconsideration;
- build the context for a model's next action from stored state rather than from the transcript;
- separate what the model decides from what code calculates and checks.

You will run three implementations, write two functions in one Python file, and compare your state-driven agent with the transcript-driven one.

## The story

A user asks for a three-night trip from RDU to Chicago for two adults, flying out on April 12, 2027. Flights and the hotel together must stay within $1,800, and the user prefers a hotel within a 15-minute walk of the conference venue. The assistant proposes a nonstop flight and a nearby hotel for $1,200.

Then the user says they are bringing their child, so the room must sleep three and every price changes. The assistant finds a larger room and proposes $1,620. The user likes that hotel and asks for an earlier flight. The assistant keeps the hotel and finds a flight that lands at noon instead of 6 p.m., for $1,680. Finally the hotel's quote expires, and the refreshed price is $180 higher. The same plan would now cost $1,860, which is over budget. A good assistant keeps the hotel the user chose, finds a cheaper flight that still lands before 6 p.m., discloses the connection and the price change, and asks the user to reconfirm the hotel at its new price.

Every price comes from a small synthetic inventory, and an independent validator checks each plan before the user sees it. The assistant proposes plans only; it cannot book or charge anything.

Everything runs offline once installed. You need no API keys or network access, because a deterministic simulator stands in for the model. The command line covers every step, and an optional local web interface offers the same controls.

## Setup

Install [uv](https://docs.astral.sh/uv/), then run:

```bash
uv sync --frozen
```

```bash
uv run workshop doctor
```

The project is tested on Python 3.12, which uv installs if needed. `doctor` checks the installation and reports the optional extras (the web interface and live-model keys) without requiring them.

## Working through the exercise

You edit one file, [workshop/exercises.py](workshop/exercises.py). Every run and every check reloads it, so you never restart anything after an edit. Each command below writes a session directory under `runs/`.

### 1. See what one request can do

```bash
uv run workshop run --implementation one-shot --scenario initial --planner simulated --out runs/one-shot
```

The one-shot planner receives the request and an inventory snapshot that the application prepared, and it returns one plan: flight `f1` and hotel `h11` for $1,200. Open `runs/one-shot/report.html` in a browser and expand "exact model input" to see everything the model received.

A one-shot call can handle the changes too, if someone restates the whole brief every time:

```bash
uv run workshop run --implementation one-shot --scenario changing-trip --planner simulated --out runs/one-shot-changes
```

```bash
uv run workshop run --implementation one-shot --scenario changing-trip --restated-brief --planner simulated --out runs/one-shot-restated
```

Without `--restated-brief`, each follow-up message arrives alone, and the planner asks for the trip details. With it, the calling code rebuilds the brief before each call, and the plans come out right.

Some questions to consider:

- Who reconstructs the current requirements in the restated version, and where does that code get them?
- What would that code need to know about the hotel the user accepted?

### 2. Run the transcript-driven agent

```bash
uv run workshop run --implementation transcript --scenario changing-trip --planner simulated --out runs/transcript
```

This agent receives the system instructions, the whole conversation, every earlier tool call and result, and each new notification, on every step. It has no other memory. It reaches the reference plans: $1,200, $1,620, $1,680, and finally $1,740 with a request to reconfirm the hotel.

Look at how its input grows:

```bash
uv run workshop session show runs/transcript model-input --index 12
```

Model call 12 is the first step after the quote expired. Everything the agent knows sits somewhere in that history, and the model must work out which facts still apply.

### 3. Run the starter state-driven agent

```bash
uv run workshop run --implementation state --strategy student --scenario changing-trip --planner simulated --out runs/state
```

This agent keeps explicit task state and sends the model a context built from that state, plus a tool for reading the retained conversation when it needs exact wording. The starter version of your two functions is naive on purpose. It records the new party and nothing else, and it sends every quote it has ever seen as current. Turn 0 works. Turns 1 through 3 end incomplete when the agent uses up its eight tool calls without a plan the validator accepts.

Look at the context it sent after the child joined, and at what the change did to the state:

```bash
uv run workshop session show runs/state model-input --index 5
```

```bash
uv run workshop session show runs/state diff --index 6
```

The quotes in `current_quotes` were issued for two adults. The validator rejects every plan built from them with `quote_scope_mismatch`, and the agent keeps trying.

Some questions to consider:

- Which facts in the state are still true after the child joins, and which no longer apply?
- What did the hotel acceptance in turn 2 change in the state? Where would you expect to find it?

### 4. Write the two functions

The first function in `exercises.py`, `apply_change(state, change)`, takes the current task state and one normalized change and returns the new state. A change is a typed record such as `PartySizeChanged` or `QuoteExpired`, already resolved by the application, so you never parse the user's words. The second, `build_context(state, next_step)`, decides what the model sees before its next action. The comments in the file mark six TODOs and list the helpers you can use.

Check your progress as often as you like:

```bash
uv run workshop check --strategy student
```

Each check prints `PASS`, `NOT YET`, or `TODO`, with a detail line that says what your function produced. The summary at the end counts them, so the starter shows `3 pass, 15 not yet`. Checks that do not pass yet are the normal state of unfinished work. `ERROR in exercises.py` names a line in your code that raised an exception, and `SETUP PROBLEM` means something outside your file broke. The checks also replay the scenario with your functions, so you see how the agent behaves with them.

[docs/workshop.md](docs/workshop.md) lists the rules a complete solution follows, if you want them beside you.

### 5. Compare the two agents

When the checks pass, run your version:

```bash
uv run workshop run --implementation state --strategy student --scenario changing-trip --planner simulated --out runs/state-fixed
```

```bash
uv run workshop compare --runs runs/transcript runs/state-fixed --output submissions/comparison.html
```

Open `submissions/comparison.html`. It shows each turn side by side: the change, every step each agent took, what each sent the model, and the plan it presented. Both agents reach the same plans, and the differences lie in how they got there. With the reference solution, the state agent sends about two-thirds as many characters as the transcript agent, and its tools differ. After the child joins, it refreshes the quote for the flight it had proposed, while the transcript agent searches all flights again.

Some questions to consider:

- Where is the user's hotel choice recorded in each agent, and what happened to it when the quote expired?
- Which model input shows the difference between the history and the current working context?
- Which tool did each agent call right after the quote expired, and what in its input justified that choice?
- Which conclusions did code check rather than trust from the model? Look for `validate_plan` calls and the reason codes they return.
- Both agents succeeded. What differences remain in how each one updates and how easily you can inspect it?

The validator's reason codes separate four questions: whether the plan breaks a hard constraint, whether its quotes are still valid, whether it meets the user's preferences, and whether the user must confirm something again. A plan can be valid and still await confirmation.

[docs/findings-template.md](docs/findings-template.md) gathers these questions in one place if you want somewhere to jot notes.

## Stepping through a session

`workshop run` processes the whole scenario. A session lets you take it one move at a time and inspect everything in between. Each scenario change is applied separately, and each agent step is one model decision plus its effect: a tool call, a plan the validator checks, or a question for the user.

```bash
uv run workshop session start my-state --implementation state --strategy student
```

```bash
uv run workshop session step my-state
```

```bash
uv run workshop session status my-state
```

`session run-turn` steps until the current turn ends, `session apply-change` applies the next scenario change, and `session run` finishes the scenario. `session reset` starts the session over with the same settings. Sessions live under `runs/`, and a session started from the command line can be continued in the web interface, and the reverse.

`workshop run --step` runs a scenario but pauses after each change, so you can inspect the state before the agent responds.

## Inspecting from the terminal

| To see | Run |
|---|---|
| The conversation, the current plan, and a state summary | `workshop session status runs/state-fixed` |
| The state after each change | `workshop session show runs/state-fixed state --index N` |
| What one change did to the state | `workshop session show runs/state-fixed diff --index N` |
| What the model received and returned, in full | `workshop session show runs/state-fixed model-input --index N` |
| Every tool call and its result | `workshop session show runs/state-fixed tools` |
| Every validator result | `workshop session show runs/state-fixed validation` |
| The normalized changes | `workshop session show runs/state-fixed changes` |

Prefix each command with `uv run`. Leave out `--index` to see the latest. Add `--json` for the raw record.

## The web interface (optional)

```bash
uv run --extra gui workshop gui
```

The first launch installs FastAPI and uvicorn. The page opens at `http://127.0.0.1:8765/` and accepts connections only from your machine. It has three areas: session and scenario controls on the left; the conversation and the proposed plan in the middle; and an inspector with tabs for state, model input, tool calls, and validation on the right. The Comparison tab shows two sessions side by side.

Both interfaces call the same Python code and write the same files, so you can switch between them at any point. Opening a tab or refreshing the page never changes a session; only the action buttons do.

| Task | Command line | Web interface |
|---|---|---|
| Choose the implementation and planner | `--implementation`, `--planner`, `--strategy` | New session form |
| Apply the next change | `session apply-change` | Add the child, Accept hotel + request earlier flight, Expire the hotel quote |
| Take one agent step | `session step` | Step |
| Finish the scenario | `session run` | Run to completion |
| Inspect state, inputs, tools, and validation | `session show` | Inspector tabs |
| Compare two runs | `compare` | Compare side by side |
| Check your functions | `check --strategy student` | Run exercise checks |
| Start over | `session reset` | Reset this session |

## What the workshop writes

Each session directory under `runs/` holds `session.sqlite`, which stores the change log, the state, the transcript, and every execution record, so either interface can resume it. After every action the workshop also writes readable copies beside it: `manifest.json` (implementation, planner mode, model settings, and fixture clock), `changes.jsonl` (the user changes and inventory notices), `state_snapshots.jsonl` (the state before and after each change, for the state agent only), `transcript.json`, `model_calls.jsonl` (exact model inputs, actions, and any token usage the provider reported), `tool_calls.jsonl`, `plans.jsonl`, `checks.json`, and `report.html`, which opens offline in any browser.

`compare` writes its page to `submissions/comparison.html` by default, and `check` writes `submissions/check-results.json`. `reference_runs/` holds example sessions produced by the reference solution; `scripts/regenerate_reference_runs.sh` rebuilds them.

## Going further

### A live language model

The simulator is a small rule-based planner, so every run is repeatable. Its results show how the data design behaves, not what a model can do. To use a real model, copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or both. Set the model names in `ANTHROPIC_MODEL` and `OPENAI_MODEL`, and confirm your account can use them. If you have run `ant auth login`, the Anthropic adapter uses that profile without a key.

```bash
uv run workshop run --implementation state --strategy student --scenario changing-trip --planner live --provider anthropic --out runs/state-live
```

Both agents use the same instructions, tools, and model settings. Each run records the exact requests, the responses, and token counts when the provider reports them, and keys never leave the backend process. A live model may infer the current state from the transcript and succeed. If it does, that shows transcript context can be enough for this task. Consider what the explicit state still gives you when the data changes or when you need to explain a decision. One run is one sample, so try more than one before you draw conclusions.

### Other scenarios

`--scenario no-compliant` raises the accepted hotel's price so far that no flight brings the trip within budget. The agent should ask the user about the tradeoff instead of presenting a plan that breaks the budget.

## Repository map

| Path | Contents |
|---|---|
| `workshop/exercises.py` | The two functions you edit |
| `workshop/state.py`, `workshop/context.py` | Helpers for updating state and building context |
| `workshop/models.py` | The typed records: requirements, decisions, quotes, changes, plans, and pending work |
| `workshop/tools.py` | The synthetic inventory and the read-only tools |
| `workshop/validation.py` | The validator, which checks plans against the inventory and the change log |
| `workshop/runner.py` | The agent loop, its limits, and the three implementations |
| `workshop/planners.py` | The simulator and the Anthropic and OpenAI adapters |
| `workshop/service.py` | The code both interfaces call |
| `workshop/gui/` | The optional web interface |
| `fixtures/` | The inventory, the fixture clock, and the scenarios |
| `solutions/` | A reference solution |
| `reference_runs/` | Example sessions from the reference solution |
| `tests/` | The test suite (`uv run pytest`) |

## Talk it over

When you finish, talk with the people around you about what this exercise means for managing data in AI applications. Some places to start:

- The transcript agent and your state agent reached the same plans here. In the systems you build, when is replaying the conversation enough, and when does it stop being enough?
- A quote for two adults stayed true but stopped applying when the party changed. Where in your systems does a fact keep its value but lose its relevance, and what marks it as no longer current?
- The user accepted a hotel at one price. Which user decisions in your systems need to be stored as decisions, separate from the data that supported them, and what should happen when that data changes?
- The validator recomputed every total instead of trusting the model's arithmetic. Which conclusions in your systems should code check before a user sees them?
- Explicit state costs schema design, migrations, and code to keep it current. Where would you pay that cost, and where would you avoid it?

Then turn to the AI agent you are building for your final project. Talk through what it needs to remember across turns, which of that is a requirement, a decision, or an observation, what each kind of change should update and what it should leave alone, and how you would see what your agent sent its model before a decision you need to explain.
