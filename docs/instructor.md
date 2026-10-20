# Instructor guide

This guide covers what a correct run looks like, what the starter code gets wrong and how that shows up, how to run the discussion, how to treat live-model results, and what to do when something breaks. Student instructions are in [workshop.md](workshop.md).

The lesson is not that more state machinery is always better. Ongoing tasks create data lifecycle responsibilities, and those responsibilities need an explicit owner in the application design. The transcript agent may succeed on every scenario, and that is an acceptable outcome to report as it is.

## Before class

1. On a clean machine, run `uv sync --frozen` and `uv run workshop doctor`. It must end with `Ready.` Add `--extra gui` to the sync if anyone will use the GUI, because a plain `uv sync` removes the GUI packages.
2. Run `uv run workshop check --strategy reference`. It must end with `Overall: PASS. All checks pass.` Expect two `n/a` lines in the scenario replays: `no_compliant_plan_asks` for `changing-trip` (a compliant plan exists) and `price_change_flagged` for `no-compliant` (the run ends on a question).
3. Run `uv run pytest`. The suite works in temporary directories, reads no credentials, and opens no network connection. It should finish with every test passing.
4. Open `reference_runs/comparison.html` to see the reference comparison. Regenerate the runs with `bash scripts/regenerate_reference_runs.sh` if you changed any code or fixture.
5. Pilot the exercise once yourself. The 20-minute target comes from the specification, and your room may need a different pace.
6. Decide how students hand in work. Git ignores `submissions/`, so a commit will not collect it.
7. If students will use live mode, finish the live-mode checks below.
8. If students will use the GUI, confirm that port 8765 is free on their machines, or tell them to pass `--port`.

## Reference behavior

All runs below use the simulated planner and, for the state agent, `--strategy reference`. They are deterministic. The simulator reads only the model input it is given (the rendered context sections for the state agent, the conversation for the transcript and one-shot agents). It never reads the scenario name, the expected answers, or the validator's private state.

### Both agents, turn by turn

Both agents make 15 model calls and 11 tool calls, and they present the same four plans.

| Turn | Change | Transcript agent | State agent | Plan presented |
|---|---|---|---|---|
| 0 | The initial request for two adults | `search_hotels`, `search_flights`, `validate_plan` (f1 and h11) | The same | plan-001: f1 + h11, $1,200 |
| 1 | The child joins | `search_hotels` and `search_flights` for two adults and a child, `validate_plan` (f1 and h17) | `refresh_quote` for f1, `search_hotels` for the new party, `validate_plan` (f1 and h17) | plan-002: f1 + h17, $1,620 |
| 2 | The user accepts the hotel and asks for an earlier flight | `search_flights` with `arrive_before` 18:00, `validate_plan` (f2 and h17) | The same | plan-003: f2 + h17, $1,680 |
| 3 | The hotel quote expires and reprices | `refresh_quote` for h17, `validate_plan` (f2 and refreshed h17), `validate_plan` (f3 and refreshed h17) | The same | plan-004: f3 + h17, $1,740, reconfirmation required |

Where the sequences differ, the justification differs.

- Turn 1. The state agent's context shows the open tasks `refresh_flight_quote` (target f1) and `find_hotel_capacity`, h11 under `unsuitable_items`, and no current quotes. It refreshes the flight in the displayed proposal and searches hotels for the new party. The transcript agent has no such record, so it repeats both searches after reading the party change.
- Turn 2. Both agents search flights with the arrival bound 18:00. The state agent reads that bound from the `earlier_arrival` preference and the `search_earlier_flight` task. The transcript agent reads it from the user message and the normalized change in the transcript. Neither agent searches for hotels, because the accepted hotel stays.
- Turn 3. Both agents refresh h17 for two adults and a child. The state agent has the accepted decision, the expired quote, and the `refresh_hotel_quote` task. The transcript agent has the notification and the earlier acceptance. The first validation (f2 and refreshed h17) returns $1,860 with `over_budget` and `confirmation_required`, and the plan is never shown. The second (f3 and refreshed h17) totals $1,740 and returns `confirmation_required` alone, so the runner displays plan-004 with `reconfirmation_required`. Its explanation discloses the connection (CLT, 55 minutes) and the hotel's change from $720 to $900.

Neither simulated agent calls `read_history`. A live agent might, which is worth looking for in its tool calls.

Input size per model call, in characters of system text plus messages (a deterministic count, not tokens):

