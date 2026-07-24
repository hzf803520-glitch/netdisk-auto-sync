from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.executor_v2.router import router
from app.executor_v2.runtime import get_runtime


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    runtime = get_runtime()
    runtime.start()
    logger.info("netdisk sync executor v2 started")
    try:
        yield
    finally:
        runtime.stop()
        logger.info("netdisk sync executor v2 stopped")


app = FastAPI(
    title="Netdisk Sync Executor",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=()"
    )
    response.headers["Cache-Control"] = response.headers.get(
        "Cache-Control", "no-store"
    )
    if request.headers.get("x-forwarded-proto") == "https":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    return JSONResponse(
        {"message": str(exc.detail)},
        status_code=exc.status_code,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _: Request,
    exc: RequestValidationError,
):
    return JSONResponse(
        {"message": "请求参数格式无效", "errors": exc.errors()},
        status_code=422,
    )


@app.exception_handler(Exception)
async def unexpected_exception_handler(_: Request, exc: Exception):
    logger.exception("unhandled executor request error")
    return JSONResponse(
        {"message": "执行器内部错误，请查看服务日志"},
        status_code=500,
    )


@app.get("/")
def index():
    return {
        "service": "netdisk-sync-executor",
        "protocolVersion": "2.0",
        "message": "全新执行器在线；请从网盘同步管理站连接。",
    }


@app.get("/livez")
def livez():
    return {"ok": True}


app.include_router(router)
