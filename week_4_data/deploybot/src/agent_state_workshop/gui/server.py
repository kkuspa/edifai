"""Optional local GUI: a small FastAPI app over the same services the CLI uses.

* GET endpoints are read-only. Opening panels, refreshing, and polling never
  execute work.
* POST /api/ops is the only way to change state. The browser supplies an
  operation ID per user action and reuses it on retry; the service records
  each ID once, so a retry or double-click returns the original operation.
* The server binds to 127.0.0.1, accepts only localhost Host headers, and
  requires a JSON body on POST (cross-site form posts cannot send one without
  a CORS preflight, which this server never grants). API keys stay in the
  server's environment and are never returned.
"""

from __future__ import annotations

import re
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import __version__
from ..planners import PROVIDERS, provider_status
from ..runner import STAGE_DESCRIPTIONS, STAGES
from ..scenarios import FOLLOWUP_FILES, SCENARIO_FILES
from ..service import ServiceError, Workshop, session_events, session_status
from ..strategies import STRATEGIES
from ..visualize import comparison_payload, run_payload

GUI_STATIC = Path(__file__).resolve().parent / "static"
SHARED_STATIC = Path(__file__).resolve().parents[1] / "static"
OP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def create_app(root: Path) -> FastAPI:
    ws = Workshop(root, interface="gui")
    ws.ops.mark_interrupted()
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="workshop-op")
    app = FastAPI(title="Agent State Workshop", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/static/gui", StaticFiles(directory=GUI_STATIC), name="gui")
    app.mount("/static/shared", StaticFiles(directory=SHARED_STATIC), name="shared")

    def session_db(name: str) -> Path:
        try:
            return ws.db_for(name)
        except ServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    def run_dir(rel: str) -> Path:
        path = (ws.root / rel).resolve()
        allowed = [(ws.root / "runs").resolve(), (ws.root / "solutions" / "example-runs").resolve()]
        if not any(path.is_relative_to(a) for a in allowed) or not (path / "progress.json").exists():
            raise HTTPException(404, f"no run at {rel}")
        return path

    def rel(p: Path) -> str:
        try:
            return str(Path(p).resolve().relative_to(ws.root))
        except ValueError:
            return str(p)

    # ------------------------------------------------------------ read-only

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(GUI_STATIC / "index.html")

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {
            "version": __version__,
            "root": str(ws.root),
            "strategies": list(STRATEGIES),
            "planner_modes": ["simulated", "live"],
            "providers": {p: provider_status()[p] for p in PROVIDERS},
            "scenarios": list(SCENARIO_FILES),
            "fixtures": list(FOLLOWUP_FILES),
            "stages": [{"name": s, "description": STAGE_DESCRIPTIONS[s]} for s in STAGES],
        }

    @app.get("/api/sessions")
    def sessions() -> list[dict[str, Any]]:
        return ws.list_sessions()

    @app.get("/api/sessions/{name}")
    def session(name: str) -> dict[str, Any]:
        db = session_db(name)
        if not db.exists():
            raise HTTPException(404, f"session {name} does not exist")
        st = session_status(db)
        for r in st["runs"]:
            r["run_dir"] = rel(Path(r["run_dir"]))
        if st["active_run"]:
            st["active_run"]["run_dir"] = rel(Path(st["active_run"]["run_dir"]))
        return st

    @app.get("/api/sessions/{name}/events")
    def events(name: str, since: int = 0) -> list[dict[str, Any]]:
        db = session_db(name)
        if not db.exists():
            raise HTTPException(404, f"session {name} does not exist")
        return session_events(db, since)

    @app.get("/api/runs")
    def runs() -> list[dict[str, Any]]:
        return ws.list_runs()

    @app.get("/api/run")
    def run(path: str) -> dict[str, Any]:
        payload = run_payload(run_dir(path))
        payload["run_dir"] = path
        return payload

    @app.get("/api/compare")
    def compare(before: str, after: str) -> dict[str, Any]:
        return comparison_payload(run_dir(before), run_dir(after))

    @app.get("/api/ops")
    def ops(limit: int = 30) -> list[dict[str, Any]]:
        return ws.ops.recent(limit)

    @app.get("/api/ops/{op_id}")
    def op(op_id: str) -> dict[str, Any]:
        record = ws.ops.get(op_id)
        if record is None:
            raise HTTPException(404, "unknown operation")
        return record

    @app.get("/api/files/submissions/{name}")
    def submission_file(name: str) -> FileResponse:
        path = (ws.root / "submissions" / name).resolve()
        if path.parent != (ws.root / "submissions").resolve() or not path.is_file():
            raise HTTPException(404, "no such submission file")
        return FileResponse(path)

    # ------------------------------------------------------------ actions

    def translate(kind: str, p: dict[str, Any]) -> dict[str, Any]:
        """Map GUI parameters (session names, run paths) onto service parameters."""
        out: dict[str, Any] = {}
        if kind in ("prepare", "reset", "append", "run", "step", "abandon"):
            out["db"] = rel(session_db(str(p.get("session", ""))))
        if kind == "prepare":
            out.update(scenario=p.get("scenario", "stale-resume"), reset=False)
        elif kind == "reset":
            out.update(scenario=p.get("scenario", "stale-resume"))
        elif kind == "append":
            out["fixture"] = str(p.get("fixture"))
        elif kind in ("run", "step"):
            out["until_done"] = bool(p.get("until_done", kind == "run"))
            if kind == "run" or p.get("strategy"):
                out.update(
                    strategy=str(p.get("strategy", "student")),
                    planner=str(p.get("planner", "simulated")),
                    provider=p.get("provider") or None,
                    model=p.get("model") or None,
                    target_environment=str(p.get("target_environment") or "staging"),
                )
        elif kind == "check":
            out["strategy"] = str(p.get("strategy", "student"))
        elif kind in ("export", "visualize"):
            out["before"] = rel(run_dir(str(p.get("before", ""))))
            out["after"] = rel(run_dir(str(p.get("after", ""))))
            if kind == "export":
                out["followups"] = [rel(run_dir(str(f))) for f in p.get("followups") or []]
                out["output"] = "submissions"
            else:
                out["output"] = "submissions/comparison.html"
        elif kind != "abandon" and kind not in ("prepare", "reset"):
            raise HTTPException(400, f"unsupported operation {kind}")
        return out

    @app.post("/api/ops")
    async def submit(request: Request) -> JSONResponse:
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "send application/json")
        body = await request.json()
        op_id, kind = str(body.get("op_id", "")), str(body.get("kind", ""))
        if not OP_ID_RE.match(op_id):
            raise HTTPException(400, "op_id must be 8-80 characters of [A-Za-z0-9_-]")
        existing = ws.ops.get(op_id)
        if existing is not None:
            return JSONResponse(existing)  # retry or double submit: never run twice
        params = translate(kind, body.get("params") or {})
        try:
            record = ws.submit(kind, params, op_id)
        except ServiceError as exc:
            raise HTTPException(409, str(exc)) from exc
        if record["status"] == "queued":
            pool.submit(ws.run_operation, op_id)
        return JSONResponse(record, status_code=202)

    @app.exception_handler(ServiceError)
    def service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    return app


def serve(root: Path, port: int = 8765, open_browser: bool = True) -> int:
    app = create_app(root)
    url = f"http://127.0.0.1:{port}/"
    print(f"Workshop GUI on {url} (localhost only). Press Ctrl+C to stop.")
    print("Everything here is also available from the CLI; see README.md.")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
