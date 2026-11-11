"""FastAPI application factory.

Run:  uvicorn app.api.main:create_app --factory --reload
Docs: http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import admin_router, router
from app.container import Container, Settings, build_container
from app.domain.errors import DomainError


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    container = container or build_container(settings)
    app = FastAPI(
        title="ScoreMe Workflow Decision Platform",
        version="1.0.0",
        description="Configurable, auditable, idempotent workflow decision engine.",
    )
    app.state.container = container

    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status,
                            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})

    app.include_router(router)
    if container.settings.enable_admin:
        app.include_router(admin_router)
    return app
