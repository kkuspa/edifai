# Findings

Copy this file to `submissions/findings.md`, and replace each bracketed prompt with your answer. Keep answers short and specific. Cite evidence by path or by name, so that a reader can find what you saw.

Name:

Runs compared (for example `runs/transcript` and `runs/state-fixed`):

Planner mode of each run (`simulated`, or `live` with the provider and model):

Strategy of the state run (`student`):

## 1. Three state transitions

Pick three changes from your state run. For each one, say what the application kept as it was, what it reconsidered, and where you saw it. The evidence is a line in `state_snapshots.jsonl` (give the `index` and the `change_id`, for example `index 6, chg-t1-0`) or a snapshot in the GUI's State tab (it is labeled `N: after CHANGE_ID`). Use `uv run workshop session show state-fixed diff --index N` to read the diff.

| Change | What was preserved | What was reconsidered | Evidence path |
|---|---|---|---|
| [change 1] | [data that stayed, and why it still applies] | [data that was marked stale, expired, flagged, or opened as work, and why] | [file and index, or GUI snapshot] |
| [change 2] | | | |
| [change 3] | | | |

## 2. One division of responsibility between code and the model

| | |
|---|---|
| A decision or calculation your code owns | [what it is] |
| A decision or calculation the model owns | [what it is] |
| Why the boundary sits there | [the reason, in one or two sentences] |
| Evidence | [a validator reason code, a tool result, or a model input you inspected] |

## 3. The seven required findings

1. Which data changed when the child was added, and which did not?

   [answer]

   Evidence: [path]

2. Where is the user's hotel choice represented?

   [answer]

   Evidence: [path]

3. What happened to that choice when the quote expired?

   [answer]

   Evidence: [path]

4. Which model input demonstrates the difference between history and current working context?

   [answer: name the run, the model call number, and the part of the input]

   Evidence: [path or GUI location]

5. Which tool did the agent choose next, and what available information justified it?

   [answer: name the tool and its arguments, and quote the lines of its input that justify the choice]

   Evidence: [path or GUI location]

6. Which conclusions were checked by code rather than trusted from generated text?

   [answer]

   Evidence: [validator reason codes, computed totals, or tool results]

7. If both agents succeeded, what differences remain in update behavior and inspectability?

   [answer]

   Evidence: [path]

## 4. The two agents compared

Fill in what you observed in your own runs. If both agents did equally well on a row, say so.

| | Transcript agent | State agent | Evidence |
|---|---|---|---|
| Correctness (the final plan, its total, hard constraints, validator codes) | | | |
| Preserved decisions (the hotel choice and the earlier-arrival reference) | | | |
| Data freshness (how stale or expired quotes were handled) | | | |
| Inspectability (what you could look up without rereading the whole conversation) | | | |
| Tool usage (which tools, how many calls) | | | |
| Input size (characters or reported tokens, per call and in total) | | | |

One run is one sample. These observations describe what happened in the runs you generated, and they do not establish that either implementation is more reliable. A simulated run shows how the data design behaves, and it does not measure what a language model can do. If you ran a live model, record the provider and model above and read the settings in `manifest.json`.

Summary (two or three sentences): [What does the state design give you here, what does it cost, and when would the transcript agent be enough?]
