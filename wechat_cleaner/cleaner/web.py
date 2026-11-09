from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import secrets
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .files import CleanupError, process_status
from .jobs import Jobs
from .reader import accounts
from .service import CleanerService
from . import __version__


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Connection(Input):
    account: str = Field(min_length=1, max_length=100)
    directory: str | None = Field(default=None, max_length=1000)


class Search(Input):
    conversation_ids: list[str] = Field(min_length=1, max_length=10000, validation_alias=AliasChoices("conversation_ids", "group_ids"))
    keyword: str = Field(min_length=1, max_length=200)
    match: Literal["contains", "exact"] = "contains"
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)


class Prepare(Input):
    scan_id: str = Field(min_length=1, max_length=64)
    ids: list[str] = Field(min_length=1, max_length=1000)


class Plan(Input):
    id: str = Field(min_length=1, max_length=64)


def create_app(data_dir, demo=False, service=None, on_quit=None):
    if service is None:
        if demo:
            from .demo import DemoService
            service = DemoService()
        else:
            service = CleanerService(data_dir)
    jobs = Jobs()
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        yield
        jobs.close()
        if hasattr(service, "close"):
            service.close()

    app = FastAPI(title="清简 · 微信记录清理", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.service, app.state.jobs = service, jobs
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_only(request, call_next):
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            expected = str(request.base_url).rstrip("/")
            if origin and origin != expected:
                return JSONResponse({"detail": "请从本机清理面板操作。"}, status_code=403)
            if request.headers.get("x-cleaner-client") != "1":
                return JSONResponse({"detail": "缺少本机客户端标识。"}, status_code=403)
            if request.method not in ("GET", "HEAD") and request.headers.get("x-cleaner-token") != token:
                return JSONResponse({"detail": "页面会话已失效，请刷新后重试。"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(CleanupError)
    async def cleanup_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        return JSONResponse({"detail": "输入格式无效，请检查会话、删除词和时间范围。"}, status_code=422)

    @app.get("/api/session")
    def session():
        return {"token": token, "demo": demo, "version": __version__}

    @app.get("/api/status")
    def status():
        return {"wechat": {"running": False, "version": "4.2.0.13"} if demo else process_status(),
                "connection": service.reader.info() if service.reader else None,
                "active_job": jobs.active, "pending_recovery": service.journal.pending()}

    @app.get("/api/accounts")
    def find_accounts(directory: str | None = None):
        if demo:
            return {"directory": "", "accounts": [{"id": "demo_account", "directory": ""}]}
        return accounts(directory)

    @app.post("/api/connect")
    def connect(body: Connection):
        return jobs.submit("connect", lambda p: service.connect(body.account, body.directory, p))

    @app.get("/api/groups")
    def groups():
        return service.groups()

    @app.get("/api/contacts")
    def contacts():
        return service.contacts()

    @app.get("/api/conversations")
    def conversations():
        return service.conversations()

    @app.post("/api/search")
    def find(body: Search):
        return jobs.submit("search", lambda p: service.search(body.model_dump(), p))

    @app.post("/api/prepare")
    def prepare(body: Prepare):
        return jobs.submit("prepare", lambda p: service.prepare(body.scan_id, body.ids, p))

    @app.post("/api/execute")
    def execute(body: Plan):
        return jobs.submit("execute", lambda p: service.execute(body.id, p))

    @app.get("/api/runs")
    def runs():
        return service.journal.list()

    @app.get("/api/runs/{identifier}")
    def run_detail(identifier: str):
        if demo:
            return service.plan_info()
        run, plan, source = service.journal.load(identifier)
        return {"id": identifier, "count": plan["count"], "group_names": plan["group_names"],
                "conversation_names": plan.get("conversation_names", plan["group_names"]),
                "conversations": plan.get("conversations"), "conversation_count": plan.get("conversation_count", len(plan["group_names"])),
                "files": len(plan["changes"]), "backup": str(run / "backup"), "status": plan["status"]}

    @app.post("/api/restore")
    def restore(body: Plan):
        return jobs.submit("restore", lambda p: service.restore(body.id, False, p))

    @app.post("/api/recover")
    def recover(body: Plan):
        return jobs.submit("recover", lambda p: service.restore(body.id, True, p))

    @app.get("/api/jobs/{identifier}")
    def job(identifier: str):
        return jobs.get(identifier)

    @app.post("/api/quit")
    def quit_app():
        if jobs.active:
            raise CleanupError("任务正在执行，完成后才能退出程序。")
        if on_quit:
            on_quit()
        return {"status": "quitting"}

    static = Path(__file__).parent / "static"
    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/")
    def index():
        entry = static / "cleaner.html"
        if not entry.exists():
            raise HTTPException(503, "界面资源尚未构建，请先运行 frontend 的 build:cleaner。")
        return FileResponse(entry)

    return app
