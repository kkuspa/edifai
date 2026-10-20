# Workshop: repairing an agent's memory after a restart

Time: 20 minutes, after `uv sync --frozen` and `uv run workshop doctor` succeed.
You edit one file: `src/agent_state_workshop/exercises.py`.
You may use a coding assistant. Your findings must cite span IDs and event IDs from your own runs.

Every command below has a GUI equivalent (`uv run --extra gui workshop gui`), listed in the README. Use whichever you prefer; the files they write are identical.

## The situation

The event history in `fixtures/stale-resume/` holds 40 events in one session. Most are routine file reads and edits. These carry the decision:

| Event | What happened |
|---|---|
| evt-001 | Policy: deploy only a commit whose required suite passed, with approval for that exact commit and environment. |
| evt-008 | Head is a1. |
| evt-010 | Required suite passed for a1. |
| evt-012 | The release lead approved a1 for staging. |
| evt-020 | The agent paused, saved its memory, and its process exited. |
| evt-021 | Head moved to b2 (while the agent was stopped). |
| evt-027 | The lint suite passed for b2. Lint is not the required suite. |
| evt-032 | Required suite failed for b2. |
| evt-040 | The user asked, "Can you finish the deployment?" |

`workshop prepare` reproduces the pause for real: a child process writes events 1 to 20 and its memory, then exits; only then are events 21 to 40 appended.

The policy is explicit. "Finish the deployment" resumes the task. It does not grant approval and does not change the approved commit or environment.

## Data rules

These are the rules your projection and context must follow.

1. Order events by `sequence`, never by `occurred_at`. `ordered_unique()` does this and drops duplicate deliveries.
2. A head change sets the current commit. It does not make the a1 test false or erase the a1 approval; it changes whether they apply.
3. A test result belongs to one commit and one suite. For the same commit and suite, the highest `run_number` wins, whatever order the events arrived in. A later result for an older commit never replaces the current commit's result.
4. An approval belongs to one commit and one environment.
5. Every fact keeps its `source_event_id`.
6. A missing fact is `unknown` or `missing`. Never invent one.
7. Keep every blocker that applies, even though the planner chooses one action. Failed tests take priority over requesting approval.

## Schedule

| Minutes | Do this | You should see |
|---|---|---|
| 0 to 3 | Run the baseline and read its context. | A deploy proposal for b2 that the gate blocks. |
| 3 to 10 | Exercises 1 and 2. | The repaired agent reports the failed b2 tests. |
| 10 to 13 | Exercise 3, then re-run. | `context.build` cites evt-021, evt-032, evt-012. |
| 13 to 18 | Compare before and after; run the follow-ups. | Visual evidence of the changed proposal; an approval is still required after tests pass. |
| 18 to 20 | Fill in `findings.md` and discuss. | Answers backed by span and event IDs. |

### 0 to 3: the baseline

```bash
uv run workshop prepare --scenario stale-resume --db .workshop/base.sqlite
uv run workshop run --db .workshop/base.sqlite --strategy baseline --planner simulated --out runs/before
uv run workshop show --run runs/before --artifact context
uv run workshop trace --run runs/before --span context.build
```

Look at `current_test` and `matching_approval` in the context. Which commit do they describe? Which event? Read the top of `src/agent_state_workshop/baseline.py` for exactly how this memory was built.

Want to watch it one stage at a time? Use `workshop step` instead of `run`:

```bash
uv run workshop prepare --db .workshop/stepping.sqlite
uv run workshop step --db .workshop/stepping.sqlite --strategy baseline
uv run workshop step --db .workshop/stepping.sqlite
uv run workshop status --db .workshop/stepping.sqlite
```

### 3 to 10: exercises 1 and 2

Open `exercises.py`. Replace each `todo(...)` call.

```bash
uv run workshop check --strategy student
```

The check output names the next unfinished step. Exercise 2's checks use a fixed input state, so you can work on it before exercise 1 passes. When both pass:

```bash
uv run workshop prepare --scenario stale-resume --db .workshop/fixed.sqlite
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --out runs/after
```

Expected proposal: `report_test_failure` for b2, with blockers `tests_failed` and `approval_missing_for_target`.

### 10 to 13: exercise 3

Fill in `annotate_context_span`. The attribute names are in `docs/telemetry-schema.md`. Re-run into a new directory (run directories are immutable):

```bash
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --out runs/after-2
uv run workshop trace --run runs/after-2 --span context.build
```

If you prefer to keep the name `runs/after`, add `--replace`. An incomplete annotation produces a warning in `workshop compare`, the HTML comparison, and the GUI.

### 13 to 18: compare and follow up

```bash
uv run workshop compare --before runs/before --after runs/after-2
uv run workshop visualize --before runs/before --after runs/after-2 --output submissions/comparison.html
```

Open the HTML file in a browser (it works offline). Click `context.build`, `planner.propose`, and `policy.validate` in each trace.

Now the follow-up. Tests pass for b2:

```bash
uv run workshop append --db .workshop/fixed.sqlite --fixture tests-pass-b2
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --out runs/tests-pass
```

Does a passing test authorize the deployment? Then grant approval and run again:

```bash
uv run workshop append --db .workshop/fixed.sqlite --fixture approve-b2-staging
uv run workshop run --db .workshop/fixed.sqlite --strategy student --planner simulated --out runs/approved
uv run workshop trace --run runs/approved
```

### 18 to 20: findings

```bash
uv run workshop export --before runs/before --after runs/after-2 --followup runs/tests-pass --followup runs/approved
```

Complete `submissions/findings.md`. `submissions/evidence.md` lists every span ID and trace tree you need.

## Inspecting without a browser

| Question | Command |
|---|---|
| What events exist? | `workshop events --db .workshop/fixed.sqlite` |
| What state did a run derive? | `workshop show --run runs/after --artifact state` |
| What changed since the last snapshot? | `workshop show --run runs/after --artifact state-diff` |
| What exactly did the planner see? | `workshop show --run runs/after --artifact planner-input` |
| Which tools ran? | `workshop show --run runs/after --artifact tools` |
| What did the gate check? | `workshop show --run runs/after --artifact gate` |
| Span tree, durations, attributes | `workshop trace --run runs/after --attributes` |
| Field-level differences between runs | `workshop diff --before runs/before --after runs/after --artifact context` |

## If something goes wrong

* `already exists`: pass `--reset` to `prepare`, or use `workshop reset --db ...`.
* `already holds a run`: choose a new `--out` or add `--replace`.
* `run ... is still in progress`: finish it with `workshop step --db ... --until-done`, or `workshop abandon --db ...`.
* `Unfinished exercise`: expected until the TODOs are done. `workshop check --strategy student` shows which.
