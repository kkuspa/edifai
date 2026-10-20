"""Optional local GUI: a thin FastAPI layer over ``WorkshopService``.

Install with ``uv sync --extra gui`` and launch with ``uv run workshop gui``. The server binds
to 127.0.0.1 only, refuses requests whose Host header is not local, and requires a custom
header on every mutating request so another website cannot drive it. Credentials stay in
the backend process; the browser only learns whether a provider is configured.

GET routes are read-only. Work happens only through POST routes, each carrying an operation
ID (repeats return the stored result) and, where it applies, the session version the page
last saw (stale requests are refused instead of executed twice).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..service import ServiceError, WorkshopService

STATIC = Path(__file__).resolve().parent / "static"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
CLIENT_HEADER = "x-workshop-client"


class CreateBody(BaseModel):
    session: str
    implementation: str
    scenario: str = "changing-trip"
    planner: str = "simulated"
    provider: str | None = None
    strategy: str = "student"
    restated_brief: bool = False
    overwrite: bool = False
    op_id: str


class OpBody(BaseModel):
    op_id: str
    expected_version: int | None = None


class CompareBody(BaseModel):
    sessions: list[str]
    output: str = "submissions/comparison.html"
    op_id: str


class CheckBody(BaseModel):
    strategy: str = "student"
    op_id: str


def _name(session: str) -> str:
    if not NAME.match(session):
        raise HTTPException(400, "Session names use letters, digits, '.', '_' and '-' only.")
    return session


def create_app(root: Path | str = "runs", port: int = 8765) -> FastAPI:
    service = WorkshopService(root=root)
    app = FastAPI(title="Context to State Workshop", docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost", "testserver"}

    @app.middleware("http")
    async def local_only(request: Request, call_next: Any) -> Any:
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"error": "This server only answers requests addressed to localhost."}, status_code=403)
        if request.method == "POST" and request.headers.get(CLIENT_HEADER) != "gui":
            return JSONResponse({"error": f"Missing {CLIENT_HEADER} header."}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "type": type(exc).__name__}, status_code=exc.status)

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "type": "ValueError"}, status_code=400)

    # -- read-only ----------------------------------------------------------------------

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/options")
    def options() -> dict[str, Any]:
        return {**service.options(), "root": str(service.root)}

    @app.get("/api/sessions")
    def sessions() -> list[dict[str, Any]]:
        return service.list_sessions()

    @app.get("/api/sessions/{session}")
    def status(session: str) -> dict[str, Any]:
        return service.status(_name(session))

    @app.get("/api/sessions/{session}/state")
    def state(session: str, snapshot: int | None = None) -> dict[str, Any]:
        return service.state(_name(session), snapshot)

    @app.get("/api/sessions/{session}/diff")
    def diff(session: str, snapshot: int | None = None) -> dict[str, Any]:
        return service.diff(_name(session), snapshot)

    @app.get("/api/sessions/{session}/records/{kind}")
    def records(session: str, kind: str) -> list[dict[str, Any]]:
        return service.records(_name(session), kind)

    @app.get("/api/sessions/{session}/transcript")
    def transcript(session: str) -> list[dict[str, Any]]:
        return service.transcript(_name(session))

    @app.get("/api/sessions/{session}/report")
    def report(session: str) -> FileResponse:
        path = service.path(_name(session)) / "report.html"
        if not path.exists():
            raise HTTPException(404, "No report yet.")
        return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/api/compare")
    def compare_data(sessions: list[str] = Query(...)) -> list[dict[str, Any]]:
        return service.compare_data([_name(s) for s in sessions])

    # -- mutating (explicit actions only) -----------------------------------------------

    @app.post("/api/sessions")
    def create(body: CreateBody) -> dict[str, Any]:
        return service.create(_name(body.session), implementation=body.implementation, scenario=body.scenario, planner=body.planner,
                              provider=body.provider or None, strategy=body.strategy, restated_brief=body.restated_brief,
                              op_id=body.op_id, overwrite=body.overwrite)

    @app.post("/api/sessions/{session}/apply-change")
    def apply_change(session: str, body: OpBody) -> dict[str, Any]:
        return service.apply_change(_name(session), op_id=body.op_id, expected_version=body.expected_version)

    @app.post("/api/sessions/{session}/step")
    def step(session: str, body: OpBody) -> dict[str, Any]:
        return service.step(_name(session), op_id=body.op_id, expected_version=body.expected_version)

    @app.post("/api/sessions/{session}/run-turn")
    def run_turn(session: str, body: OpBody) -> dict[str, Any]:
        return service.run_turn(_name(session), op_id=body.op_id, expected_version=body.expected_version)

    @app.post("/api/sessions/{session}/run")
    def run(session: str, body: OpBody) -> dict[str, Any]:
        return service.run_to_completion(_name(session), op_id=body.op_id, expected_version=body.expected_version)

    @app.post("/api/sessions/{session}/reset")
    def reset(session: str, body: OpBody) -> dict[str, Any]:
        return service.reset(_name(session), op_id=body.op_id, expected_version=body.expected_version)

    @app.post("/api/sessions/{session}/export")
    def export(session: str, body: OpBody) -> dict[str, Any]:
        return service.export(_name(session))

    @app.post("/api/compare")
    def compare(body: CompareBody) -> dict[str, Any]:
        return service.compare([_name(s) for s in body.sessions], body.output, op_id=body.op_id)

    @app.post("/api/check")
    def check(body: CheckBody) -> dict[str, Any]:
        return service.check(body.strategy, op_id=body.op_id)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def serve(root: Path = Path("runs"), port: int = 8765) -> None:
    import uvicorn

    print(f"Workshop GUI on http://127.0.0.1:{port}  (sessions in {root.resolve()}; Ctrl+C to stop)")
    uvicorn.run(create_app(root, port), host="127.0.0.1", port=port, log_level="warning")
