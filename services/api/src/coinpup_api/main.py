"""Authenticated application with independent ledgers and atomic financial posting."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api import __version__
from coinpup_api.auth import create_auth_router
from coinpup_api.config import Settings
from coinpup_api.database import Database, DatabaseProbe
from coinpup_api.documents.router import create_document_router
from coinpup_api.ledger.posting_router import create_posting_router
from coinpup_api.ledger.router import create_ledger_router

logger = logging.getLogger("coinpup")


class Health(BaseModel):
    service: Literal["coinpup-api"] = "coinpup-api"
    status: Literal["ok", "unavailable"] = "ok"


def create_app(
    settings: Settings | None = None,
    database: DatabaseProbe | None = None,
    web_dist: Path | None = None,
) -> FastAPI:
    settings = settings or Settings()
    probe = database if database is not None else Database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Coinpup API starting")
        try:
            yield
        finally:
            probe.close()
            logger.info("Coinpup API stopped")

    production = settings.environment == "production"
    app = FastAPI(
        title="Coinpup API",
        version=__version__,
        description="Coinpup session, ledger structure and exact financial operations API.",
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path != "/docs":  # Development-only Swagger uses its own scripts.
            response.headers.setdefault(
                "Content-Security-Policy",
                (
                    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
                ),
            )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # FastAPI normally returns rejected input, which may contain credentials.
        details = [
            {"loc": item["loc"], "type": item["type"], "msg": "Invalid value"}
            for item in error.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": details})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError):
        logger.warning("Database operation unavailable")
        return JSONResponse(status_code=503, content={"detail": "Service unavailable"})

    app.include_router(create_auth_router(settings, getattr(probe, "engine", None)))
    app.include_router(create_ledger_router(settings, getattr(probe, "engine", None)))
    app.include_router(create_posting_router(settings, getattr(probe, "engine", None)))
    app.include_router(create_document_router(settings, getattr(probe, "engine", None)))

    @app.get("/api/v1/health/live", response_model=Health, tags=["health"])
    def liveness() -> Health:
        """Process health. Does not require a database connection."""
        return Health()

    @app.get(
        "/api/v1/health/ready",
        response_model=Health,
        responses={503: {"model": Health, "description": "Database unavailable"}},
        tags=["health"],
    )
    def readiness() -> Health | JSONResponse:
        """Check connectivity, not migration state or completed product capabilities."""
        try:
            probe.check()
        except SQLAlchemyError:
            # Never include exception text: drivers can expose connection information.
            logger.warning("Database readiness check failed")
            return JSONResponse(status_code=503, content=Health(status="unavailable").model_dump())
        return Health()

    distribution = web_dist or Path(__file__).resolve().parents[4] / "apps/web/dist"
    if (distribution / "index.html").is_file():
        if (distribution / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=distribution / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        @app.get("/login", include_in_schema=False)
        def web_index():
            return FileResponse(distribution / "index.html", headers={"Cache-Control": "no-cache"})

        if (distribution / "coinpup.svg").is_file():

            @app.get("/coinpup.svg", include_in_schema=False)
            def web_brand():
                return FileResponse(distribution / "coinpup.svg", media_type="image/svg+xml")

    return app
