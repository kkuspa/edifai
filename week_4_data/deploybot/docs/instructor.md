# Instructor notes

Keep `solutions/` and this file out of the copy students start from if you want the exercise unspoiled. `--strategy reference` and `workshop examples` need `solutions/reference.py`; everything else works without it.

## Before class

Ask students to run, at home or on arrival:

```bash
uv sync --frozen
uv run workshop doctor
```

`doctor` must end with `doctor: OK`. Optional extras it reports (GUI, Jaeger, LLM keys) are not needed for the assignment. If you plan to demonstrate Jaeger, have students pull the image in advance with `docker compose pull`.

Check the reference path yourself in a fresh clone:

```bash
uv run workshop check --strategy reference
uv run pytest
```

All 19 checks pass, and the test suite passes in the starter (no student edits) and after a correct solution.

## Expected behaviour

Before any edits, `workshop check --strategy student` reports every check as `TODO`, and `workshop run --strategy student` stops at `memory.project` with exit code 2 and the message "Unfinished exercise". Both say the runtime is fine. The baseline runs normally.

| Run | Context the planner sees | Proposal | Gate | Executed |
|---|---|---|---|---|
| baseline at seq 40 | b2 (evt-021); tests "passed" and approval "approved", with no commit and no source; memory through seq 20 | deploy b2 / staging | blocked: tests_failed, approval_missing_for_target | nothing |
| repaired at seq 40 | b2 failed run 1 (evt-032); approval missing; a1/staging (evt-012) kept as history | report_test_failure b2 | allowed (no_side_effect) | nothing |
| after tests-pass-b2 | b2 passed run 2 (evt-041); approval missing | request_approval b2 / staging | allowed (no_side_effect) | nothing |
| after approve-b2-staging | b2 passed (evt-041); approval-b2-staging (evt-042) | deploy b2 / staging | allowed (all_checks_passed) | mock deploy, receipt |
| same facts, `--target-environment production` | approval missing for production | request_approval b2 / production | allowed (no_side_effect) | nothing |
| head moves to c3 with no test (check only) | tests unknown | request_test_run c3 | allowed (no_side_effect) | nothing |

The gate "allowed" a non-deploy proposal because there is no side effect to authorize; the runner still records the gate's own authoritative blockers on the span (`workshop.gate.authoritative_blockers`).

Running the approved deployment twice returns the existing receipt (`reused_existing: true`).

## How the baseline was built

The full description is the module docstring of `src/agent_state_workshop/baseline.py`. In short: at the pause the checkpoint process sets `tests_passed` if any test passed and `deployment_approved` if any approval exists, writes one templated sentence, and saves it. On resume the baseline reads only that summary, asks the read-only head tool for the current commit, and pairs b2 with the unscoped flags. The simulated planner turns "passed + approved" into a deploy proposal. This shows one way an implementation can fail; it does not claim every summary or every LLM fails this way.

## Discussion notes for instructors

Span and event IDs differ per run; event IDs below are fixed by the fixture.

1. The baseline assumed the pause-time facts still described the current commit. Its `context.json` shows `current_test.status=passed` and `matching_approval.status=approved` with no commit, no environment, and no source; `workshop.context.unsourced_facts` on its `context.build` span lists both. Its memory stopped at sequence 20 (`workshop.projection.events_not_incorporated=20`).
2. evt-021 sets the head to b2.
3. evt-010 is a passing run for a1. The required suite for b2 failed in evt-032. A result belongs to its commit.
4. evt-012 approves a1 for staging only. Nothing approves b2.
5. `planner.propose` changes from `deploy b2` to `report_test_failure b2`; the repaired context cites evt-021, evt-032, evt-012.
6. No deployment in either run: neither trace has a `tool.deploy` span and neither directory has `receipt.json`. In the baseline the gate blocked the deploy (`proposal.rejected` event); in the repaired run nothing was proposed to deploy.
7. After evt-041 the tests pass, but the context still shows `approval_missing_for_target`; evt-012 covers a1. Only evt-042 authorizes b2 for staging, and then only staging.

Facts about a1 remain true after the head change. They stop deciding the next action because they describe a different commit.

## Common student mistakes

* Overwriting the a1 test record when b2's result arrives (fails "history keeps its scope").
* Keeping the latest-arriving run instead of the highest run number (fails "highest run number wins").
* Treating the lint pass (evt-027) as the required suite. `find_test` defaults to the required suite.
* Matching an approval by commit only, so the production target looks approved.
* Returning only the first blocker.
* Annotating with nested objects or `None` values. `set_attr` skips `None`; nested data belongs in `context.json`.

## Setup recovery

| Symptom | Fix |
|---|---|
| `uv sync --frozen` fails on Python | `uv python install 3.12` |
| `already exists` from prepare | `workshop reset --db ...` or `prepare --reset` |
| `already holds a run` | new `--out`, or `--replace` |
| `run ... is still in progress` | `workshop step --db ... --until-done` or `workshop abandon --db ...` |
| GUI says the extra is missing | `uv run --extra gui workshop gui` |
| GUI port in use | `uv run --extra gui workshop gui --port 8766` |
| A GUI action shows `interrupted` | The server stopped mid-operation; run it again. |
| Jaeger shows nothing | Check `manifest.json` `otlp.errors`; the local trace is unaffected. |

## Interfaces

The CLI and GUI call the same functions in `service.py`. Mutating actions go through operations recorded in `.workshop/operations.sqlite` with an operation ID; re-submitting an ID returns the original record. Operations on one session database are serialized with a file lock, so a CLI command and a GUI click on the same session cannot interleave. The GUI binds to 127.0.0.1, rejects non-localhost Host headers, requires JSON on POST, and never returns key values. All GUI reads are side-effect free; polling only reads.

Student and reference code is compiled from source at every run and check, which is why the GUI server never needs a restart after an edit.

## Live mode preflight

1. Put a key in `.env` and confirm the model name in `ANTHROPIC_MODEL` or `OPENAI_MODEL` is available to that account.
2. `uv run workshop doctor` shows the key as configured (never its value).
3. Run the baseline and the reference once each with `--planner live` and read `llm-request.json` and `llm-response.json`.

Both adapters force a single tool call whose arguments must validate as a `Proposal`; malformed output is retried up to three attempts, each with its own `model.generate` span. Models often notice the stale summary and decline to deploy. That is a legitimate observation and a useful discussion point; it does not change the deterministic exercise.

## Example traces

`solutions/example-runs/` holds runs generated by the reference solution: `before` (baseline), `after`, `tests-pass`, `approved`, and an `evidence/` export. Regenerate them with:

```bash
uv run workshop examples
```

## Pilot status

The exercise is untimed and has not been piloted yet. The reference solution changes about 60 lines of `exercises.py`. If people get stuck on the mechanics rather than the ideas, provide more of `build_context` (for example the `TestEvidence` construction) rather than dropping the trace comparison.

## Optional extensions

* Change `PROJECTION_VERSION` and confirm `state.load` reports `version_mismatch` and the state is rebuilt.
* Add an `approval.revoked` event type and decide how it affects the materialized view.
* Deliver CI results out of order with explicit run numbers.
* Compare full-history prompts, the narrative summary, and structured context across repeated live runs.
* Carry trace context into a separate mock deployment service.
* Measure latency and input tokens across strategies without a token budget.
