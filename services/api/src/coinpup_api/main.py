"""Health-only application foundation. No bookkeeping endpoints exist yet."""

import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api import __version__
from coinpup_api.config import Settings
from coinpup_api.database import Database, DatabaseProbe

logger = logging.getLogger("coinpup")


class Health(BaseModel):
    service: Literal["coinpup-api"] = "coinpup-api"
    status: Literal["ok", "unavailable"] = "ok"


def create_app(settings: Settings | None = None, database: DatabaseProbe | None = None) -> FastAPI:
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
        description="Engineering foundation: liveness and database connectivity only.",
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )

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

    return app