| Run | Per call, in order | Total |
|---|---|---:|
| Transcript | 2,118; 4,678; 7,913; 8,387; 8,776; 10,858; 13,570; 14,044; 14,657; 16,890; 17,364; 17,917; 18,612; 19,317; 19,915 | 195,016 |
| State | 3,421; 5,395; 8,148; 8,474; 6,127; 6,520; 7,837; 7,997; 8,691; 10,180; 10,347; 10,245; 10,926; 11,414; 11,909 | 127,631 |

The transcript input grows at every call because it replays everything. The state input rises and falls with what is current. The state agent sends more on the first call, because its context carries structure the transcript does not. The simulator reports no token usage, and the report says so.

### The no-compliant variant

The `no-compliant` scenario reprices the accepted hotel to $1,200. With the reference functions the state agent follows the same path through turn 2, then refreshes h17 in turn 3 and asks a question: "No combination fits the $1,800 budget. Lakeview Suites, queen suite with sofa bed (h17), which you accepted, now costs $1,200 (it was $720). The lowest total with it is $2,040 using flight f3. Would you rather raise the budget to at least $2,040, or have me look for a different hotel?" The transcript agent asks the same question. The runner shows a question and never a non-compliant plan.

## What the starter does wrong, and how it shows up

The starter `apply_change` only overwrites the party. The starter `build_context` sends the requirements and every quote ever observed, all under `current_quotes [CURRENT]`. It has no decisions, pending work, or historical sections. Run it with `uv run workshop run --implementation state --strategy student --scenario changing-trip --planner simulated --out runs/state`.

Turn 1 ends incomplete. After the child joins, all nine two-person quotes are still `current`, `h11` is not marked unsuitable, the proposal is not flagged, and the requirements revision stays at 1. The simulated agent trusts those labels and proposes a series of combinations built from two-person quotes (f1 and h17, f2 and h17, f4 and h17, and so on). Each `validate_plan` result reports `quote_scope_mismatch`, because the quote was issued for two adults. After eight tool calls the runner ends the turn with "Tool-call limit (8) exhausted without a validated plan or question." The agent presents a stale price as current until the limit stops it, and the runner never invents a plan.

Turn 2 resolves against the wrong proposal. The scenario resolves "that hotel looks good" against the proposal displayed in this run. Turn 1 displayed nothing, so the only displayed proposal is plan-001, and the acceptance resolves to h11, a room that sleeps two. The starter stores neither the acceptance nor the earlier-arrival anchor, so the state holds no decision. The validator still folds both changes into its own view, so validations now report `confirmation_required` and `arrival_preference_unmet`. The turn ends incomplete at the tool limit again. You can see the resolution in `uv run workshop session show state changes`, where `chg-t2-0` names h11 and plan-001.

Turn 3 ends incomplete for the same reason. There is only one h17 quote to expire (`quote-h17-r1-2a0c`, issued for two adults), so the run records one expiry change where the reference run records two.

The starter passes 3 of the 18 exercise checks. `uv run workshop check --strategy student` fails the rest with messages such as "still current: ['quote-h11-r1-2a0c', ...]" and "decisions.hotel is None". The scenario replays report `turns_finished: incomplete turns: [1, 2, 3]`.

This failure belongs to the simulator, which trusts the `CURRENT` label. A live model might notice that a quote's `party` field disagrees with the requirements and recover. If a live run of the starter succeeds, use that result in the discussion instead of explaining it away.

### Partial solutions

Both functions matter, and the checks isolate which one is missing.

| Student state | What `check` and `run` show |
|---|---|
| `apply_change` done, `build_context` still the starter | The four context checks fail (`missing_data_explicit`, `context_required_sections`, `context_current_vs_historical`, `context_shows_decision_and_work`). The run completes turns 1 and 2, but turn 3 drops the accepted hotel. With no decision or refresh task in front of it, the agent presents f2 + h22 at $1,460 (a 25-minute walk, so preferences are unmet), flagged `reconfirmation_required`. The state held the decision, and the model never saw it. The `no-compliant` replay fails `no_compliant_plan_asks`, and `state_no_stuck_tasks` fails because the refresh and search tasks never close. |
| `build_context` done, `apply_change` still the starter | The state never records the child, the acceptance, or the anchor, so thirteen exercise checks fail and turns 1 to 3 end incomplete. A good context over state that nobody updated has nothing true to show. |

## Discussion

### The seven required findings, with model answers

Snapshot numbers and call numbers below come from the reference run. A student run that makes the same tool calls matches them.

