import os
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.auth import AdminAuthRedirect
from app.services.monitoring_config import load_config as load_monitoring_config
from app.services.monitoring_poller import (
    init_monitoring_poller,
    refresh_stale_monitoring,
    shutdown_monitoring_poller,
)
from app.services.poller import discovery_poll
from app.services.registry import load_from_env

_scheduler = AsyncIOScheduler()

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown events."""
    # Load workspace API keys from environment on startup
    load_from_env()

    # Load monitoring config (factory defaults + disk/env overrides).
    # Must happen before init_monitoring_poller() because the poller's
    # classifier reads thresholds via monitoring_config.get_config().
    load_monitoring_config()

    # Register background discovery poll job (per D-12, OPS-04)
    poll_interval = int(os.getenv("QA_POLL_INTERVAL_SECONDS", "300"))
    _scheduler.add_job(
        discovery_poll,
        "interval",
        seconds=poll_interval,
        id="discovery_poll",
        replace_existing=True,
    )

    # Register monitoring refresh job (phase 2, GOAL.md 4.4). Shares the
    # scheduler with QA discovery but uses its own cache + shared httpx client.
    init_monitoring_poller(_scheduler)

    _scheduler.start()

    # Run initial discovery on startup so cache is pre-populated (per OPS-04)
    await discovery_poll()

    # Kick off an immediate monitoring refresh so the UI has data on first load
    # instead of showing "loading" for up to 60s. Errors are swallowed inside
    # refresh_stale_monitoring().
    await refresh_stale_monitoring()

    yield

    # Shutdown — stop scheduler first, then close the monitoring poller's
    # shared httpx client and await any in-flight Phase 2 backfill tasks.
    _scheduler.shutdown(wait=False)
    await shutdown_monitoring_poller()


def create_app() -> FastAPI:
    """App factory — creates and configures the FastAPI application."""
    application = FastAPI(
        title="Prospeqt Email QA",
        description="QA dashboard for Instantly email campaigns across workspaces",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Mount static files if the directory exists
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        application.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Register routers
    from app.routes import admin, dashboard, monitoring
    application.include_router(dashboard.router, prefix="/qa")
    application.include_router(monitoring.router)
    application.include_router(admin.router)

    # Root redirect -> monitoring (per phase-1 spec)
    @application.get("/")
    async def root_redirect():
        return RedirectResponse(url="/monitoring", status_code=307)

    # Health check at root level (NOT under /qa) — Render deployment
    @application.get("/health")
    async def health():
        return {"status": "ok"}

    @application.exception_handler(AdminAuthRedirect)
    async def admin_auth_redirect_handler(request: Request, exc: AdminAuthRedirect):
        return RedirectResponse(url="/admin/login", status_code=303)

    return application


app = create_app()
