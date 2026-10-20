# Notes

A place to jot down what you noticed. Nobody collects this; use it to think, and bring it to the discussion if it helps. `submissions/evidence.md` lists the span IDs, event IDs, and trace trees from your runs if you want to look something up.

## Before and after

What did the baseline assume, and where in its context could you see the assumption?

Which event establishes the current commit?

Why does the earlier passing test fail to show that `b2` is ready?

Why does the earlier approval fail to authorize this deployment?

What changed between the baseline's proposal and the repaired one?

Did the deployment tool run in either case? How did the trace show it?

## The follow-ups

Once the tests passed for `b2`, why was that still not enough to deploy?

What did the approval for staging cover, and what did it leave out?

## What still holds

After the head moved to `b2`, which facts about `a1` remained true? Why did they stop deciding the next action?

## If you tried a live model

Did the model repeat the baseline's mistake? How did what you observed differ from what the simulator showed, and why might that be?

## For your own agent

What does the agent you are building for the final project remember between sessions? Where does that memory come from, and how would you rebuild it if it went stale?
