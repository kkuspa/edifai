# Workshop instructions: the user changed their mind

You have 20 minutes after setup. Setup means `uv sync --frozen` and a `uv run workshop doctor` that ends with `Ready.` Work from the repository root. Every command below has a GUI equivalent, and the GUI is optional. The table at the end maps one to the other.

## The assignment

> You have a travel assistant that can generate a useful initial plan. Now the user changes the brief, accepts one part of the plan, and asks for revisions while inventory prices change.
>
> Design the state the application needs to continue the task. Implement two small functions: one that applies a change, and one that builds context for the next model invocation.
>
> Preserve requirements and choices that still apply. Mark affected options and quotes for reconsideration. Give the agent enough information to choose its next tool call and produce a valid revised plan.
>
> Compare a transcript-driven agent with your state-driven agent using the supplied scenario. Show what each implementation supplied to the model and what happened next.

The scenario starts with this request: "Plan a three-night trip from RDU to Chicago for two adults. Keep round-trip flights and the hotel under $1,800 total. Prefer a hotel within a 15-minute walk of the conference venue." Then three things happen, in order.

1. The party changes: "Actually, we're bringing our child. We need a room that accommodates three."
2. The user accepts a hotel and asks for revisions: "That hotel looks good, but can you find an earlier flight?" The application has already resolved "that hotel" to the hotel in the proposal the user was looking at, so you receive two typed changes (`HotelAccepted` and `EarlierFlightRequested`) with explicit IDs.
3. Inventory changes: the accepted hotel's quote expires, and a refreshed quote costs more.

The README lists the fixed facts: dates, the budget rule, what "earlier" means, and the reference arithmetic. Read its "The fixed scenario" section if you have not.

## The 20 minutes

| Minutes | You do | Evidence |
|---|---|---|
| 0 to 4 | Inspect the three implementations and run the initial change sequence. Predict which data each change affects. | A short prediction table |
| 4 to 12 | Complete the two functions in `workshop/exercises.py`. | Current quotes and decisions stay correctly scoped |
| 12 to 17 | Run the changes with your functions and generate the comparison report. | Actual inputs, tools, totals, and preserved choices |
| 17 to 20 | Complete the findings and discuss. | One selective invalidation and one code and model boundary |

## Minutes 0 to 4: inspect and predict

Spend the first minute on the prediction table. Fill it in before you run anything. "Data" means requirements, quotes, the user's decisions, the current proposal, and pending work.

| Change | What I expect to change | What I expect to stay | Tool I expect the agent to call next |
|---|---|---|---|
| 1. A child joins the party | | | |
| 2. The user accepts the hotel and asks for an earlier flight | | | |
| 3. The hotel quote expires and reprices | | | |

Then run the three implementations. If a session from an earlier run already exists at that path, add `--overwrite` to replace it. `--overwrite` replaces only an existing session and deletes only the files the session owns; it refuses a directory that holds anything else.

```bash
uv run workshop run --implementation one-shot --scenario initial --planner simulated --out runs/one-shot
uv run workshop run --implementation transcript --scenario changing-trip --planner simulated --out runs/transcript
uv run workshop run --implementation state --strategy student --scenario changing-trip --planner simulated --out runs/state
```

In the GUI, run `uv run workshop gui`, open http://127.0.0.1:8765, and open the "New session" form. Create each session with these settings, then press "Run to completion".

| Session name | Implementation | Planner | Strategy | Scenario |
|---|---|---|---|---|
| `one-shot` | One-shot | simulated | any | `initial` |
| `transcript` | Transcript-driven | simulated | any | `changing-trip` |
| `state` | Structured state | simulated | student (workshop/exercises.py) | `changing-trip` |

To go slower, press "Run turn" instead (the agent finishes the current turn), then press the next button under "Scenario changes", then "Run turn" again. "Step" takes one planner decision at a time.

What to look at:

