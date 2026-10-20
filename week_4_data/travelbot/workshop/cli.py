"""Command-line interface. Calls the application services directly (no HTTP).

    uv run workshop doctor
    uv run workshop run --implementation state --strategy student --scenario changing-trip --planner simulated --out runs/state
    uv run workshop check --strategy student
    uv run workshop compare --runs runs/transcript runs/state-fixed --output submissions/comparison.html
    uv run workshop session start my-state --implementation state
    uv run workshop session step my-state
    uv run workshop gui
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from .models import dollars
from .service import ServiceError, WorkshopService


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _status_text(st: dict[str, Any]) -> str:
    cfg, cur = st["config"], st["cursor"]
    lines = [
        f"Session {st['session']} ({st['path']})",
        f"  implementation={cfg['implementation']} planner={cfg['planner']}"
        + (f" provider={cfg['provider']}" if cfg.get("provider") else "")
        + (f" strategy={cfg['strategy']}" if cfg["implementation"] == "state" else "")
        + f" scenario={cfg['scenario']}",
        f"  phase={cur['phase']} turn={cur['turn']} step={cur['step_in_turn']} tool_calls_this_turn={cur['tool_calls_in_turn']} version={cur['version']} clock={st['clock']}",
    ]
    for t in st["scenario"]["turns"]:
        mark = "x" if t["applied"] else (">" if t["next"] else " ")
        lines.append(f"  [{mark}] turn {t['turn']} ({t['source']}): {t['text']}")
    lines.append("Conversation:")
    for item in st["conversation"]:
        if item["speaker"] in ("user", "notification"):
            lines.append(f"  t{item['turn']} {item['speaker']}: {item['text']}")
        elif item.get("proposal"):
            p = item["proposal"]
            v = p["validation"]
            lines.append(
                f"  t{item['turn']} assistant: {p['proposal_id']} flight {p['flight_id']} + hotel {p['hotel_id']}, total {dollars(p['computed_total_cents'])}; "
                f"hard={'pass' if v['hard_constraints_ok'] else 'FAIL'} fresh={'yes' if v['quotes_fresh'] else 'NO'} "
                f"preferences={'met' if v['preferences_met'] else 'unmet'} confirmation={p['confirmation_status']}"
            )
        else:
            lines.append(f"  t{item['turn']} assistant ({item['outcome']}): {item.get('text')}")
    ss = st.get("state_summary")
    if ss:
        dec = (ss["decisions"] or {}).get("hotel")
        lines.append("Task state:")
        lines.append(f"  party={ss['party']} requirements_revision={ss['requirements_revision']}")
        lines.append(f"  hotel decision: {json.dumps(dec) if dec else 'none'}")
        if ss["earlier_arrival"]:
            ea = ss["earlier_arrival"]
            lines.append(f"  earlier arrival: before {ea['reference_arrival_local']} (reference {ea['reference_proposal_id']})")
        lines.append("  current quotes: " + (", ".join(f"{q['quote_id']} {dollars(q['total_cents'])}" for q in ss["current_quotes"]) or "none"))
        lines.append("  invalidated: " + (", ".join(f"{q['quote_id']} [{q['status']}]" for q in ss["invalidated_quotes"]) or "none"))
        open_tasks = [t for t in ss["pending"] if t["status"] == "open"]
        lines.append("  open tasks: " + (", ".join(f"{t['kind']}({t['target']})" for t in open_tasks) or "none"))
    elif cfg["implementation"] == "transcript":
        lines.append("Task state: none (this implementation's memory is the transcript).")
    elif cfg["implementation"] == "one-shot":
        lines.append("Task state: none (each one-shot call sees only its own request; nothing carries between turns).")
    if st["can_apply_change"]:
        lines.append(f"Next: `workshop session apply-change {st['session']}` applies turn {st['next_change']['turn']}.")
    elif st["can_step"]:
        lines.append(f"Next: `workshop session step {st['session']}` takes one agent step.")
    else:
        lines.append("Scenario complete.")
    return "\n".join(lines)


def _op_text(result: dict[str, Any]) -> str:
    prefix = "(replayed, nothing executed) " if result.get("replayed") else ""
    detail = result.get("detail")
    items = detail if isinstance(detail, list) else [detail]
    lines = [f"{prefix}{result['kind']} -> phase={result['phase']} version={result['version']} op_id={result['op_id']}"]
    for d in items:
        if not d:
            continue
        r = d.get("result")
        if r == "change_applied" or "applied" in d:
            lines.append(f"  applied turn {d.get('turn')}: {', '.join(d.get('applied', [])) or 'no changes'}"
                         + (f"; unresolved: {d['unresolved']}" if d.get("unresolved") else ""))
        elif r == "tool_called":
            lines.append(f"  t{d['turn']} tool {d['tool']} ({d['tool_call_id']}) ok={d['ok']}")
        elif r == "plan_presented":
            lines.append(f"  t{d['turn']} plan presented {d['proposal_id']} total {dollars(d['total_cents'])} ({d['confirmation_status']})")
        elif r == "plan_rejected":
            lines.append(f"  t{d['turn']} plan rejected by validator: {', '.join(d['codes'])}")
        elif r == "question_asked":
            lines.append(f"  t{d['turn']} question: {d['question']}")
        elif r == "incomplete":
            lines.append(f"  t{d['turn']} INCOMPLETE: {d.get('reason') or d.get('errors') or d.get('codes')}")
        elif "message" in d:
            lines.append(f"  {d['message']}")
    return "\n".join(lines)


def _show(service: WorkshopService, args: argparse.Namespace) -> None:
    what = args.what
    if what == "state":
        data = service.state(args.session, args.index)
    elif what == "diff":
        data = service.diff(args.session, args.index)
        if not args.json and data.get("available"):
            print(f"Snapshot {data['index']} after {data['change_id']}:")
            for d in data["changes"]:
                print(f"  {d['op']:<8} {d['path']}: {json.dumps(d['before'], default=str)[:120]} -> {json.dumps(d['after'], default=str)[:120]}")
            return
    elif what == "model-input":
        calls = service.records(args.session, "model_calls")
        if not calls:
            print("No model calls yet.")
            return
        call = calls[-1] if args.index is None else next((c for c in calls if c["index"] == args.index), None)
        if call is None:
            raise ServiceError(f"No model call {args.index}")
        if args.json:
            _print_json(call)
            return
        mi = call["input"]
        print(f"Model call {call['index']} (turn {call['turn']}, step {call['step']}, {call['purpose']}; {call['input_chars']:,} chars; planner_mode={call['planner_mode']})")
        print(f"Trigger: {call['trigger']}")
        print("--- system ---")
        print(mi["system"])
        for m in mi["messages"]:
            label = m["role"] + (f" -> {m['tool_call']['name']}({json.dumps(m['tool_call']['arguments'])})" if m.get("tool_call") else "")
            print(f"--- {label} [turn {m['turn']}] ---")
            print(m["content"])
        print(f"--- returned action --- {json.dumps(call['action'])}")
        if call["errors"]:
            print(f"--- errors --- {call['errors']}")
        print(f"--- usage --- {call['usage'] or 'not reported'}; latency_ms={call['latency_ms']}")
        return
    elif what == "tools":
        data = service.records(args.session, "tool_calls")
        if not args.json:
            for t in data:
                print(f"{t['tool_call_id']} t{t['turn']} [{t['caller']}] {t['name']}({json.dumps(t['arguments'])})")
                print(textwrap.indent(json.dumps(t["result"], indent=2)[:3000], "    "))
            return
    elif what == "proposals":
        data = service.records(args.session, "plans")
    elif what == "validation":
        data = service.records(args.session, "validations")
    elif what == "transcript":
        data = service.transcript(args.session)
    elif what == "changes":
        data = service.changes(args.session)
    else:
        raise ServiceError(f"Unknown view {what}")
    _print_json(data)


def cmd_doctor(service: WorkshopService) -> int:
    ok = True

    def report(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"  {'ok  ' if passed else 'FAIL'} {label}" + (f": {detail}" if detail else ""))

    print("Workshop doctor (offline; no network calls)")
    try:
        import pydantic  # noqa: F401

        from .runner import list_scenarios
        from .tools import World

        world = World()
        report("imports and fixtures", True, f"{world.fixture_revision}; scenarios: {', '.join(s['name'] for s in list_scenarios())}")
    except Exception as exc:  # noqa: BLE001
        report("imports and fixtures", False, str(exc))
        return 1
    with tempfile.TemporaryDirectory(prefix="workshop-doctor-") as tmp:
        try:
            svc = WorkshopService(root=Path(tmp))
            svc.create("doctor", implementation="state", scenario="changing-trip", planner="simulated", strategy="reference")
            svc.run_to_completion("doctor")
            files = sorted(p.name for p in (Path(tmp) / "doctor").iterdir())
            report("persistence and simulated run", True, "reference state agent completed changing-trip")
            report("report generation", "report.html" in files, ", ".join(f for f in files if not f.startswith(".")))
        except Exception as exc:  # noqa: BLE001
            report("persistence and simulated run", False, f"{type(exc).__name__}: {exc}")
    try:
        from .runner import load_strategy

        load_strategy("student")
        report("student exercise module imports", True, "workshop/exercises.py")
    except Exception as exc:  # noqa: BLE001
        report("student exercise module imports", False, f"{type(exc).__name__}: {exc}")
    from .planners import live_provider_status

    status = live_provider_status()
    print(f"  info live credentials configured: anthropic={status['anthropic']} openai={status['openai']} (optional; not contacted)")
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401

        print("  info GUI extra installed: `uv run workshop gui` will work")
    except ImportError:
        print("  info GUI not installed (optional): `uv run --extra gui workshop gui` installs and launches it")
    print("Ready." if ok else "Problems found; see FAIL lines.")
    return 0 if ok else 1


def cmd_run(service: WorkshopService, args: argparse.Namespace) -> int:
    service.create(args.out, implementation=args.implementation, scenario=args.scenario, planner=args.planner, provider=args.provider,
                   strategy=args.strategy, restated_brief=args.restated_brief, overwrite=args.overwrite)
    if args.step:
        if not sys.stdin.isatty():
            print("--step needs an interactive terminal; running to completion instead.")
        else:
            print(_op_text(service.run_turn(args.out)))
            while True:
                st = service.status(args.out)
                if not st["can_apply_change"]:
                    break
                result = service.apply_change(args.out)
                print(_op_text(result))
                diff = service.diff(args.out)
                if diff.get("available"):
                    print(f"State changes from {diff['change_id']}:")
                    for d in diff["changes"][:40]:
                        print(f"  {d['op']:<8} {d['path']}")
                input("Paused after the change. Inspect with `workshop session show ...` in another terminal, then press Enter to run the turn. ")
                print(_op_text(service.run_turn(args.out)))
    result = service.run_to_completion(args.out) if service.status(args.out)["cursor"]["phase"] != "complete" else None
    if result:
        print(_op_text(result))
    st = service.status(args.out)
    print(_status_text(st))
    print(f"Artifacts: {service.path(args.out)}/report.html (and manifest.json, *.jsonl, checks.json)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="workshop", description="From prompt context to agent state: workshop CLI.")
    p.add_argument("--root", default="runs", help="Directory that holds named sessions (default: runs)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Check imports, fixtures, persistence, and report generation (offline)")

    def add_run_options(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--implementation", choices=["one-shot", "transcript", "state"], required=True)
        sp.add_argument("--scenario", default="changing-trip")
        sp.add_argument("--planner", choices=["simulated", "live"], default="simulated")
        sp.add_argument("--provider", choices=["anthropic", "openai"], default=None, help="Live provider (default: LLM_PROVIDER or whichever key is set)")
        sp.add_argument("--strategy", choices=["student", "reference"], default="student", help="Which apply_change/build_context to use (state agent)")
        sp.add_argument("--restated-brief", action="store_true", help="One-shot only: the caller restates the full brief every turn")

    run = sub.add_parser("run", help="Run a scenario to completion in a fresh session directory")
    add_run_options(run)
    run.add_argument("--out", required=True, help="Session directory to create, e.g. runs/state")
    run.add_argument("--overwrite", action="store_true", help="Replace an existing session at --out")
    run.add_argument("--step", action="store_true", help="Pause after each scenario change for inspection")

    chk = sub.add_parser("check", help="Check your exercise functions and see what is left to do")
    chk.add_argument("--strategy", choices=["student", "reference"], default="student")
    chk.add_argument("--output", default="submissions/check-results.json")
    chk.add_argument("--json", action="store_true")

    cmp_ = sub.add_parser("compare", help="Write a side-by-side HTML comparison of recorded runs")
    cmp_.add_argument("--runs", nargs="+", required=True)
    cmp_.add_argument("--output", default="submissions/comparison.html")

    sess = sub.add_parser("session", help="Work with a persistent session step by step (same sessions the GUI uses)")
    ssub = sess.add_subparsers(dest="action", required=True)
    start = ssub.add_parser("start", help="Create a session and apply the initial request")
    start.add_argument("session")
    add_run_options(start)
    start.add_argument("--overwrite", action="store_true")
    ssub.add_parser("list", help="List sessions under --root")
    for name, help_text in [
        ("status", "Show phase, conversation, plan, and state summary"),
        ("apply-change", "Apply the next scenario change (adds the child, accepts + earlier flight, expires the quote)"),
        ("step", "Take one agent step: one planner decision and its effect"),
        ("run-turn", "Step until the current turn ends"),
        ("run", "Apply changes and step until the scenario is complete"),
        ("reset", "Discard progress and restart the session with the same settings"),
        ("export", "Rewrite the session's artifacts (manifest, jsonl files, checks.json, report.html)"),
    ]:
        sp = ssub.add_parser(name, help=help_text)
        sp.add_argument("session")
        if name in ("apply-change", "step", "run-turn", "run", "reset"):
            sp.add_argument("--op-id", default=None, help="Operation ID; repeating one returns the stored result instead of re-executing")
        if name in ("apply-change", "step", "run-turn", "run", "reset"):
            sp.add_argument("--expected-version", type=int, default=None, help="Refuse if the session moved past this version")
        if name == "step":
            sp.add_argument("--count", type=int, default=1)
        sp.add_argument("--json", action="store_true")
    show = ssub.add_parser("show", help="Inspect recorded data")
    show.add_argument("session")
    show.add_argument("what", choices=["state", "diff", "model-input", "tools", "proposals", "validation", "transcript", "changes"])
    show.add_argument("--index", type=int, default=None, help="Snapshot index (state/diff) or model call index (model-input)")
    show.add_argument("--json", action="store_true")

    gui = sub.add_parser("gui", help="Launch the optional local GUI: uv run --extra gui workshop gui")
    gui.add_argument("--port", type=int, default=8765)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = WorkshopService(root=args.root)
    try:
        if args.command == "doctor":
            return cmd_doctor(service)
        if args.command == "run":
            return cmd_run(service, args)
        if args.command == "check":
            from .checks import format_report

            result = service.check(args.strategy, args.output)
            report = result["report"]
            _print_json(report) if args.json else print(format_report(report))
            print(f"Saved {args.output}")
            # Checks are feedback, not a gate: only a broken setup is a nonzero exit.
            return 1 if report["overall"] == "infrastructure_error" else 0
        if args.command == "compare":
            result = service.compare(args.runs, args.output)
            print(f"Wrote {result['output']}")
            return 0
        if args.command == "gui":
            try:
                from .gui.app import serve
            except ImportError as exc:
                print(f"The GUI is an optional extra. Launch it with: uv run --extra gui workshop gui ({exc})")
                return 2
            serve(root=Path(args.root), port=args.port)
            return 0
        if args.command == "session":
            a = args.action
            if a == "list":
                for s in service.list_sessions():
                    cfg = s["config"] or {}
                    print(f"{s['session']:<24} {cfg.get('implementation', '?'):<11} {cfg.get('planner', '?'):<10} {cfg.get('scenario', '?'):<14} phase={s['phase']} version={s['version']}")
                return 0
            if a == "start":
                result = service.create(args.session, implementation=args.implementation, scenario=args.scenario, planner=args.planner,
                                        provider=args.provider, strategy=args.strategy, restated_brief=args.restated_brief, overwrite=args.overwrite)
                print(_op_text(result))
                print(_status_text(service.status(args.session)))
                return 0
            if a == "status":
                st = service.status(args.session)
                _print_json(st) if args.json else print(_status_text(st))
                return 0
            if a == "show":
                _show(service, args)
                return 0
            if a == "export":
                result = service.export(args.session)
                print(f"Wrote {', '.join(result['files'])} in {service.path(args.session)}")
                return 0
            if a == "reset":
                result = service.reset(args.session, op_id=args.op_id, expected_version=args.expected_version)
            elif a == "apply-change":
                result = service.apply_change(args.session, op_id=args.op_id, expected_version=args.expected_version)
            elif a == "step":
                result = service.step(args.session, op_id=args.op_id, expected_version=args.expected_version)
                for _ in range(max(0, args.count - 1)):
                    if not service.status(args.session)["can_step"]:
                        break
                    print(_op_text(result))
                    result = service.step(args.session)
            elif a == "run-turn":
                result = service.run_turn(args.session, op_id=args.op_id, expected_version=args.expected_version)
            elif a == "run":
                result = service.run_to_completion(args.session, op_id=args.op_id, expected_version=args.expected_version)
            else:
                raise ServiceError(f"Unknown action {a}")
            _print_json(result) if args.json else print(_op_text(result))
            return 0
    except ServiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
