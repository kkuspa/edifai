"""`workshop` command-line interface.

Every command calls the shared services in service.py directly (never over
HTTP). Commands that change state run as operations with an operation ID;
pass --op-id to make a scripted retry safe.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import __version__

PUBLIC_COMMANDS = (
    "doctor", "prepare", "reset", "append", "run", "step", "abandon", "status", "events", "show",
    "trace", "diff", "compare", "visualize", "export", "check", "trace-url", "ops", "gui", "examples",
)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path.cwd() / ".env", override=False)
    except ImportError:  # pragma: no cover
        pass


def _ws() -> Any:
    from .service import Workshop

    return Workshop(Path.cwd(), interface="cli")


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str))


def _op(kind: str, params: dict[str, Any], op_id: str | None) -> dict[str, Any]:
    record = _ws().execute(kind, params, op_id)
    if record["status"] == "failed":
        print(f"error: {record['error']}", file=sys.stderr)
        raise SystemExit(1)
    if record["status"] not in ("done",):
        print(f"operation {record['op_id']} is {record['status']}", file=sys.stderr)
        raise SystemExit(1)
    return record["result"]


# ----------------------------------------------------------------- printing


def _rel(path: str) -> str:
    try:
        return os.path.relpath(path)
    except ValueError:
        return path


def print_run(result: dict[str, Any]) -> int:
    for st in result.get("executed_stages", []):
        mark = {"done": "ok  ", "skipped": "skip", "failed": "FAIL"}[st["status"]]
        detail = st["summary"].get("reason", "") if st["status"] == "skipped" else (st.get("error") or "")
        dur = f"{st['duration_ms']:.2f} ms" if st.get("duration_ms") is not None else ""
        print(f"  [{mark}] {st['stage']:<16} {st['span_id'] or '':<17} {dur:>10}  {detail}")
    print(f"run       : {result['run_id']} (strategy={result['strategy']}, planner_mode={result['planner_mode']}, "
          f"target={result['target_environment']})")
    p, g, r = result.get("proposal"), result.get("gate"), result.get("receipt")
    if p:
        print(f"proposal  : {p['action']} {p['commit']} -> {p['environment']}"
              + (f"  blockers: {', '.join(p['blockers'])}" if p.get("blockers") else ""))
        print(f"            \"{p['explanation']}\"")
    if g:
        print(f"gate      : {g['outcome'].upper()} ({', '.join(g['reason_codes'])})")
    if result["status"] in ("completed", "failed") or r:
        print(f"executed  : {'mock deploy, receipt ' + r['receipt_id'] + (' (existing receipt reused)' if r.get('reused_existing') else '') if r else 'nothing (no deployment)'}")
    ann = result.get("annotation")
    if ann and not ann["complete"]:
        print(f"warning   : context.build annotation incomplete, missing {', '.join(ann['missing'])}")
    print(f"trace     : {result['trace_id']}")
    if result.get("jaeger_url"):
        print(f"jaeger    : {result['jaeger_url']}")
    print(f"artifacts : {_rel(result['run_dir'])}")
    if result["status"] == "active":
        print(f"next stage: {result['next_stage']}  (run `workshop step --db ...` again, or add --until-done)")
    if result["status"] == "failed":
        if result.get("exercise_incomplete"):
            print(f"\nUnfinished exercise: {result['error']}\n"
                  "This is expected until you complete src/agent_state_workshop/exercises.py.\n"
                  "The runtime is fine; run `uv run workshop check --strategy student` to see what is left.",
                  file=sys.stderr)
            return 2
        print(f"\nrun failed: {result['error']}", file=sys.stderr)
        return 1
    return 0


# ----------------------------------------------------------------- commands


def cmd_doctor(args: argparse.Namespace) -> int:
    from .planners import provider_status
    from .telemetry import otlp_endpoint_from_env

    ok = True

    def line(status: str, text: str) -> None:
        print(f"  [{status}] {text}")

    print(f"agent-state-workshop {__version__}, Python {sys.version.split()[0]}")
    if sys.version_info < (3, 12):
        ok = False
        line("FAIL", "Python 3.12+ is required")
    try:
        import opentelemetry.sdk.version as otel_version
        import pydantic

        line("ok", f"imports: pydantic {pydantic.VERSION}, opentelemetry-sdk {otel_version.__version__}")
    except ImportError as exc:
        ok = False
        line("FAIL", f"import error: {exc}. Run `uv sync --frozen`.")
    from .scenarios import fixtures_dir

    line("ok" if (fixtures_dir() / "stale-resume").exists() else "FAIL", f"fixtures: {fixtures_dir()}")
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
        try:
            from .store import EventStore

            with EventStore(Path(tmp) / "doctor.sqlite") as store:
                store.set_meta("doctor", "ok")
            line("ok", "SQLite database is writeable in this directory")
        except Exception as exc:
            ok = False
            line("FAIL", f"SQLite: {exc}")
        try:
            from .telemetry import RunTelemetry, new_span_id, new_trace_id, read_spans

            tel = RunTelemetry(Path(tmp), new_trace_id(), new_span_id(), None)
            with tel.stage_span("doctor.check"):
                pass
            tel.shutdown()
            spans = read_spans(Path(tmp) / "spans.jsonl")
            line("ok" if spans else "FAIL", "OpenTelemetry SDK spans export to local JSONL")
        except Exception as exc:
            ok = False
            line("FAIL", f"local span export: {exc}")
        try:
            from .visualize import selftest_html

            selftest_html(Path(tmp) / "t.html")
            line("ok", "offline HTML trace renderer works")
        except Exception as exc:
            ok = False
            line("FAIL", f"HTML renderer: {exc}")

    endpoint = otlp_endpoint_from_env()
    if endpoint:
        import urllib.error
        import urllib.request

        try:
            urllib.request.urlopen(urllib.request.Request(endpoint, data=b"", method="POST",
                                                          headers={"Content-Type": "application/x-protobuf"}), timeout=2)
            line("ok", f"OTLP endpoint reachable: {endpoint}")
        except urllib.error.HTTPError:
            line("ok", f"OTLP endpoint reachable: {endpoint}")
        except Exception:
            line("info", f"OTLP endpoint {endpoint} not reachable. Runs still write local traces. "
                         "Start Jaeger with `docker compose up -d jaeger` or unset the variable.")
    else:
        line("info", "OTLP export off (optional). Local spans.jsonl and HTML comparison cover the assignment.")
    for provider, st in provider_status().items():
        state = "configured" if st["key_configured"] else "not configured"
        line("info", f"live planner {provider}: key {state}, model {st['model']} (optional)")
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401

        line("info", "GUI extra installed (`uv run --extra gui workshop gui`)")
    except ImportError:
        line("info", "GUI extra not installed (optional): `uv sync --frozen --extra gui`")
    print("\nOffline path: prepare -> run (simulated) -> visualize/export. No Docker, network, or keys needed.")
    print("doctor: OK" if ok else "doctor: problems found")
    return 0 if ok else 1


def cmd_checkpoint_child(args: argparse.Namespace) -> int:
    from .service import checkpoint_child

    print(json.dumps(checkpoint_child(Path(args.db), args.scenario)))
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    r = _op("prepare", {"db": args.db, "scenario": args.scenario, "reset": args.reset}, args.op_id)
    print(f"prepared {r['db_path']}")
    print(f"  child process {r['checkpoint_pid']} wrote events 1-{r['paused_at_sequence']}, saved its memory, and exited")
    print(f"  process {r['this_pid']} appended {r['appended_while_paused']} events while the agent was stopped "
          f"(now through sequence {r['through_sequence']})")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    r = _op("reset", {"db": args.db, "scenario": args.scenario}, args.op_id)
    print(f"reset {r['db_path']}: re-created from the fixture (through sequence {r['through_sequence']}). "
          "Existing run directories are kept.")
    return 0


def cmd_append(args: argparse.Namespace) -> int:
    r = _op("append", {"db": args.db, "fixture": args.fixture}, args.op_id)
    if r["appended"]:
        print(f"appended {', '.join(r['appended'])} from {r['fixture']} (now through sequence {r['through_sequence']})")
    else:
        print(f"{r['fixture']} was already applied ({', '.join(r['already_present'])}); nothing changed")
    return 0


def _start_params(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "strategy": args.strategy, "planner": args.planner, "provider": args.provider, "model": args.model,
        "target_environment": args.target_environment, "out": args.out, "replace": args.replace,
    }


def cmd_run(args: argparse.Namespace) -> int:
    params = {"db": args.db, **_start_params(args), "until_done": True}
    return print_run(_op("run", params, args.op_id))


def cmd_step(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"db": args.db, "until_done": args.until_done}
    if args.strategy:
        params.update(_start_params(args))
    return print_run(_op("step", params, args.op_id))


def cmd_abandon(args: argparse.Namespace) -> int:
    r = _op("abandon", {"db": args.db}, args.op_id)
    print(f"abandoned {r['run_id']}" if r.get("run_id") else r.get("message", "nothing to abandon"))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from .runner import STAGE_DESCRIPTIONS, STAGES
    from .service import session_status

    st = session_status(Path(args.db))
    if args.json:
        _print_json(st)
        return 0
    print(f"session {st['meta'].get('session_id')} in {st['db_path']} (scenario {st['meta'].get('scenario')})")
    print(f"  events through sequence {st['through_sequence']} ({st['event_count']} events)")
    print(f"  pause checkpoint written by pid {st['meta'].get('checkpoint_pid')}; "
          f"resumed history appended by pid {st['meta'].get('resumer_pid')}")
    print(f"  follow-ups applied: {', '.join(st['followups_applied']) or 'none'}; "
          f"available: {', '.join(st['followups_available']) or 'none'}")
    for m in st["memories"]:
        print(f"  memory {m['kind']} through {m['through_sequence']} (pid {m['writer_pid']})")
    for s in st["snapshots"]:
        print(f"  snapshot {s['strategy']} v{s['projection_version']} through {s['through_sequence']} hash {s['state_hash'][:12]}")
    for r in st["receipts"]:
        print(f"  receipt {r['receipt_id']}: {r['commit']} -> {r['environment']} (run {r['run_id']})")
    print("  runs:")
    for r in st["runs"]:
        print(f"    {r['run_id']}  {r['status']:<9} {r['strategy']:<9} {r['planner_mode']:<9} {r['run_dir']}")
    if st["active_run"]:
        nxt = st["active_run"]["next_stage"]
        print(f"\nactive run {st['active_run']['run_id']}: next stage {nxt}: {STAGE_DESCRIPTIONS[nxt]}")
        print(f"  stages: {' -> '.join(STAGES)}")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    from .service import session_events

    for e in session_events(Path(args.db), args.since):
        payload = ", ".join(f"{k}={v}" for k, v in e["payload"].items())
        print(f"{e['sequence']:>3} {e['event_id']} {e['occurred_at']} {e['type']:<26} {e['actor']:<9} {payload}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from .evidence import load_run

    view = load_run(Path(args.run))
    if args.artifact == "list":
        names = ["manifest.json", "progress.json", *view.artifacts.keys(), "spans.jsonl"]
        for n in names:
            if (view.run_dir / n).exists():
                print(n)
        return 0
    name = args.artifact if args.artifact.endswith((".json", ".jsonl")) else f"{args.artifact}.json"
    if name == "manifest.json":
        _print_json(view.manifest)
    elif name == "progress.json":
        _print_json(view.progress)
    elif name == "spans.jsonl":
        _print_json(view.spans)
    elif name in view.artifacts:
        _print_json(view.artifacts[name])
    else:
        print(f"{name} was not written by this run (stage not reached, or not applicable)", file=sys.stderr)
        return 1
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    from .evidence import load_run
    from .traceview import render_tree_text

    view = load_run(Path(args.run))
    p = view.progress
    print(f"{view.run_dir}: strategy={p['strategy']} planner_mode={p['planner_mode']} status={p['status']}")
    print(render_tree_text(view.spans, show_attributes=args.attributes or bool(args.span), only=args.span))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    from .evidence import compare, diff_text

    result = compare(Path(args.before), Path(args.after))
    names = ["state.json", "context.json", "proposal.json", "gate.json"] if args.artifact == "all" else [f"{args.artifact}.json"]
    for n in names:
        print(f"== {n}")
        print(diff_text(result["diffs"][n]))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    from .evidence import compare, compare_text

    print(compare_text(compare(Path(args.before), Path(args.after))))
    return 0


def cmd_visualize(args: argparse.Namespace) -> int:
    r = _op("visualize", {"before": args.before, "after": args.after, "output": args.output}, args.op_id)
    print(f"wrote {r['output']} (self-contained; open it in any browser, offline)")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    r = _op("export", {"before": args.before, "after": args.after, "followups": args.followup or [],
                       "output": args.output}, args.op_id)
    for f in r["files"]:
        print(f"wrote {f}")
    for n in r["notices"]:
        print(f"  ! {n}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    r = _op("check", {"strategy": args.strategy}, args.op_id)
    current = None
    for res in r["results"]:
        if res["exercise"] != current:
            current = res["exercise"]
            print(f"\nExercise {current}")
        print(f"  {res['status'].upper():<5} {res['name']}")
        if res["status"] != "pass" or args.verbose:
            for line in res["detail"].splitlines():
                print(f"        {line}")
    s = r["summary"]
    print(f"\n{s['pass']} passed, {s['todo']} unfinished, {s['fail']} failed, {s['error']} errors "
          f"(strategy={r['strategy']})")
    if s["todo"] and not (s["fail"] or s["error"]):
        print("Unfinished work is expected until you complete exercises.py; the runtime is working.")
    return 0 if s["pass"] == len(r["results"]) else 1


def cmd_trace_url(args: argparse.Namespace) -> int:
    from .runner import read_json
    from .telemetry import jaeger_trace_url

    p = read_json(Path(args.run) / "progress.json")
    if not p:
        print(f"{args.run} is not a run directory", file=sys.stderr)
        return 1
    print(f"trace id : {p['trace_id']}")
    print(f"jaeger   : {jaeger_trace_url(p['trace_id'])}")
    if not p.get("otlp_endpoint"):
        print("note     : this run did not export over OTLP; re-run with OTEL_EXPORTER_OTLP_TRACES_ENDPOINT set")
    return 0


def cmd_ops(args: argparse.Namespace) -> int:
    for op in _ws().ops.recent(args.limit):
        print(f"{op['created_at']}  {op['op_id']}  {op['interface']:<4} {op['kind']:<9} {op['status']:<11} "
              f"{op['error'] or ''}")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .gui.server import serve
    except ImportError as exc:
        print(f"The GUI is optional and its dependencies are not installed ({exc.name}).\n"
              "Install and launch with: uv run --extra gui workshop gui", file=sys.stderr)
        return 1
    return serve(Path.cwd(), port=args.port, open_browser=not args.no_browser)


def cmd_examples(args: argparse.Namespace) -> int:
    """Regenerate the labeled example traces from the reference solution."""
    import shutil

    out = Path(args.output)
    if out.exists():
        shutil.rmtree(out)
    work = out / "_work"
    ws = _ws()
    base, fixed = str(work / "base.sqlite"), str(work / "fixed.sqlite")
    ws.execute("prepare", {"db": base, "reset": True})
    ws.execute("prepare", {"db": fixed, "reset": True})
    plan = [
        ("run", {"db": base, "strategy": "baseline", "out": str(out / "before")}),
        ("run", {"db": fixed, "strategy": "reference", "out": str(out / "after")}),
        ("append", {"db": fixed, "fixture": "tests-pass-b2"}),
        ("run", {"db": fixed, "strategy": "reference", "out": str(out / "tests-pass")}),
        ("append", {"db": fixed, "fixture": "approve-b2-staging"}),
        ("run", {"db": fixed, "strategy": "reference", "out": str(out / "approved")}),
    ]
    for kind, params in plan:
        rec = ws.execute(kind, params)
        if rec["status"] != "done":
            print(f"{kind} failed: {rec['error']}", file=sys.stderr)
            return 1
    ws.execute("export", {"before": str(out / "before"), "after": str(out / "after"),
                          "followups": [str(out / "tests-pass"), str(out / "approved")], "output": str(out / "evidence")})
    shutil.rmtree(work)
    print(f"regenerated example runs in {out}")
    return 0


# ------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    from .planners import PROVIDERS
    from .scenarios import FOLLOWUP_FILES, SCENARIO_FILES
    from .strategies import STRATEGIES

    parser = argparse.ArgumentParser(prog="workshop", description="Agent memory, state, and traces workshop.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True, metavar="{" + ",".join(PUBLIC_COMMANDS) + "}")

    def op_id(p: argparse.ArgumentParser) -> None:
        p.add_argument("--op-id", help="operation ID; re-sending the same ID never repeats the work")

    def run_opts(p: argparse.ArgumentParser, required: bool) -> None:
        p.add_argument("--strategy", choices=STRATEGIES, required=required)
        p.add_argument("--planner", choices=("simulated", "live"), default="simulated")
        p.add_argument("--provider", choices=PROVIDERS, help="live mode provider (default: LLM_PROVIDER or whichever key is set)")
        p.add_argument("--model", help="live mode model (default: <PROVIDER>_MODEL from .env)")
        p.add_argument("--target-environment", default="staging")
        p.add_argument("--out", help="run directory (default: runs/<session>-<strategy>-NN)")
        p.add_argument("--replace", action="store_true", help="discard an existing run directory at --out")

    p = sub.add_parser("doctor", help="check the environment")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("checkpoint-child")
    p.add_argument("--db", required=True)
    p.add_argument("--scenario", default="stale-resume")
    p.set_defaults(func=cmd_checkpoint_child)

    p = sub.add_parser("prepare", help="create a session database with a real process pause")
    p.add_argument("--scenario", choices=sorted(SCENARIO_FILES), default="stale-resume")
    p.add_argument("--db", required=True)
    p.add_argument("--reset", action="store_true", help="discard an existing database first")
    op_id(p)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("reset", help="explicitly reset a session database to the fixture")
    p.add_argument("--db", required=True)
    p.add_argument("--scenario", choices=sorted(SCENARIO_FILES), default="stale-resume")
    op_id(p)
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("append", help="append a follow-up fixture event")
    p.add_argument("--db", required=True)
    p.add_argument("--fixture", choices=sorted(FOLLOWUP_FILES), required=True)
    op_id(p)
    p.set_defaults(func=cmd_append)

    p = sub.add_parser("run", help="start a run and execute every stage")
    p.add_argument("--db", required=True)
    run_opts(p, required=True)
    op_id(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("step", help="execute the next stage (starting a run if none is active)")
    p.add_argument("--db", required=True)
    run_opts(p, required=False)
    p.add_argument("--until-done", action="store_true", help="execute all remaining stages")
    op_id(p)
    p.set_defaults(func=cmd_step)

    p = sub.add_parser("abandon", help="abandon the active run")
    p.add_argument("--db", required=True)
    op_id(p)
    p.set_defaults(func=cmd_abandon)

    p = sub.add_parser("status", help="show a session's events, runs, snapshots, receipts")
    p.add_argument("--db", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("events", help="print the event history")
    p.add_argument("--db", required=True)
    p.add_argument("--since", type=int, default=0)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("show", help="print a run artifact (state, context, planner-input, proposal, gate, tools, ...)")
    p.add_argument("--run", required=True)
    p.add_argument("--artifact", default="list")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("trace", help="print a run's span tree, durations, attributes, evidence IDs")
    p.add_argument("--run", required=True)
    p.add_argument("--attributes", action="store_true", help="print every span's attributes and events")
    p.add_argument("--span", help="print attributes for this span name only")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("diff", help="structural diff of two runs' artifacts")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.add_argument("--artifact", choices=("state", "context", "proposal", "gate", "all"), default="all")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("compare", help="terminal side-by-side comparison of two runs")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("visualize", help="write the offline HTML trace comparison")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.add_argument("--output", default="submissions/comparison.html")
    op_id(p)
    p.set_defaults(func=cmd_visualize)

    p = sub.add_parser("export", help="write the submission evidence bundle")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.add_argument("--followup", action="append", help="additional run directories (repeatable)")
    p.add_argument("--output", default="submissions")
    op_id(p)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("check", help="run the exercise acceptance checks")
    p.add_argument("--strategy", choices=("student", "reference"), default="student")
    p.add_argument("-v", "--verbose", action="store_true")
    op_id(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("trace-url", help="print a run's trace ID and Jaeger URL")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_trace_url)

    p = sub.add_parser("ops", help="list recent operations from both interfaces")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_ops)

    p = sub.add_parser("gui", help="launch the optional local GUI (needs the gui extra)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("examples", help="regenerate example traces with the reference solution")
    p.add_argument("--output", default="solutions/example-runs")
    p.set_defaults(func=cmd_examples)
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        from .runner import RunError
        from .service import ServiceError

        if isinstance(exc, (ServiceError, RunError)):
            print(f"error: {exc}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
