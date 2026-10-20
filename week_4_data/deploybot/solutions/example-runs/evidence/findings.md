# Findings

Name:
Interface used (CLI, GUI, or both):
Planner mode (simulated or live; if live, provider and model):

Cite IDs from your own runs. `submissions/evidence.md` lists them. A trace shows what data the application supplied, what was proposed, and what was executed; it does not show a model's internal reasoning.

| Question | Your answer | Evidence (span IDs, event IDs, artifact) |
|---|---|---|
| What did the baseline incorrectly assume? | | context.json of runs/before; context.build span ID |
| Which event establishes the current commit? | | event ID |
| Why doesn't the earlier test result establish readiness? | | commit of that test; current test event ID |
| Why doesn't the earlier approval authorize this deployment? | | approval commit and environment; event ID |
| What changed in the repaired proposal? | | planner.propose span ID, before and after |
| Did the deployment tool actually execute? | | tool.deploy span ID and receipt, or its absence and the gate outcome |
| Why are passing b2 tests insufficient by themselves? | | follow-up context; approval evidence |

## Which historical facts are still true?

After the head moved to b2, which facts about a1 remain true, and why do they no longer decide the next action?

## Observed versus intended

If you used the live planner, did the model reproduce the baseline mistake? Separate what you observed from what the deterministic fixture is designed to show.