- The one-shot agent answers the first request, so it presents plan-001 at $1,200. Run `uv run workshop run --implementation one-shot --scenario changing-trip --planner simulated --out runs/one-shot-bare` to see what it does with the later changes. It asks a question each time, because nobody restated the brief. Add `--restated-brief` and it plans correctly, which raises the question of who rebuilds the brief.
- The transcript agent presents four plans and ends with plan-004 at $1,740, flagged for reconfirmation. Run `uv run workshop session status transcript` to read the conversation.
- The starter state agent presents plan-001 and then ends turns 1, 2, and 3 as incomplete. The end of its output shows why: nine two-person quotes are still current, `invalidated: none`, and `hotel decision: none`. Run `uv run workshop session show state tools` and read the `validate_plan` results. Each one reports `quote_scope_mismatch`, because the agent keeps proposing quotes that were issued for two adults.
- Compare what you predicted with what the transcript agent did. In the GUI, open the "State" tab of each session and note which two implementations have no structured state to show.

## Minutes 4 to 12: implement

Open `workshop/exercises.py`. It contains `apply_change` and `build_context`, and the starter versions run but are naive. Six TODO comments mark your decisions. About 30 to 60 lines of code complete them. The comments and the module docstring name the helpers to call.

The application calls your functions on its own schedule.

- `apply_change(state, change)` runs once for every change, in order: each user change, each world notification, each tool result (including `validate_plan` results), each plan shown, and each question asked. Return a new `TaskState` and leave the one you received alone. Duplicate changes, sequence numbers, and revisions of the state itself are handled for you.
- Tasks you open close themselves. When new evidence arrives (a quote for the item, a flight search with the matching arrival time, a validation), a supplied helper marks the task done. You open tasks and never write the closing code.
- `build_context(state, next_step)` runs before every model call of the state agent. `next_step` says why the call is happening. It is never an answer key.

Do not edit the fixtures, the tools, or the validator. The validator builds its own view of the requirements from the change log, so changing state cannot make a proposal pass.

Edit, then run the checks:

```bash
uv run workshop check --strategy student
```

In the GUI, set "Strategy" under "Checks and evidence" to `student`, press "Run exercise checks", and read the "Check results" tab. Both interfaces reload `workshop/exercises.py` on every run, so there is nothing to restart.

### What each TODO needs

| TODO | The branch | Checks that cover it |
|---|---|---|
| 1 | `PartySizeChanged` | `child_preserves_requirements`, `old_quotes_not_current`, `small_hotel_unsuitable_with_reason`, `proposal_flagged_after_party_change`, `pending_work_after_party_change` |
| 2 | `HotelAccepted` | `acceptance_recorded`, `acceptance_survives_earlier_request` |
| 3 | `EarlierFlightRequested` | `earlier_anchor_set`, `earlier_anchor_stays_fixed`, `acceptance_survives_earlier_request` |
| 4 | `QuoteExpired` | `expiry_keeps_hotel_identity`, `expiry_flags_proposal` |
| 5 | `ToolObservationReceived` | `price_rise_needs_reconfirmation` |
| 6 | `build_context` | `missing_data_explicit`, `context_required_sections`, `context_current_vs_historical`, `context_shows_decision_and_work` |

Two checks (`two_person_quote_cannot_validate` and `duplicate_change_is_noop`) test supplied code and should pass from the start. After the exercise checks, `check` replays the simulated state agent on the `changing-trip` and `no-compliant` scenarios and checks the recorded outcomes.

### Hints

TODO 1. Ask what is true once the party is three, and what stays exactly as it was. Every requirement other than the party stays. A quote records the party and dates it was issued for, and `scope_matches` tells you whether a quote still prices the trip. A quote that no longer matches stays on record with a different status. Capacity is a fact stored in each hotel quote, so derive unsuitable hotels from it, and write down why so the reason survives. Consider what the change does to a hotel decision that already exists, to the proposal on the table, and to the work that remains.

