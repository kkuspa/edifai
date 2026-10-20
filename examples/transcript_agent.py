"""Transcript-driven agent: the full conversation and every tool result are replayed each step.

    uv run python examples/transcript_agent.py [--planner live]

Same as `uv run workshop run --implementation transcript --scenario changing-trip ...`.
"""

import sys

from workshop.cli import main

if __name__ == "__main__":
    sys.exit(main(["run", "--implementation", "transcript", "--scenario", "changing-trip", "--overwrite",
                   "--out", "runs/example-transcript", *sys.argv[1:]]))
