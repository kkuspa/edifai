# Workshop reference

The [README](../README.md) walks through the exercise step by step. This page collects the background you may want beside it: the events that matter, the rules the repaired code follows, the commands for looking around, and fixes for common snags.

## The events that matter

The history in `fixtures/stale-resume/` holds 40 events in one session. Most are routine file reads and edits. These carry the decision:

| Event | What happened |
|---|---|
| evt-001 | Policy: deploy only a commit whose required suite passed, with approval for that exact commit and environment. |
| evt-008 | The head is `a1`. |
| evt-010 | The required suite passed for `a1`. |
| evt-012 | The release lead approved `a1` for staging. |
| evt-020 | The assistant paused, saved its memory, and its process exited. |
| evt-021 | The head moved to `b2` while the assistant was stopped. |
| evt-027 | The lint suite passed for `b2`. Lint is not the required suite. |
| evt-032 | The required suite failed for `b2`. |
| evt-040 | The user asked, "Can you finish the deployment?" |
| evt-041 | Follow-up: the required suite passed for `b2` on its second run. |
| evt-042 | Follow-up: the release lead approved `b2` for staging. |

`workshop prepare` reproduces the pause for real. A child process writes events 1 through 20 and the assistant's memory, then exits. Only then does the parent process append events 21 through 40. The two follow-ups arrive when you run `workshop append`.

## The data rules

The repaired projection and context follow these rules.

1. Order events by `sequence`, never by `occurred_at`. The helper `ordered_unique()` sorts them and drops duplicate deliveries.
2. A head change sets the current commit. It leaves the `a1` test and the `a1` approval true; it changes only whether they apply.
3. A test result belongs to one commit and one suite. For the same commit and suite, the highest `run_number` wins, whatever order the events arrived in. A later result for an older commit never replaces the current commit's result.
4. An approval belongs to one commit and one environment.
5. Every fact keeps its `source_event_id`.
6. A missing fact is reported as `unknown` or `missing`, never invented.
7. The context lists every blocker that applies, even though the planner chooses one action. Failed tests take priority over a request for approval.

## Looking around

| To see | Run |
|---|---|
| The event history | `workshop events --db .workshop/fixed.sqlite` |
| A session's runs, snapshots, and receipts | `workshop status --db .workshop/fixed.sqlite` |
| The state a run derived | `workshop show --run runs/after --artifact state` |
| What changed since the last saved state | `workshop show --run runs/after --artifact state-diff` |
| Exactly what the planner received | `workshop show --run runs/after --artifact planner-input` |
| Which tools ran | `workshop show --run runs/after --artifact tools` |
| What the gate checked | `workshop show --run runs/after --artifact gate` |
| Span tree, durations, and attributes | `workshop trace --run runs/after --attributes` |
| Field-level differences between two runs | `workshop diff --before runs/before --after runs/after` |

Prefix each command with `uv run`. Every one of them has a counterpart in the web interface (`uv run --extra gui workshop gui`).

## Common snags

| Message | What to do |
|---|---|
| `... already exists` from `prepare` | Add `--reset`, or run `workshop reset --db ...`. |
| `... already holds a run` | Choose a new `--out`, or add `--replace` to discard the old run. |
| `run ... is still in progress` | Finish it with `workshop step --db ... --until-done`, or drop it with `workshop abandon --db ...`. |
| `Unfinished exercise` | `project_state` or `build_context` still has a `todo(...)`. `workshop check --strategy student` shows which. |
| A warning that the annotation is incomplete | `annotate_context_span` is missing an attribute or the `evidence.selected` event. The warning names what is missing. |
