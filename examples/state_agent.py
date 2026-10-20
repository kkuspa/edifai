"""State-driven agent: explicit task state plus a context built for each planning step.

    uv run python examples/state_agent.py [--strategy reference] [--planner live]

Uses your workshop/exercises.py by default. Same as
`uv run workshop run --implementation state --strategy student --scenario changing-trip ...`.
"""

import sys

from workshop.cli import main

if __name__ == "__main__":
    sys.exit(main(["run", "--implementation", "state", "--scenario", "changing-trip", "--overwrite",
                   "--out", "runs/example-state", *sys.argv[1:]]))
