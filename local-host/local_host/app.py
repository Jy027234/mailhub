"""Local MailHub host application factory.

Runtime entrypoint lives in ``local_host.serve``; this module stays a pure
factory so contract tests can assemble the application with fixtures.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from local_host.config import HostSettings
from local_host.routes import build_routes
from local_host.stores import LocalStores


def create_host_app(
    settings: HostSettings, stores: LocalStores, broker: Any
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stores.initialize()
        await broker.initialize()
        yield

    host_app = FastAPI(
        title="MailHub Local Host (B4)", version="0.1.0", lifespan=lifespan
    )
    host_app.include_router(build_routes(settings, stores, broker))

    @host_app.exception_handler(HTTPException)
    async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        code = detail.get("code") if isinstance(detail, dict) else "host_error"
        return JSONResponse(status_code=exc.status_code, content={"code": code})

    @host_app.middleware("http")
    async def parse_json_body(request: Request, call_next: Any) -> Any:
        body = await request.body()
        if body:
            try:
                request.state.body = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                request.state.body = None
        else:
            request.state.body = None
        return await call_next(request)

    return host_app
