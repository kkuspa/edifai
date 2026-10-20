# Workshop reference

The [README](../README.md) walks through the exercise step by step. This page collects the background you may want beside it: the scenario's facts and numbers, the rules a complete solution follows, and fixes for common snags.

## The scenario's facts

The trip runs from April 12 to April 15, 2027: three nights, RDU to Chicago, round trip. Times of day are Chicago time (America/Chicago). Every run starts the same fixture clock at 2027-03-15 14:00 UTC, and the scenario moves it forward by fixed amounts, so nothing depends on the time on your machine.

These rules hold throughout:

- Flight prices are per traveler and cover the round trip. A child needs a seat and pays the same fare.
- Hotel prices cover all three nights, and a child counts toward the room's occupancy.
- The budget covers flights and the hotel only. The total must not exceed $1,800, so a total of $1,800 fits.
- Dates, seats, occupancy, and the budget are hard constraints. The walking distance and an earlier arrival are preferences.
- "Earlier" means an outbound arrival in Chicago strictly before the arrival of the flight the user was looking at when they asked.
- A quote holds only for the party and dates it was issued for, and only until it expires (after 120 minutes on the fixture clock) or the inventory reprices the item.

## The changes that matter

| Turn | What happens | What it should affect |
|---|---|---|
| 0 | The initial request for two adults | Nothing yet; the agent searches and proposes `plan-001` |
| 1 | The user adds a child | The party, every party-scoped quote, the capacity of each room, and the proposal's total |
| 2 | The user accepts the hotel and asks for an earlier flight | A new hotel decision and a new arrival preference, anchored to the flight in `plan-002` |
| 3 | The accepted hotel's quote expires and reprices | That quote's validity, the proposal's total, and whether the user must confirm the hotel again |

Turn 2 is one user message that yields two normalized changes, applied in order with no planning step between them. The application resolves "that hotel" to the hotel in the proposal it displayed, so your code receives an explicit hotel ID.

## The inventory

| Item | Facts | Price |
|---|---|---|
| `f1` | Nonstop, lands 18:00 | $300 per traveler |
| `f2` | Nonstop, lands 12:00 | $320 per traveler |
| `f3` | One stop in Charlotte, lands 15:00 | $280 per traveler |
| `f4` | Nonstop, lands 08:45 | $540 per traveler |
| `f5` | Nonstop, lands 13:30, two seats left | $310 per traveler |
| `f6` | Flies a day later than the trip | $250 per traveler |
| `h11` | Sleeps 2, 8-minute walk | $600 |
| `h17` | Sleeps 3, 10-minute walk | $660 for two, $720 for three; $900 for three after it reprices |
| `h22` | Sleeps 4, 25-minute walk | $500 |
| `h30` | Sleeps 3, 6-minute walk | $1,150 |
| `h41` | Sleeps 3, 12-minute walk, sold out | none |

The reference solution produces this path. Other plans are fine when they meet the hard constraints and disclose their tradeoffs.

| Turn | Plan | Total | Why |
|---|---|---:|---|
| 0 | `f1` × 2 + `h11` | $1,200 | The cheapest nonstop and the cheapest room within the walk |
| 1 | `f1` × 3 + `h17` | $1,620 | `h11` sleeps two, and every price now covers three travelers |
| 2 | `f2` × 3 + `h17` | $1,680 | Keeps the accepted hotel and lands before 18:00 |
| 3 | `f2` × 3 + `h17` at $900 | $1,860 | Over budget; the validator rejects it, and the user never sees it |
| 3 | `f3` × 3 + `h17` at $900 | $1,740 | Lands at 15:00, discloses the connection and the price change, and asks the user to reconfirm the hotel |

In the `no-compliant` scenario the hotel reprices to $1,200 instead. The cheapest flight with it brings the total to $2,040, so the agent asks the user whether to raise the budget or look at other hotels.

## The data rules

A complete `apply_change` and `build_context` follow these rules.

1. A party change replaces the party and nothing else among the requirements. The origin, destination, dates, budget, and walking preference carry over.
2. Quotes belong to a party and dates. When the party changes, each current quote for the old party becomes stale. It stays on record for comparison, but it no longer prices the trip.
3. A room too small for the party becomes unsuitable, and the state keeps the reason.
4. When a change invalidates the inputs to the current proposal, the proposal is flagged for a recheck, and the remaining work appears as pending tasks: refresh a quote, find a room, search for an earlier flight, revalidate, or confirm with the user.
5. A hotel acceptance is a decision of its own. It records the hotel, the proposal it came from, the quote, and the accepted price. It is not a booking, and it does not accept a different price.
6. An earlier-arrival request records the arrival time of the flight in the proposal the user saw. Later proposals never move that reference.
7. An expired quote is marked expired. The user's choice of hotel survives, and a task to refresh the quote opens.
8. When a fresh quote prices the accepted hotel differently, the decision needs the user's confirmation again.
9. The context shows current data as current and keeps stale quotes, earlier proposals, and notes in a section marked historical. Required inputs that are unknown appear in their own section, so the model asks instead of guessing.

The helpers in `workshop/state.py` handle the bookkeeping, such as change IDs, task IDs, and closing a task once the state shows it done. Your code decides what each change affects.

## What the model sees

The state agent's context is a list of named sections, each marked current, historical, or instructions. The simulator and the report understand these names: `purpose`, `requirements`, `unknowns`, `decisions`, `current_quotes`, `unsuitable_items`, `current_proposal`, `pending_work`, `validation_feedback`, `historical`, and `history_access`. The simulator trusts what the context says. If a stale quote appears under `current_quotes`, the simulator uses it, and the validator rejects the plan. A live model might notice the mismatch, and comparing the two is one of the experiments in the README.

## Common snags

| Message | What to do |
|---|---|
| `... already holds a session` | Choose another name or `--out`, run `uv run workshop session reset NAME`, or add `--overwrite`. |
| `... exists, is not empty, and is not a session directory` | The path holds other files. Choose another path. |
| `Cannot step while the session is 'awaiting_change'` | The turn ended. Apply the next change with `session apply-change NAME`, or finish with `session run NAME`. |
| `Cannot apply a change while the session is 'ready'` | The agent has not finished the current turn. Use `session step NAME` or `session run-turn NAME`. |
| `Your exercise code raised ...` | Fix the line the message names. The failed action saved nothing, so continue with `session run NAME` afterwards. |
| A turn ends `incomplete` at the tool-call limit | The agent kept proposing plans the validator rejected. Look at `session show NAME validation` and the latest `model-input` to see what it was given. |
| The web interface says the session is at a newer version | Another tab or a terminal command changed the session. The page has refreshed, so press the button again. |
| `No session at ...` for a path in `reference_runs/` | Fresh clones lack the session databases. Run `bash scripts/regenerate_reference_runs.sh`. |
