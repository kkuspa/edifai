"""One-shot planner: one well-scoped model input, one structured plan, no tool loop.

    uv run python examples/one_shot.py                  # bounded initial request
    uv run python examples/one_shot.py --restated-brief # the caller rebuilds the brief every turn

Same as `uv run workshop run --implementation one-shot ...`. Ask yourself who does the
reconstruction when --restated-brief is on.
"""

import sys

from workshop.cli import main

if __name__ == "__main__":
    restated = "--restated-brief" in sys.argv
    args = ["run", "--implementation", "one-shot", "--planner", "simulated", "--overwrite",
            "--scenario", "changing-trip" if restated else "initial",
            "--out", "runs/example-one-shot" + ("-restated" if restated else "")]
    if restated:
        args.append("--restated-brief")
    sys.exit(main(args + [a for a in sys.argv[1:] if a != "--restated-brief"]))
