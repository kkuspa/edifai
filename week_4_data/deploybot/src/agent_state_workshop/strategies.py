"""Memory strategies: baseline (flawed), student (exercises.py), reference (solutions/).

Student and reference code is compiled from source on every load, so a run
or check always uses the file as it is on disk right now. A long-lived
process such as the GUI server never needs a restart after an edit.
"""

from __future__ import annotations

import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import baseline
from .scenarios import repo_root

STRATEGIES = ("baseline", "student", "reference")


@dataclass(frozen=True)
class StructuredStrategy:
    name: str
    source_path: Path
    project_state: Callable[..., Any]
    build_context: Callable[..., Any]
    annotate_context_span: Callable[..., Any]
    kind: str = "structured"


@dataclass(frozen=True)
class SummaryStrategy:
    name: str = "baseline"
    kind: str = "summary"
    source_path: Path = Path(baseline.__file__)
    annotate_context_span: Callable[..., Any] = baseline.annotate_context_span


Strategy = StructuredStrategy | SummaryStrategy


class StrategyUnavailable(RuntimeError):
    pass


def exercises_path() -> Path:
    return Path(__file__).resolve().parent / "exercises.py"


def reference_path() -> Path:
    return repo_root() / "solutions" / "reference.py"


def strategy_source_path(name: str) -> Path:
    return {"baseline": Path(baseline.__file__), "student": exercises_path(), "reference": reference_path()}[name]


def strategy_kind(name: str) -> str:
    return "summary" if name == "baseline" else "structured"


def _compile_fresh(name: str, path: Path) -> types.ModuleType:
    if not path.exists():
        raise StrategyUnavailable(f"{path} does not exist")
    module = types.ModuleType(name)
    module.__file__ = str(path)
    code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
    exec(code, module.__dict__)  # noqa: S102 - workshop code from this checkout
    return module


def load_strategy(name: str) -> Strategy:
    if name == "baseline":
        return SummaryStrategy()
    if name == "student":
        path = exercises_path()
    elif name == "reference":
        path = reference_path()
    else:
        raise StrategyUnavailable(f"unknown strategy {name!r}; choose from {', '.join(STRATEGIES)}")
    module = _compile_fresh(f"workshop_strategy_{name}", path)
    return StructuredStrategy(
        name=name,
        source_path=path,
        project_state=module.project_state,
        build_context=module.build_context,
        annotate_context_span=module.annotate_context_span,
    )
