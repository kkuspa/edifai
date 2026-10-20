"""The complete CLI exercise works with the GUI dependencies unavailable."""

from __future__ import annotations

import subprocess
import sys

BLOCK_GUI = r"""
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("fastapi", "uvicorn", "starlette"):
            raise ImportError("GUI dependency blocked: " + name)
sys.meta_path.insert(0, Block())
from agent_state_workshop.cli import main
steps = [
    ["prepare", "--db", ".workshop/base.sqlite"],
    ["prepare", "--db", ".workshop/fixed.sqlite"],
    ["run", "--db", ".workshop/base.sqlite", "--strategy", "baseline", "--out", "runs/before"],
    ["run", "--db", ".workshop/fixed.sqlite", "--strategy", "reference", "--out", "runs/after"],
    ["trace", "--run", "runs/after", "--attributes"],
    ["compare", "--before", "runs/before", "--after", "runs/after"],
    ["append", "--db", ".workshop/fixed.sqlite", "--fixture", "tests-pass-b2"],
    ["run", "--db", ".workshop/fixed.sqlite", "--strategy", "reference", "--out", "runs/tests-pass"],
    ["export", "--before", "runs/before", "--after", "runs/after", "--followup", "runs/tests-pass"],
]
for argv in steps:
    code = main(argv)
    assert code == 0, (argv, code)
code = main(["check", "--strategy", "reference"])
assert code == 0, code
assert not any(m.split(".")[0] in ("fastapi", "uvicorn", "starlette") for m in sys.modules)
print("CLI-ONLY-OK")
"""


def test_full_cli_workflow_without_gui_dependencies(tmp_path):
    proc = subprocess.run([sys.executable, "-c", BLOCK_GUI], cwd=tmp_path, capture_output=True, text=True, timeout=180)
    assert "CLI-ONLY-OK" in proc.stdout, proc.stdout + proc.stderr