1. Which data changed when the child was added, and which did not? The party changed from two adults to two adults and a child, and the requirements revision moved from 1 to 2. Nine two-person quotes became `stale`, each keeping a reason ("Issued for 2a0c ...; party is now 2a1c."). `h11` entered `unsuitable_items` with "Capacity 2 < occupancy 3." The proposal's status became `needs_recheck`. Three tasks opened: `refresh_flight_quote` (f1), `find_hotel_capacity`, and `revalidate_plan`. Origin, destination, dates, nights, budget, and walking preference did not change, and no quote was deleted. Evidence: the diff of snapshot 6 (`chg-t1-0`).
2. Where is the user's hotel choice represented? It is `decisions.hotel` in the task state: hotel `h17`, accepted from `plan-002`, quote `quote-h17-r1-2a1c`, accepted total 72,000 cents, status `accepted`. It is separate from the proposal (which will be replaced) and from the requirements (which hold constraints and preferences). The validator keeps its own copy, derived from the change log. The transcript agent has no structured record, only the user's words and the normalized change inside the transcript. Evidence: the diff of snapshot 11 (`chg-t2-0`).
3. What happened to that choice when the quote expired? The choice stayed. At expiry (snapshots 16 and 17) the quote became `expired`, plan-003 became `needs_recheck`, and the tasks `refresh_hotel_quote` (h17) and `revalidate_plan` opened. `decisions.hotel` was untouched. When the refreshed $900 quote arrived (snapshot 18, `obs-tc-0012`), the decision became `needs_reconfirmation` with `requires_reconfirmation` true and the reason "Price changed from $720 to $900 since acceptance.", and a `confirm_with_user` task opened. The identity of the hotel survived. The user's acceptance of the price did not.
4. Which model input demonstrates the difference between history and current working context? Call 12 of each run, the first call after the expiry. The state agent's single message has sections marked `[CURRENT]` (requirements, decisions, current quotes, pending work) and one marked `[HISTORICAL]` (inactive quotes with status and reason, previous proposals, notes). The transcript agent's call has 26 messages in one sequence, where a $720 quote, a $900 quote, and a two-person quote carry equal weight and the model must decide which still applies.
5. Which tool did the agent choose next, and what available information justified it? In turn 3, `refresh_quote` for h17 with two adults and a child. The state agent's input shows an accepted decision for h17, an expired quote, and an open `refresh_hotel_quote` task for h17. The transcript agent's input shows the expiry notice and the earlier acceptance. In turn 2, `search_flights` with `arrive_before` 18:00 followed from the earlier-arrival preference and the `search_earlier_flight` task (state) or from the user's message (transcript). Ask students to cite the lines in the input, not their memory of the scenario.
6. Which conclusions were checked by code rather than trusted from generated text? Every total (recomputed in integer cents from per-traveler fares and the whole-stay hotel price, then compared with the claimed total), quote freshness (clock, revision, and expiry), quote scope (party and dates), seats, hotel capacity, availability, the budget, the earlier-arrival preference, and whether a plan replaces or reprices the accepted hotel. The runner validates every plan before display. The clearest example is f2 + refreshed h17 at $1,860: it carried a claimed total, the validator returned `over_budget`, and the user never saw it.
7. If both agents succeeded, what differences remain in update behavior and inspectability? Both reach plan-004 with 11 tool calls. The differences are in how they got there. The state agent's updates are code the team wrote, tests, and can diff after every change (21 snapshots), and it exposes the retained decision and the fixed anchor as data. The transcript agent derives current truth from text on every call, so its "state" cannot be inspected, tested, or handed to another component. The state agent's input is smaller from turn 1 onward (127,631 characters against 195,016 in total) but larger on the first call. The cost on the state side is schema design, update rules that must stay correct, and a second record that can drift from the transcript.

### Closing questions, with model answers

These are the questions from the specification. Answers are starting points for the discussion.

**If the full transcript already contains every fact, why maintain another representation?** A transcript records what happened. It does not say what is true now. The $720 quote, the $900 quote, and the two-person quotes all sit in it with equal standing, and the model must rederive the current picture on every call, with no guarantee that it does so the same way twice. Code that derives the current picture is testable and repeatable, and it exposes the result to the validator, the user interface, and a human reviewer. The transcript agent succeeded here, so the answer depends on who else needs the current picture and how long the task runs.

**Who reconstructs current requirements in the one-shot version?** The caller does. Without a restated brief, the one-shot agent asks for the requirements every turn after the first. With `--restated-brief`, the application rebuilds the brief from its change log and sends it. That rebuilding is state management, moved outside the model call. The one-shot call is bounded by design, and judging it as a multi-turn workflow misreads what it promises.

