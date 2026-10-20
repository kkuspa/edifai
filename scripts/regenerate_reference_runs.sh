#!/usr/bin/env bash
# Regenerate the small committed reference artifacts in reference_runs/.
# Simulated planner, reference strategy; deterministic apart from generated run IDs.
set -euo pipefail
cd "$(dirname "$0")/.."
uv run workshop run --implementation one-shot --scenario initial --planner simulated --out reference_runs/one-shot --overwrite >/dev/null
uv run workshop run --implementation transcript --scenario changing-trip --planner simulated --out reference_runs/transcript --overwrite >/dev/null
uv run workshop run --implementation state --strategy reference --scenario changing-trip --planner simulated --out reference_runs/state-reference --overwrite >/dev/null
uv run workshop run --implementation state --strategy reference --scenario no-compliant --planner simulated --out reference_runs/state-reference-no-compliant --overwrite >/dev/null
uv run workshop compare --runs reference_runs/transcript reference_runs/state-reference --output reference_runs/comparison.html
echo "Reference runs written to reference_runs/"
