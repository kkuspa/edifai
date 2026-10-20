# Notes

A place to jot down what you noticed. Nobody collects this; use it to think, and bring it to the discussion if it helps. `uv run workshop session show NAME diff --index N` and `session show NAME model-input --index N` find the details again if you want to look something up.

## One request at a time

What did the one-shot planner need in its input to produce a good plan?

When the brief was restated before each call, which code did the restating, and where did it get the current requirements?

## When the child joined

Which data changed, and which stayed as it was?

What happened to the quotes issued for two adults? Were they still true, and did they still apply?

## The hotel the user chose

Where is the user's hotel choice recorded in your state agent? Where is it in the transcript agent?

What happened to that choice when the quote expired, and again when the refreshed price came back?

What does "earlier" refer to after the agent proposes a new flight?

## What the model saw

Which model input shows the difference between the conversation's history and the current working context?

Which tool did the agent call right after the quote expired, and what in its input justified that choice?

Which conclusions did code check rather than trust from the model?

## The two agents

Both agents reached the same plans. What differences remain in how each one updates its data, how much it sends the model, and how easily you can inspect what it knew?

## If you tried a live model

Did the transcript agent work out the current state from the conversation? How did what you observed differ from what the simulator showed, and why might that be?

## For your own agent

What does the agent you are building for the final project need to remember across turns? Which of that is a requirement, a decision, or an observation, and what should each kind of change update?