**When is a historical fact still true but no longer applicable?** The quote of $720 for h17 for two adults and a child remains a true record of what the world said at one time, and it no longer prices the trip once it expires. The user's acceptance of h17 at $720 stays true, and it does not authorize $900. A two-adult quote remains accurate for two adults. Store the fact and its applicability separately, so the fact survives and the applicability can change.

**Does a user preference belong in the same structure as a price quote?** No. A preference or decision comes from the user and changes only when the user changes it, and it survives repricing. A quote comes from the world, carries a scope, and expires by clock or revision. They have different authorities and different invalidation triggers. In this repository, requirements and decisions live apart from quotes, and the hard constraints live apart from the preferences.

**Which parts of state should the model propose updates to, and which updates should code validate?** The model suits interpretation (turning an ambiguous message into a typed change), choosing the next tool, comparing options, and explaining tradeoffs. Code should own arithmetic, scope and freshness checks, capacity and budget rules, the application of updates, and persistence. A model can propose a typed change, and code checks and applies it. This repository applies that split: totals the model states are claims, and the validator computes the authoritative ones.

**What does explicit state cost in schema design, migrations, and maintenance?** Every kind of change needs a rule for what it affects, and the rules multiply as features arrive. This exercise has five student-written branches for one trip. A wrong rule shows the model something false with no error. Persisted state needs migrations when the schema changes (the models here forbid unknown fields, so a new required field breaks old sessions unless it has a default). The team must also keep the structured record consistent with the transcript and with the instructions in the prompt.

**When would the simpler transcript-based implementation be sufficient?** When tasks are short, changes are rare, nothing besides the model reads the current picture, and an independent validator catches mistakes, as here. It starts to cost more as histories grow, as decisions multiply, as a user interface or a second agent needs the current picture, or as audits need to show how a conclusion was reached.

## Live mode

Run live comparisons only after the simulated path works. Live mode answers a different question: how a particular model behaves with each context design.

Before class, check the configuration with one throwaway session:

```bash
uv run workshop run --implementation state --strategy reference --scenario changing-trip --planner live --provider anthropic --out runs/live-smoke
```

Replace `anthropic` with `openai` as needed. Open the session's `model_calls.jsonl` or the GUI's "Model input" tab and confirm that the model name matches your intent and that `errors` is empty. A wrong model name or a missing credential fails at the first call. For OpenAI, confirm that the default `gpt-5.5` is a model your account can use, or set `OPENAI_MODEL`. The `info live credentials configured` line in `doctor` reports only that a key or profile exists. It does not prove that the key works.

When you run the comparison:

- Give both agents the same model and settings. The report and the GUI warn when the settings of two compared runs differ, because a difference in outcome could then come from the settings.
- One run is one sample. Do not tell students that either implementation is more reliable on the strength of one run. If you want variability, repeat each agent several times under different session names and compare them all (`workshop compare --runs` accepts more than two).
- Compare actual usage. A live run records provider-reported input and output tokens and latency. Treat them as observations and not as targets.
- A model may infer the state correctly from the transcript, reach plan-004, and match the state agent. Treat that as evidence that transcript context can suffice for this case. Then ask what the application still lacks: a record it can show or test, a way to inspect why a choice survived, and a basis for deciding what to preserve when the conversation is a hundred turns long.
- A live state agent may fail where the simulator succeeded, or the reverse. The state design cannot fix a model that chooses poorly, and a model cannot fix state that is wrong. Look at the exact inputs before deciding which one failed.

