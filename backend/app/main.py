"""FastAPI application entrypoint for KubeAgent.

Run locally with::

    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import incidents, stream
from app.config import Settings, get_settings

logger = logging.getLogger("kubeagent")


def configure_logging(settings: Settings) -> None:
    """Configure root logging once, honouring the configured log level."""
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        force=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan hook.

    Startup wires process-wide state onto ``app.state``; shutdown releases it.
    Long-lived background watchers (Kubernetes event streams, chaos runners)
    will be started and stopped from here.
    """
    settings: Settings = app.state.settings
    app.state.started_at = time.time()

    if settings.is_production and settings.simulation_mode:
        logger.warning(
            "SIMULATION_MODE is enabled in a production environment; "
            "the agent will not touch the live cluster"
        )

    logger.info(
        "Starting %s v%s (env=%s, simulation_mode=%s, llm=%s)",
        settings.app_name,
        settings.app_version,
        settings.environment,
        settings.simulation_mode,
        settings.llm_model if settings.llm_enabled else "rule-based",
    )

    try:
        yield
    finally:
        uptime = time.time() - app.state.started_at
        logger.info("Shutting down %s after %.1fs", settings.app_name, uptime)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.app_name,
        description=(
            "Autonomous Linux ops and incident remediation agent: collects "
            "Kubernetes and host evidence, reasons over it and proposes "
            "reversible remediation steps."
        ),
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url=f"{settings.api_prefix}/openapi.json",
    )
    app.state.settings = settings

    origins = settings.cors_origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        # Browsers reject credentialed CORS responses for a wildcard origin.
        allow_credentials="*" not in origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(incidents.router, prefix=settings.api_prefix)
    app.include_router(stream.router, prefix=settings.api_prefix)

    @app.get("/healthz", tags=["system"], summary="Liveness and readiness probe")
    async def healthz(request: Request) -> dict[str, Any]:
        """Lightweight probe used by Docker, Kubernetes and uptime checks."""
        current: Settings = request.app.state.settings
        started_at = getattr(request.app.state, "started_at", time.time())
        return {
            "status": "ok",
            "service": current.app_name,
            "version": current.app_version or __version__,
            "environment": current.environment,
            "simulation_mode": current.simulation_mode,
            "llm_configured": current.llm_enabled,
            "uptime_seconds": round(time.time() - started_at, 3),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/", tags=["system"], summary="Service banner")
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "health": "/healthz",
            "api": settings.api_prefix,
        }

    return app


app = create_app()