TODO 2. The change carries three IDs. The decision also needs the price the user saw, and the quote in `state.quotes` holds it. Store that total on the decision itself, because the quote's status can change later and the decision must still say what the user agreed to. Accepting a hotel invalidates nothing.

TODO 3. Take the anchor from the change, never from `state.proposal`. The current proposal changes with every new plan, and the anchor must not move. A preference lives in the requirements, so the requirements revision moves with it. The hotel decision is none of this change's business.

TODO 4. Separate what is no longer true (the price evidence) from what stays true (the user's choice). The world expires every quote of the repriced hotel, including old two-person quotes you already marked stale, so this branch runs more than once for one hotel. Decide for each quote whether follow-up work is needed. `add_task` ignores a task that is already open with the same kind and target. `proposal_uses_quote` tells you whether the current proposal depends on the quote.

TODO 5. A price change shows up as a new quote, and `record_tool_observation` returns the quotes the state had not seen. A new quote should trigger reconfirmation only when it is for the accepted hotel, it prices the current party and dates, and its total differs from the total the user accepted. Compare against the accepted total, not against the previous quote. A quote for another hotel, a quote for the old party, and an unchanged price all leave the decision alone.

TODO 6. The simulator and the report understand these section names: `requirements`, `unknowns`, `decisions`, `current_quotes`, `unsuitable_items`, `current_proposal`, `pending_work`, `historical`. Label each section `current` or `historical` honestly. A useful test is whether the model would be wrong to rely on the item today. Inactive quotes, earlier proposals, and notes are history, and the model should still be able to find them. A compact summary (ID, status, reason, total) serves history better than a full dump. `unknowns` comes from `state.requirements.missing_fields()`. `pending_work` holds open tasks only. Keep the supplied sections.

Common mistakes the checks catch: clearing everything on every change (`child_preserves_requirements` fails), deleting old quotes (`old_quotes_not_current` tells you to keep them and change their status), moving the earlier-arrival anchor to the newest proposal (`earlier_anchor_stays_fixed`), and listing stale quotes as current (`context_current_vs_historical`).

### Reading check output

`check` prints one line per check, then the simulated replays, then an `Overall` line. It exits with 0 only when everything passes.

```text
Exercise checks for strategy=student
  PASS                     child_preserves_requirements: requirements preserved; party is 2 adults, 1 child
  FAIL                     old_quotes_not_current: AssertionError: still current: ['quote-h11-r1-2a0c', ...]
  TODO                     acceptance_recorded: stage 'accepted_and_earlier' was not reached; building 'accepted_and_earlier' raised NotImplementedError: TODO 2
  ERROR (your code)        expiry_flags_proposal: stage 'quote_expired' was not reached; building 'quote_expired' raised KeyError: 'no-such-quote'
```

| Label | Meaning | What to do |
|---|---|---|
| `PASS` | The assertion held. | Nothing. |
| `FAIL` | Your function ran, and its result did not meet the check. The line states what the check found. | Read the detail and compare it with the hint for that TODO. |
| `TODO` | A function raised `NotImplementedError`. Later checks that need its output say `stage ... was not reached`. | Implement that branch. |
| `ERROR (your code)` | An exception other than `NotImplementedError` came from `workshop/exercises.py`. The GUI shows the traceback under the row. | Fix the exception, starting with the first one listed. |
| `ERROR (infrastructure)` | The failure came from code outside your file. | Tell the instructor. It is not your bug. |

The starter file passes 3 of the 18 exercise checks and fails the rest with readable messages, because it contains no `NotImplementedError`. You will see `FAIL` lines first, not `TODO`. The scenario replays print `PASS`, `FAIL`, or `n/a` lines (`n/a` means the check did not apply to that run). The last line is `Overall: PASS`, `FAIL`, `TODO`, `STUDENT_ERROR` (your file does not import), or `INFRASTRUCTURE_ERROR`. The full results are in `submissions/check-results.json`.

If the `Overall` line says `INFRASTRUCTURE_ERROR` but a detail line begins `Your exercise code raised` or names `exercises.py`, the cause is in your file. A syntax error reports its file and line number in the message.

If a `run` or `session` command stops with `Your exercise code raised ...` and `Nothing was saved`, the error is in your file and the failed operation changed nothing. The session directory exists at its starting point. After you fix the code, continue it with `uv run workshop session run NAME` or replace it by repeating the `run` command with `--overwrite`.

## Minutes 12 to 17: run, compare, and look

When `check` is green, or the scenario runs complete, generate your runs.

```bash
uv run workshop run --implementation state --strategy student --scenario changing-trip --planner simulated --out runs/state-fixed
uv run workshop compare --runs runs/transcript runs/state-fixed --output submissions/comparison.html
```

Open `submissions/comparison.html` in a browser (`open submissions/comparison.html` on macOS, `xdg-open` on Linux). A correct solution reaches the same four plans as the transcript agent (plan-001 through plan-004, ending at $1,740).

In the GUI: create a session named `state-fixed` (Structured state, simulated, student strategy, `changing-trip`) and press "Run to completion". Select the `transcript` session under "Open session", choose `state-fixed` under "Compare with", and press "Compare side by side". The "Comparison" tab shows both runs. Press "Export comparison HTML" to write `submissions/comparison.html`. You can also press "Run exercise checks", which writes `submissions/check-results.json`.

### What to look at in the state diff

The diff shows what one change did to the task state. Focus on the `chg-` snapshots, which follow user and world changes. The `obs-` snapshots follow tool results, and the `presented-` snapshots follow plans shown to the user. In the reference run the snapshot numbers are 6 (`chg-t1-0`, the child joins), 11 and 12 (`chg-t2-0` and `chg-t2-1`, the acceptance and the earlier-flight request), 16 and 17 (the two quote expirations), and 18 (`obs-tc-0012`, the refreshed hotel quote arrives). Your numbers match if your agent makes the same tool calls. To list yours:

```bash
uv run python -c "import json; [print(s['index'], s['change_id']) for s in map(json.loads, open('runs/state-fixed/state_snapshots.jsonl'))]"
```

```bash
uv run workshop session show state-fixed diff --index 6
```

In the GUI, open the "State" tab, find "State snapshots", pick a snapshot, and read "What snapshot N changed". The checkbox hides revision counters and bookkeeping. Look for four things.

- Which paths changed, and which requirement fields do not appear at all. Origin, destination, dates, budget, and the walking preference should be absent from the diff of the child change.
- Whether old quotes changed status and kept their reason, or disappeared.
- Whether `decisions.hotel` appears after the acceptance and survives every later change.
- Which pending tasks the change opened.

### What to look at in the model input

Open the exact input of a model call. In the GUI, use the "Model input" tab and pick a call from the list, which shows each call's turn, step, and input size. In the CLI, run `uv run workshop session show NAME model-input --index N`; without `--index` it shows the latest call, and its first line gives the call's turn, step, and size. Call 12 is the first call of turn 3 in the reference run, right after the quote expires.

Compare the two agents at that call.

- The transcript agent receives 26 messages: the whole conversation, every tool result, and the expiry notification. Nothing marks which earlier results still hold. The model must work out from each quote's party fields that the two-person quotes are stale.
- The state agent receives one message built from your `build_context`. Check that sections marked `CURRENT` hold only what is true now, that `decisions` still shows the accepted hotel, that `pending_work` lists the refresh task, and that `HISTORICAL` holds the old quotes with their status and reason.
- Compare the sizes. In the reference run, call 12 is about 10,000 characters for the state agent and about 18,000 for the transcript agent. The state agent is larger on the first call. Size follows the design, and it is not a goal.

Then open the "Tool calls" tab and note which tool each agent called next. Find the information in its input that justified that call.

## Minutes 17 to 20: findings and discussion

Copy [findings-template.md](findings-template.md) to `submissions/findings.md` and fill it in. The template has a table of three state transitions, one division of responsibility between code and the model, answers to seven questions, and a short comparison of the two agents. One run is one sample, so write what this run showed, not what it proves.

| Question | Where to look |
|---|---|
| 1. Which data changed when the child was added, and which did not? | The state diff for `chg-t1-0` |
| 2. Where is the hotel choice represented? | "Retained hotel decision" in the State tab, and the diff for `chg-t2-0` |
| 3. What happened to that choice when the quote expired? | The diffs for `chg-t3-1` and for `obs-tc-0012` |
| 4. Which input shows history versus current context? | A late model call of each agent |
| 5. Which tool did the agent call next, and why? | The "Tool calls" tab and the input of the call before it |
| 6. Which conclusions did code check? | The "Validation" tab: reason codes, computed totals, claimed totals |
| 7. If both agents succeeded, what differs? | The comparison page: tool counts, input sizes, retained decision, state transitions |

## Submission

- `workshop/exercises.py`, completed.
- `submissions/comparison.html`, generated from your own `runs/transcript` and `runs/state-fixed` runs.
- `submissions/check-results.json`, written by `workshop check`.
- `submissions/findings.md`, your completed findings template.

Git ignores `submissions/`, so hand the files in the way your instructor asks.

## Step by step, and the GUI equivalents

Use these commands when you want to pause between changes. A session named `NAME` lives in `runs/NAME`, so `--out runs/state` and the session name `state` refer to the same directory.

| To do this | CLI | GUI |
|---|---|---|
| Create a session | `uv run workshop session start NAME --implementation state --strategy student` | "New session": Name, Implementation, Planner, Strategy, Scenario, then "Create session" |
| Run a whole scenario | `uv run workshop run ... --out runs/NAME`, or `session run NAME` | "Run to completion" |
| Let the agent answer the current turn | `uv run workshop session run-turn NAME` | "Run turn" |
| Take one planner decision | `uv run workshop session step NAME` | "Step" |
| Apply the next change | `uv run workshop session apply-change NAME` | The button under "Scenario changes" marked "next: ready to apply" ("Turn 1: Add the child", "Turn 2: Accept hotel + request earlier flight", "Turn 3: Expire the hotel quote") |
| See the status and conversation | `uv run workshop session status NAME` | "Conversation" tab and the "Proposed plan" card |
| Inspect the task state | `uv run workshop session show NAME state` | "State" tab |
| See what a change did | `uv run workshop session show NAME diff --index N` | "State" tab, "State snapshots" |
| See the exact model input | `uv run workshop session show NAME model-input --index N` | "Model input" tab, pick a call |
| See tool calls | `uv run workshop session show NAME tools` | "Tool calls" tab |
| See validator results | `uv run workshop session show NAME validation` | "Validation" tab |
| Run the exercise checks | `uv run workshop check --strategy student` | "Run exercise checks", then the "Check results" tab |
| Compare two runs | `uv run workshop compare --runs runs/A runs/B --output submissions/comparison.html` | "Compare with", "Compare side by side", then "Export comparison HTML" |
| Rewrite a run's artifacts | `uv run workshop session export NAME` | "Export artifacts", then "Open report.html" |
| Start the session over | `uv run workshop session reset NAME` | "Reset this session" (asks for confirmation) |

A session starts in the `ready` phase with the initial request applied and unanswered. Let the agent answer it (a step or a turn) before you apply the first change. A change button stays disabled until the agent has finished the turn before it.

If a command says a session already exists, choose another name, add `--overwrite` to `run` or `session start`, or reset the session. If the GUI reports that the session is at a newer version, something else changed it, such as a CLI command or another tab. The page has already refreshed, so press the button again.