The runner records a live failure as it happened. A reply that is not one valid tool call is retried up to `LLM_MAX_RETRIES` times and then ends the turn as `incomplete`, with the errors in the model call. A provider error also ends the turn as `incomplete`. The session cannot retry the turn, so reset it and run it again. A refusal appears as an error (`model refused`), and `ANTHROPIC_SERVER_FALLBACKS=default` enables Anthropic's server-side fallback for requests that a safety classifier declines.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `doctor` prints `FAIL imports and fixtures` | The environment is not installed, or the Python version is wrong. Run `uv sync --frozen`. The repository needs Python 3.12, which uv installs. |
| `doctor` prints `FAIL persistence and simulated run` or `FAIL report generation` | The machine cannot write to its temp directory, or a supplied file is broken. Read the message after the colon. If it names a file outside `workshop/exercises.py`, treat it as infrastructure. |
| `doctor` prints `FAIL student exercise module imports: SyntaxError ... (exercises.py, line N)` | The student's file does not import. Open that line. |
| `check` ends with `Overall: INFRASTRUCTURE_ERROR` and a message that names `exercises.py` or begins `Your exercise code raised` | The error is in the student's file. A syntax error and an exception raised during the scenario replays can reach the report under this heading. Fix the file as the message says. |
| `Your exercise code raised NotImplementedError: ... Nothing was saved; fix it and retry.` | A branch still raises `NotImplementedError`. The failed operation rolled back, so the session is unchanged and sits at its starting point (`phase=ready`, version 1). After the fix, continue with `uv run workshop session run NAME`, or repeat the `run` command with `--overwrite`. `check` reports the same condition as `TODO`. |
| A Python traceback that ends in `exercises.py` when running `run` or `session start`, and an "HTTP 500" message in the GUI | The student's module fails at import (a syntax error, or a bad name at module level). `doctor` and `check` print the file and line. |
| `error: ... already holds a session. Use reset, choose another name, or pass --overwrite.` | The name is taken. Choose another name, run `session reset NAME`, or repeat the command with `--overwrite`. In the GUI the message is "That session name is taken." |
| `error: ... exists, is not empty, and is not a session directory.` | The target directory holds files that are not a session. The command refuses it even with `--overwrite`; choose another `--out` or session name. |
| `error: Cannot apply a change while the session is 'ready'.` | The agent has not finished the turn. Run `session run-turn NAME`, or press "Run turn". |
| `error: Cannot step while the session is 'awaiting_change'.` | The turn has ended. Apply the next change with `session apply-change NAME`. |
| `error: Cannot apply a change while the session is 'complete'.` | Every scenario turn is done. Reset the session to run it again. |
| GUI message: "Session is at version N, request expected M. Refresh and retry. The page was refreshed with the current state." (HTTP 409) | Something else moved the session after the page last read it: a CLI command, another browser tab, or a repeated click. The page has already refreshed. Press the button again if the action is still wanted. The check protects against running a step twice. |
| GUI message: "Already done. This operation ID was applied earlier, so nothing ran again." | The page retried a request after a network failure, and the server returned the stored result. No action is needed. |
| GUI shows old data after a CLI command | The page does not watch the disk. Reload it. |
| `uv run workshop gui` fails with `address already in use` | Another process holds port 8765, often a second copy of the GUI. Run `uv run workshop gui --port 8766` and open that address. |
| `The GUI needs the optional extra: uv sync --extra gui` | The extra is not installed, or a plain `uv sync` removed it. Run `uv sync --frozen --extra gui`. |
| The GUI answers `This server only answers requests addressed to localhost.` | The page was opened under another host name. The server binds to 127.0.0.1 on purpose. Use `http://127.0.0.1:PORT`. |
| A command seems to hang | Another process holds the session's lock, for example a long live run started from the GUI. The CLI and the GUI serialize against each other per session. Wait for it to finish. |
| `error: No live provider configured. Copy .env.example to .env and set ANTHROPIC_API_KEY or OPENAI_API_KEY.` | No credential is present, and `--planner live` needs one. Create `.env` as the README describes. |
| Live turns end `incomplete` with `provider error` in the errors | Authentication, quota, network, or an unknown model name. Check the key and `ANTHROPIC_MODEL` or `OPENAI_MODEL`. Reset the session and run again. |
| Windows reports `No module named 'fcntl'` | The session lock needs a POSIX system. Use WSL, macOS, or Linux. |
| `workshop compare` reports `No session at ...` for a path in `reference_runs/` | Git ignores `*.sqlite`, so a fresh clone lacks the databases. Run `bash scripts/regenerate_reference_runs.sh` first. |

## Optional extensions

These sit outside the timed exercise.

- Extract typed changes with a live model. The scenarios in `fixtures/scenarios/` ship typed changes with resolved references, and `Engine.apply_next_change` in `workshop/runner.py` turns them into `Change` objects. A model-driven extractor would feed that step from the user's raw text. Compare its output with the shipped changes, and decide which of its proposals code should validate before applying.
- Add a conflicting request that needs clarification. Write a new scenario file (copy `changing-trip.json`) in which two changes disagree, and check that the agent asks one targeted question and does not guess.
- Add an inventory-unavailable event. `World.apply_event` in `workshop/tools.py` handles only the `reprice` event. A new event needs a world rule, a typed change in `workshop/models.py`, a branch in the student functions, and a decision about what happens to an accepted hotel that can no longer be booked (`no_longer_suitable` exists for this case).
- Measure variability. Run each live agent several times under different session names and compare them all with one `workshop compare --runs ...` command.
- Add a schema migration. Add a field to `TaskState` (the models reject unknown fields), keep an old session loadable, and write the step that upgrades its stored state.
- Connect the run records to the companion OpenTelemetry workshop. The JSONL artifacts carry stable identifiers (`tool_call_id`, `change_id`, quote IDs) that map onto span attributes.
