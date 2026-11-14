"""FastAPI application entrypoint for KubeAgent.

Run locally with::

    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

try:  # normal path: `uvicorn app.main:app`, the Docker image, tests
    from app import __version__
    from app.api import incidents, stream
    from app.config import Settings, get_settings
except ModuleNotFoundError:  # `python -m backend.app.main` from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import __version__
    from app.api import incidents, stream
    from app.config import Settings, get_settings

logger = logging.getLogger("kubeagent")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        """Inline icon so browsers never log a 404 for /favicon.ico."""
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
            "<rect width='32' height='32' rx='8' fill='#020617'/>"
            "<circle cx='16' cy='16' r='9' fill='none' stroke='#34d399' stroke-width='2'/>"
            "<circle cx='16' cy='16' r='2.5' fill='#34d399'/></svg>"
        )
        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

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
            "reasoning_engine": (
                f"llm:{current.llm_model}" if current.llm_enabled else "heuristic"
            ),
            "uptime_seconds": round(time.time() - started_at, 3),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get(
        f"{settings.api_prefix}/cluster/health",
        tags=["system"],
        summary="Cluster health summary for the Mission Control dashboard",
    )
    async def cluster_health() -> dict[str, Any]:
        """Pod counts across the watched namespaces plus the active incident total.

        A namespace whose collector fails is reported in ``errors`` rather than
        failing the whole request, so the dashboard still renders.
        """
        orchestrator = incidents.get_orchestrator()
        total_pods = 0
        unhealthy_pods = 0
        errors: list[str] = []

        for namespace in settings.namespace_list:
            try:
                report = await asyncio.to_thread(
                    orchestrator.k8s.get_unhealthy_pods, namespace
                )
            except Exception as exc:  # live clusters can deny or time out
                errors.append(f"{namespace}: {exc}")
                continue
            total_pods += report.total_pods
            unhealthy_pods += report.unhealthy_count

        active_incidents = sum(
            1
            for incident in incidents.store.list(limit=200)
            if incident.status is not incidents.IncidentStatus.RESOLVED
        )

        return {
            "total_pods": total_pods,
            "unhealthy_pods": unhealthy_pods,
            "active_incidents": active_incidents,
            "simulation_mode": settings.simulation_mode,
            "llm_configured": settings.llm_enabled,
            "llm_model": settings.llm_model if settings.llm_enabled else None,
            "reasoning_engine": (
                f"llm:{settings.llm_model}" if settings.llm_enabled else "heuristic"
            ),
            "namespaces": settings.namespace_list,
            "errors": errors,
        }

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard(request: Request) -> HTMLResponse:
        """Serve the single-page Mission Control dashboard."""
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"app_name": settings.app_name, "app_version": settings.app_version},
        )

    @app.get(f"{settings.api_prefix}", tags=["system"], summary="Service banner")
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "health": "/healthz",
            "dashboard": "/",
        }

    return app


app = create_app()


if __name__ == "__main__":
    # `python -m backend.app.main` (repo root) and `python -m app.main`
    # (inside backend/) both start a real server on the configured port.
    import uvicorn

    _settings = get_settings()
    uvicorn.run(
        app,
        host=_settings.host,
        port=_settings.port,
        log_level=_settings.uvicorn_log_level,
    )
