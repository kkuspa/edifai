"""Comparison artifacts and offline HTML.

Everything here is rendered from the session's recorded execution. Nothing is invented: if a
provider did not report token usage, the report says so instead of estimating it.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .models import dollars
from .store import RunStore

SESSION_DB = "session.sqlite"


def json_diff(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """Leaf-level differences between two JSON values."""
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[dict[str, Any]] = []
        for key in list(before.keys()) + [k for k in after.keys() if k not in before]:
            sub = f"{path}.{key}" if path else str(key)
            if key not in after:
                out.append({"path": sub, "op": "removed", "before": before[key], "after": None})
            elif key not in before:
                out.append({"path": sub, "op": "added", "before": None, "after": after[key]})
            else:
                out.extend(json_diff(before[key], after[key], sub))
        return out
    if isinstance(before, list) and isinstance(after, list):
        out = []
        for i in range(max(len(before), len(after))):
            sub = f"{path}[{i}]"
            if i >= len(before):
                out.append({"path": sub, "op": "added", "before": None, "after": after[i]})
            elif i >= len(after):
                out.append({"path": sub, "op": "removed", "before": before[i], "after": None})
            else:
                out.extend(json_diff(before[i], after[i], sub))
        return out
    if before != after:
        return [{"path": path, "op": "changed", "before": before, "after": after}]
    return []


# --------------------------------------------------------------------------------------
# Artifact export
# --------------------------------------------------------------------------------------


def _jsonl(path: Path, rows: list[Any]) -> None:
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))


def export_artifacts(root: Path) -> list[str]:
    from .checks import scenario_checks

    store = RunStore(root / SESSION_DB)
    try:
        config = store.get("config")
        cursor = store.get("cursor")
        world = store.get("world")
        changes = [c.model_dump(mode="json") for c in store.changes()]
        model_calls = store.records("model_calls")
        settings = model_calls[0]["settings"] if model_calls else store.get("manifest_settings", {})
        manifest = {
            "implementation": config["implementation"],
            "planner_mode": config["planner"],
            "provider": config.get("provider"),
            "model_settings": settings,
            "strategy": config["strategy"] if config["implementation"] == "state" else None,
            "scenario": config["scenario"],
            "restated_brief": config.get("restated_brief", False),
            "fixture_revision": world["inventory"]["fixture_revision"],
            "clock": {"start": world["clock_start"], "now": world["clock_now"], "time_zone": world["inventory"]["destination_time_zone"]},
            "run_id": config["run_id"],
            "session": config["session_id"],
            "phase": cursor["phase"],
            "version": cursor["version"],
            "limits": {"max_tool_calls_per_turn": 8, "max_steps_per_turn": 12},
        }
        files = {
            "manifest.json": json.dumps(manifest, indent=2),
            "transcript.json": json.dumps([e.model_dump(mode="json") for e in store.transcript()], indent=2),
        }
        for name, text in files.items():
            (root / name).write_text(text)
        _jsonl(root / "changes.jsonl", [c for c in changes if c["source"] in ("user", "world")] + [{"world_event": e} for e in world["events"]])
        if config["implementation"] == "state":
            _jsonl(root / "state_snapshots.jsonl", store.snapshots())
        elif (root / "state_snapshots.jsonl").exists():
            (root / "state_snapshots.jsonl").unlink()
        _jsonl(root / "model_calls.jsonl", model_calls)
        _jsonl(root / "tool_calls.jsonl", store.records("tool_calls"))
        _jsonl(root / "plans.jsonl", store.records("plans"))
    finally:
        store.close()
    checks = scenario_checks(root)
    (root / "checks.json").write_text(json.dumps(checks, indent=2))
    (root / "report.html").write_text(render_report(run_summary(root)))
    written = ["manifest.json", "changes.jsonl", "transcript.json", "model_calls.jsonl", "tool_calls.jsonl", "plans.jsonl", "checks.json", "report.html"]
    if (root / "state_snapshots.jsonl").exists():
        written.insert(2, "state_snapshots.jsonl")
    return written


# --------------------------------------------------------------------------------------
# Summaries shared by the HTML report and the GUI
# --------------------------------------------------------------------------------------


def _action_summary(action: dict[str, Any] | None) -> str:
    if action is None:
        return "no valid action"
    if action["kind"] == "call_tool":
        args = ", ".join(f"{k}={v}" for k, v in action["arguments"].items() if v is not None)
        return f"call_tool {action['name']}({args})"
    if action["kind"] == "present_plan":
        p = action["plan"]
        return f"present_plan {p['flight_quote_id']} + {p['hotel_quote_id']} (claimed {dollars(p.get('claimed_total_cents'))})"
    return f"ask_user: {action['question']}"


def _tool_result_summary(result: dict[str, Any]) -> str:
    if not result.get("ok", True):
        return f"error: {result.get('error')}"
    if "validation" in result:
        v = result["validation"]
        return f"total {dollars(v['computed_total_cents'])}; codes: {', '.join(r['code'] for r in v['reasons']) or 'none'}"
    if "quotes" in result:
        quotes = ", ".join(f"{q['quote_id']} {dollars(q['total_cents'])}" for q in result["quotes"])
        extra = result.get("excluded") or result.get("ineligible") or []
        tail = f"; excluded: {', '.join(e['item_id'] + ' (' + e['reason'] + ')' for e in extra)}" if extra else ""
        unavailable = f"unavailable: {result.get('reason')}" if result.get("available") is False else ""
        return (quotes or unavailable or "no quotes") + tail
    if "entries" in result:
        return f"{len(result['entries'])} history entries"
    return json.dumps(result)[:200]


def run_summary(root: Path) -> dict[str, Any]:
    from .checks import scenario_checks

    store = RunStore(root / SESSION_DB)
    try:
        store.read().__enter__()
        config = store.get("config")
        cursor = store.get("cursor")
        world = store.get("world")
        transcript = store.transcript()
        model_calls = store.records("model_calls")
        tool_calls = store.records("tool_calls")
        plans = store.records("plans")
        outcomes = store.records("outcomes")
        snapshots = store.snapshots()
        changes = [c.model_dump(mode="json") for c in store.changes()]
    finally:
        store.close()

    tool_by_id = {t["tool_call_id"]: t for t in tool_calls}
    plan_by_call = {p["tool_call_id"]: p for p in plans}
    turn_ids = sorted({0} | {e.turn for e in transcript} | {o["turn"] for o in outcomes})
    turns = []
    for t in turn_ids:
        inputs = [
            {"role": e.role, "text": (e.normalized or {}).get("display", e.content),
             "changes": (e.normalized or {}).get("changes", []), "unresolved": (e.normalized or {}).get("unresolved", [])}
            for e in transcript if e.turn == t and e.role in ("user", "notification") and "display" in (e.normalized or {})
        ]
        steps = []
        for mc in (m for m in model_calls if m["turn"] == t):
            call_id = mc["action_call_id"]
            tool = tool_by_id.get(call_id)
            plan = plan_by_call.get(call_id)
            steps.append({
                "step": mc["step"], "purpose": mc["purpose"], "trigger": mc["trigger"],
                "action": _action_summary(mc["action"]), "action_kind": (mc["action"] or {}).get("kind"),
                "tool_name": (mc["action"] or {}).get("name"),
                "result": _tool_result_summary(tool["result"]) if tool else (
                    ("displayed as " + plan["proposal"]["proposal_id"]) if plan and plan.get("displayed")
                    else ("rejected: " + ", ".join(r["code"] for r in plan["validation"]["reasons"])) if plan else ""
                ),
                "input_chars": mc["input_chars"], "usage": mc["usage"], "latency_ms": mc["latency_ms"],
                "errors": mc["errors"], "attempts": mc["attempts"],
                "input": mc["input"], "tool_result": tool["result"] if tool else None,
            })
        outcome = next((o for o in outcomes if o["turn"] == t), None)
        displayed = next((p["proposal"] for p in plans if p["turn"] == t and p.get("displayed")), None)
        turns.append({
            "turn": t, "inputs": inputs, "steps": steps, "outcome": outcome, "proposal": displayed,
            "app_tool_calls": [tc for tc in tool_calls if tc["turn"] == t and tc["caller"] == "application"],
        })

    usage_in = [m["usage"]["input_tokens"] for m in model_calls if m.get("usage") and m["usage"].get("input_tokens") is not None]
    usage_out = [m["usage"]["output_tokens"] for m in model_calls if m.get("usage") and m["usage"].get("output_tokens") is not None]
    state_timeline = []
    for s in snapshots:
        diffs = json_diff(s["before"] or {}, s["after"])
        state_timeline.append({"index": s["index"], "change_id": s["change_id"], "diff": diffs, "after": s["after"]})

    return {
        "session": config["session_id"],
        "path": str(root),
        "config": config,
        "planner_mode": config["planner"],
        "settings": model_calls[0]["settings"] if model_calls else None,
        "phase": cursor["phase"],
        "clock": {"start": world["clock_start"], "now": world["clock_now"]},
        "fixture_revision": world["inventory"]["fixture_revision"],
        "turns": turns,
        "changes": [c for c in changes if c["source"] in ("user", "world")],
        "world_events": world["events"],
        "totals": {
            "model_calls": len(model_calls),
            "agent_tool_calls": sum(1 for tc in tool_calls if tc["caller"] == "agent"),
            "application_tool_calls": sum(1 for tc in tool_calls if tc["caller"] == "application"),
            "input_chars": sum(m["input_chars"] for m in model_calls),
            "input_tokens": sum(usage_in) if usage_in else None,
            "output_tokens": sum(usage_out) if usage_out else None,
            "usage_reported": bool(usage_in),
        },
        "state_timeline": state_timeline,
        "final_state": snapshots[-1]["after"] if snapshots else None,
        "checks": scenario_checks(root),
    }


# --------------------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------------------

CSS = """
:root { --fg:#1d1d1f; --muted:#5f6368; --line:#d9dce1; --bg:#ffffff; --panel:#f6f7f9; --ok:#1e7d32; --bad:#b3261e; --warn:#8a5a00; --accent:#1a4f8b; }
@media (prefers-color-scheme: dark) { :root { --fg:#e8eaed; --muted:#a0a4aa; --line:#3a3d42; --bg:#16181b; --panel:#1f2226; --ok:#7cc48a; --bad:#f28b82; --warn:#e6b85c; --accent:#8ab4f8; } }
* { box-sizing: border-box; }
body { font-family: Helvetica, Arial, sans-serif; color: var(--fg); background: var(--bg); margin: 0; padding: 24px 16px 64px; line-height: 1.45; }
main { max-width: 1200px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 18px; margin: 32px 0 8px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }
h3 { font-size: 15px; margin: 16px 0 6px; }
.muted { color: var(--muted); } .ok { color: var(--ok); } .bad { color: var(--bad); } .warn { color: var(--warn); }
.badge { display: inline-block; padding: 2px 8px; border: 1px solid currentColor; border-radius: 3px; font-size: 12px; font-weight: bold; letter-spacing: .04em; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 6px 0 12px; }
th, td { text-align: left; vertical-align: top; padding: 5px 8px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: normal; }
pre { white-space: pre-wrap; word-break: break-word; background: var(--panel); padding: 10px; font-size: 12px; max-height: 480px; overflow: auto; }
details { margin: 4px 0; } summary { cursor: pointer; color: var(--accent); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
.card { border: 1px solid var(--line); padding: 10px 12px; background: var(--panel); }
.turn { border-left: 3px solid var(--line); padding-left: 12px; margin: 12px 0; }
"""


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _mode_badge(mode: str) -> str:
    cls = "warn" if mode == "simulated" else "ok"
    label = "planner_mode=simulated (rule-based; not an LLM measurement)" if mode == "simulated" else "planner_mode=live"
    return f'<span class="badge {cls}">{e(label)}</span>'


def _proposal_card(p: dict[str, Any] | None) -> str:
    if not p:
        return '<p class="muted">No plan displayed.</p>'
    v = p["validation"]

    def flag(ok: bool, yes: str, no: str) -> str:
        return f'<span class="{"ok" if ok else "bad"}">{e(yes if ok else no)}</span>'

    reasons = "".join(f"<li>{e(r['category'])}: {e(r['code'])} ({e(r['message'])})</li>" for r in v["reasons"]) or "<li>none</li>"
    return (
        f'<div class="card"><strong>{e(p["proposal_id"])}</strong>: flight {e(p["flight_id"])} ({e(p["flight_quote_id"])}) + '
        f'hotel {e(p["hotel_id"])} ({e(p["hotel_quote_id"])})<br>'
        f'Computed total <strong>{e(dollars(p["computed_total_cents"]))}</strong> (claimed {e(dollars(p.get("claimed_total_cents")))})<br>'
        f'Hard constraints: {flag(v["hard_constraints_ok"], "pass", "fail")} &middot; '
        f'Quote freshness: {flag(v["quotes_fresh"], "fresh", "stale")} &middot; '
        f'Preferences: {flag(v["preferences_met"], "met", "unmet")} &middot; '
        f'Confirmation: <span class="{"warn" if p["confirmation_status"] == "reconfirmation_required" else "muted"}">{e(p["confirmation_status"])}</span>'
        f'<details><summary>Reason codes</summary><ul>{reasons}</ul></details>'
        f'<details><summary>Explanation shown to the user</summary><p>{e(p["explanation"])}</p></details></div>'
    )


def _outcome_line(o: dict[str, Any] | None) -> str:
    if not o:
        return '<p class="muted">Turn not finished.</p>'
    if o["kind"] == "plan_presented":
        return f'<p class="ok">Plan presented: {e(o["proposal_id"])} ({o["tool_calls"]} tool calls, {o["steps"]} steps)</p>'
    if o["kind"] == "question_asked":
        return f'<p class="warn">Question asked ({e(o["unresolved_constraint"])}): {e(o["question"])}</p>'
    return f'<p class="bad">Incomplete: {e(o["reason"])}</p>'


def _steps_table(steps: list[dict[str, Any]], app_calls: list[dict[str, Any]]) -> str:
    rows = []
    for c in app_calls:
        rows.append(f'<tr><td>app</td><td class="muted">application prefetch</td><td>{e(c["name"])}</td><td>{e(_tool_result_summary(c["result"]["flights"]))} / {e(_tool_result_summary(c["result"]["hotels"]))}</td><td></td></tr>')
    for s in steps:
        usage = s["usage"] or {}
        size = f'{s["input_chars"]:,} chars' + (f', {usage["input_tokens"]:,} input tokens' if usage.get("input_tokens") is not None else "")
        exact = json.dumps(s["input"], indent=2)
        result_json = json.dumps(s["tool_result"], indent=2) if s["tool_result"] is not None else ""
        errs = f'<br><span class="bad">{e("; ".join(s["errors"]))}</span>' if s["errors"] else ""
        rows.append(
            f'<tr><td>{s["step"]}</td><td>{e(s["purpose"])}<br><span class="muted">{e(s["trigger"])}</span></td>'
            f'<td>{e(s["action"])}{errs}</td><td>{e(s["result"])}'
            + (f'<details><summary>tool result</summary><pre>{e(result_json)}</pre></details>' if result_json else "")
            + f'</td><td>{e(size)}<details><summary>exact model input</summary><pre>{e(exact)}</pre></details></td></tr>'
        )
    return "<table><tr><th>Step</th><th>Purpose</th><th>Action</th><th>Result</th><th>Model input</th></tr>" + "".join(rows) + "</table>"


def _checks_table(checks: list[dict[str, Any]]) -> str:
    rows = "".join(
        f'<tr><td class="{"ok" if c["passed"] else ("muted" if c["passed"] is None else "bad")}">'
        f'{"PASS" if c["passed"] else ("n/a" if c["passed"] is None else "FAIL")}</td><td>{e(c["id"])}</td><td>{e(c["description"])}</td><td>{e(c["detail"])}</td></tr>'
        for c in checks
    )
    return f"<table><tr><th></th><th>Check</th><th>Description</th><th>Detail</th></tr>{rows}</table>"


def _usage_text(totals: dict[str, Any]) -> str:
    tokens = (f"{totals['input_tokens']:,} input / {totals['output_tokens']:,} output tokens (provider reported)"
              if totals["usage_reported"] else "token usage not reported (simulated planner)")
    return (f"{totals['model_calls']} model calls, {totals['agent_tool_calls']} agent tool calls, "
            f"{totals['application_tool_calls']} application prefetches, {totals['input_chars']:,} input characters; {tokens}")


def _state_section(summary: dict[str, Any]) -> str:
    if not summary["state_timeline"]:
        return ('<p class="muted">This implementation keeps no structured task state. Its only memory is the transcript '
                '(transcript.json), which is replayed to the model in full on every step.</p>')
    parts = []
    for s in summary["state_timeline"]:
        rows = "".join(
            f'<tr><td>{e(d["op"])}</td><td>{e(d["path"])}</td><td>{e(json.dumps(d["before"], default=str)[:200])}</td><td>{e(json.dumps(d["after"], default=str)[:200])}</td></tr>'
            for d in s["diff"] if not d["path"].startswith(("applied_change_ids", "revision", "last_seq"))
        )
        parts.append(f'<details><summary>{e(s["change_id"])} ({len(s["diff"])} leaf changes)</summary>'
                     f'<table><tr><th>op</th><th>path</th><th>before</th><th>after</th></tr>{rows}</table></details>')
    final = summary["final_state"] or {}
    decision = (final.get("decisions") or {}).get("hotel")
    return (
        f'<h3>Retained hotel decision</h3><pre>{e(json.dumps(decision, indent=2))}</pre>'
        f'<h3>State transitions</h3>' + "".join(parts)
    )


def render_report(summary: dict[str, Any]) -> str:
    cfg = summary["config"]
    turns_html = []
    for t in summary["turns"]:
        inputs = "".join(
            f'<p><strong>{e(i["role"])}:</strong> {e(i["text"])}</p>'
            + (f'<details><summary>normalized changes</summary><pre>{e(json.dumps(i["changes"], indent=2))}</pre></details>' if i["changes"] else "")
            + (f'<p class="bad">Unresolved: {e(json.dumps(i["unresolved"]))}</p>' if i["unresolved"] else "")
            for i in t["inputs"]
        )
        turns_html.append(
            f'<div class="turn"><h3>Turn {t["turn"]}</h3>{inputs or "<p class=muted>(one-shot request built at step time)</p>"}'
            f'{_steps_table(t["steps"], t["app_tool_calls"])}{_outcome_line(t["outcome"])}{_proposal_card(t["proposal"])}</div>'
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run Report</title><style>{CSS}</style></head><body><main>
<h1>Run report: {e(cfg["implementation"])} agent, {e(cfg["scenario"])}</h1>
<p>{_mode_badge(summary["planner_mode"])} <span class="muted">session {e(summary["session"])} &middot; run {e(cfg["run_id"])} &middot;
fixture {e(summary["fixture_revision"])} &middot; clock {e(summary["clock"]["start"])} to {e(summary["clock"]["now"])} &middot; phase {e(summary["phase"])}</span></p>
<p class="muted">Model settings: {e(json.dumps(summary["settings"]))}</p>
<p>{e(_usage_text(summary["totals"]))}</p>
<h2>Turn-by-turn</h2>{"".join(turns_html)}
<h2>Task state</h2>{_state_section(summary)}
<h2>Independent checks</h2>{_checks_table(summary["checks"])}
<p class="muted">Rendered from recorded execution data in {e(summary["path"])}. Synthetic inventory; not travel advice.</p>
</main></body></html>"""


def compare_runs(roots: list[Path], output: Path) -> Path:
    summaries = [run_summary(r) for r in roots]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_comparison(summaries))
    return output


def render_comparison(summaries: list[dict[str, Any]]) -> str:
    header = "".join(
        f'<div class="card"><strong>{e(s["config"]["implementation"])}</strong> &middot; {e(s["session"])}<br>{_mode_badge(s["planner_mode"])}<br>'
        f'<span class="muted">{e(json.dumps(s["settings"]))}</span><br>{e(_usage_text(s["totals"]))}</div>'
        for s in summaries
    )
    settings = {json.dumps(s["settings"], sort_keys=True) for s in summaries}
    settings_note = ("" if len(settings) == 1 else
                     '<p class="warn">Model settings differ between these runs; differences in outcome may come from settings, not data design.</p>')
    all_turns = sorted({t["turn"] for s in summaries for t in s["turns"]})
    rows = []
    for turn in all_turns:
        cells = []
        for s in summaries:
            t = next((x for x in s["turns"] if x["turn"] == turn), None)
            if t is None:
                cells.append('<div class="card muted">not reached</div>')
                continue
            steps = "".join(f"<li>{e(st['action'])}<br><span class='muted'>{e(st['result'])}</span></li>" for st in t["steps"])
            chars = sum(st["input_chars"] for st in t["steps"])
            cells.append(
                f'<div class="card"><ol>{steps}</ol><p class="muted">{len(t["steps"])} model calls, {chars:,} input characters this turn</p>'
                f'{_outcome_line(t["outcome"])}{_proposal_card(t["proposal"])}</div>'
            )
        first_inputs = next((x["inputs"] for s in summaries for x in s["turns"] if x["turn"] == turn and x["inputs"]), [])
        label = " / ".join(i["text"] for i in first_inputs) or "initial request"
        rows.append(f'<h3>Turn {turn}: {e(label)}</h3><div class="grid">{"".join(cells)}</div>')
    state_cells = "".join(f'<div class="card"><strong>{e(s["config"]["implementation"])}</strong>{_state_section(s)}</div>' for s in summaries)
    checks_cells = "".join(f'<div class="card"><strong>{e(s["config"]["implementation"])}</strong>{_checks_table(s["checks"])}</div>' for s in summaries)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run Comparison</title><style>{CSS}</style></head><body><main>
<h1>Run comparison</h1>
<p class="muted">Each column is rendered from that run's recorded execution. One run is one sample; a single stochastic run does not establish reliability or superiority.</p>
{settings_note}<div class="grid">{header}</div>
<h2>Turn-by-turn, side by side</h2>{"".join(rows)}
<h2>Preserved decisions and state</h2><div class="grid">{state_cells}</div>
<h2>Independent checks</h2><div class="grid">{checks_cells}</div>
</main></body></html>"""
