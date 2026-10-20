"""Terminal rendering of recorded spans: hierarchy, durations, attributes, evidence IDs."""

from __future__ import annotations

from typing import Any

from .evidence import EVENT_ID_RE

BAR_WIDTH = 24


def build_tree(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nest spans by parent_span_id. Orphans (parent not recorded) become roots."""
    by_id = {s["span_id"]: {**s, "children": []} for s in spans}
    roots = []
    for node in by_id.values():
        parent = by_id.get(node.get("parent_span_id") or "")
        (parent["children"] if parent else roots).append(node)
    for node in by_id.values():
        node["children"].sort(key=lambda n: n["start_time_unix_nano"])
    roots.sort(key=lambda n: n["start_time_unix_nano"])
    return roots


def duration_ms(span: dict[str, Any]) -> float:
    return (span["end_time_unix_nano"] - span["start_time_unix_nano"]) / 1e6


def _fmt_ms(ms: float) -> str:
    if ms >= 60_000:
        return f"{ms / 60_000:.1f} min"
    if ms >= 1000:
        return f"{ms / 1000:.2f} s"
    return f"{ms:.2f} ms"


def _bar(span: dict[str, Any], t0: int, total: int) -> str:
    if total <= 0:
        return ""
    start = int((span["start_time_unix_nano"] - t0) / total * BAR_WIDTH)
    length = max(1, int(duration_ms(span) * 1e6 / total * BAR_WIDTH))
    start = min(start, BAR_WIDTH - 1)
    length = min(length, BAR_WIDTH - start)
    return "." * start + "#" * length + "." * (BAR_WIDTH - start - length)


def _value(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(str(x) for x in v) + "]"
    return str(v)


def render_tree_text(spans: list[dict[str, Any]], show_attributes: bool = False, only: str | None = None) -> str:
    if not spans:
        return "(no spans recorded)"
    roots = build_tree(spans)
    t0 = min(s["start_time_unix_nano"] for s in spans)
    total = max(s["end_time_unix_nano"] for s in spans) - t0
    lines = [f"trace {spans[0]['trace_id']}  ({len(spans)} spans; bar = position within the trace)"]

    def walk(node: dict[str, Any], prefix: str, last: bool, depth: int) -> None:
        branch = "" if depth == 0 else ("`- " if last else "|- ")
        status = node["status"]["code"]
        status_txt = "" if status == "UNSET" else f"  status={status}"
        name = f"{prefix}{branch}{node['name']}"
        lines.append(f"{name:<34} {node['span_id']}  {_fmt_ms(duration_ms(node)):>10}  {_bar(node, t0, total)}{status_txt}")
        child_prefix = prefix + ("" if depth == 0 else ("   " if last else "|  "))
        detail = child_prefix + ("|     " if node["children"] else "      ")
        if show_attributes and (only is None or node["name"] == only):
            for key in sorted(node["attributes"]):
                lines.append(f"{detail}{key} = {_value(node['attributes'][key])}")
            for ev in node.get("events", []):
                attrs = ", ".join(f"{k}={_value(v)}" for k, v in ev["attributes"].items())
                lines.append(f"{detail}event {ev['name']}: {attrs}")
        ids = evidence_ids(node)
        if ids:
            lines.append(f"{detail}evidence IDs: {', '.join(ids)}")
        for i, child in enumerate(node["children"]):
            walk(child, child_prefix, i == len(node["children"]) - 1, depth + 1)

    for root in roots:
        walk(root, "", True, 0)
    return "\n".join(lines)


def evidence_ids(span: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for v in span.get("attributes", {}).values():
        for item in (v if isinstance(v, (list, tuple)) else [v]):
            found += EVENT_ID_RE.findall(str(item))
    for ev in span.get("events", []):
        for v in ev.get("attributes", {}).values():
            for item in (v if isinstance(v, (list, tuple)) else [v]):
                found += EVENT_ID_RE.findall(str(item))
    return list(dict.fromkeys(found))
