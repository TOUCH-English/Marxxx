"""FastAPI 应用：只监听本机；前端静态文件与 API 同源，因此不开放 CORS。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import PROJECT_ROOT
from ..services.runs import ensure_seed
from . import routes_config, routes_ns, routes_sys, security

DIST = PROJECT_ROOT / "frontend" / "dist"


def create_app() -> FastAPI:
    app = FastAPI(title="Moomoo AI 自动交易工作台", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")

    @app.on_event("startup")
    def _startup():
        for ns in ("sim", "live"):
            ensure_seed(ns)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        try:
            security.check_host_origin(request)
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'")
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    app.include_router(routes_sys.router)
    app.include_router(routes_config.router)
    app.include_router(routes_ns.router)

    if (DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        f = DIST / path
        if path and f.is_file() and DIST in f.resolve().parents:
            return FileResponse(f)
        idx = DIST / "index.html"
        if idx.exists():
            return FileResponse(idx)
        return JSONResponse({"detail": "前端尚未构建：请运行 scripts/start 脚本（会自动构建），或在 frontend/ 运行 npm run build"},
                            status_code=503)

    return app


app = create_app()
