"""Self-contained HTML comparison of two recorded runs.

The page embeds the runs' spans and selected artifacts as escaped JSON and
renders them with the shared viewer in static/traceview.js. It loads nothing
from the network and needs no file access, so it opens offline.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence import compare, key_facts, load_run
from .runner import display_path

STATIC = Path(__file__).resolve().parent / "static"
EMBEDDED_ARTIFACTS = (
    "events.json", "memory-loaded.json", "snapshot-before.json", "state.json", "state-diff.json", "context.json", "planner-input.json",
    "llm-request.json", "llm-response.json", "proposal.json", "gate.json", "tools.json", "receipt.json",
)


def run_payload(run_dir: Path) -> dict[str, Any]:
    view = load_run(run_dir)
    return {
        "run_dir": display_path(run_dir),
        "progress": view.progress,
        "manifest": view.manifest,
        "facts": key_facts(view),
        "spans": view.spans,
        "artifacts": {k: v for k, v in view.artifacts.items() if k in EMBEDDED_ARTIFACTS},
    }


def comparison_payload(before: Path, after: Path) -> dict[str, Any]:
    result = compare(before, after)
    return {
        "rows": result["rows"],
        "notices": result["notices"],
        "diffs": result["diffs"],
        "before": run_payload(before),
        "after": run_payload(after),
    }


def safe_json(data: Any) -> str:
    """JSON that can sit inside a <script> element without ending it."""
    text = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return (text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trace Comparison</title>
<style>{css}
main {{ max-width: 1500px; margin: 0 auto; padding: 16px; }}
</style>
</head>
<body>
<main>
  <h1>Trace comparison</h1>
  <p class="muted">Generated {generated} by <code>workshop visualize</code> from recorded OpenTelemetry SDK spans and run
  artifacts. Trace differences show which data the application supplied, what was proposed, and what was executed;
  they do not reveal a model's internal reasoning.</p>
  <div id="app"><noscript>This page needs JavaScript to render the embedded trace data.</noscript></div>
</main>
<script type="application/json" id="trace-data">{data}</script>
<script>{js}</script>
<script>
(function () {{
  var data = JSON.parse(document.getElementById("trace-data").textContent);
  TraceView.renderComparison(document.getElementById("app"), data);
}})();
</script>
</body>
</html>
"""


def render_comparison(before: Path, after: Path, output: Path) -> Path:
    payload = comparison_payload(Path(before), Path(after))
    html = _PAGE.format(
        css=(STATIC / "traceview.css").read_text(encoding="utf-8"),
        js=(STATIC / "traceview.js").read_text(encoding="utf-8"),
        data=safe_json(payload),
        generated=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    )
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return output


def selftest_html(path: Path) -> None:
    """Used by `workshop doctor`: escaping and template assembly work."""
    sample = safe_json({"x": "</script><b>"})
    assert "</script>" not in sample
    path.write_text(_PAGE.format(css="", js="", data=sample, generated="selftest"), encoding="utf-8")
