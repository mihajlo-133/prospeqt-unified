import os
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.auth import AdminAuthRedirect
from app.services.poller import discovery_poll
from app.services.workspace import load_from_env

_scheduler = AsyncIOScheduler()

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown events."""
    # Load workspace API keys from environment on startup
    load_from_env()

    # Register background discovery poll job (per D-12, OPS-04)
    poll_interval = int(os.getenv("QA_POLL_INTERVAL_SECONDS", "300"))
    _scheduler.add_job(
        discovery_poll,
        "interval",
        seconds=poll_interval,
        id="discovery_poll",
        replace_existing=True,
    )
    _scheduler.start()

    # Run initial discovery on startup so cache is pre-populated (per OPS-04)
    await discovery_poll()

    yield

    # Shutdown
    _scheduler.shutdown(wait=False)


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
    from app.routes import admin, dashboard
    application.include_router(dashboard.router)
    application.include_router(admin.router)

    @application.exception_handler(AdminAuthRedirect)
    async def admin_auth_redirect_handler(request: Request, exc: AdminAuthRedirect):
        return RedirectResponse(url="/admin/login", status_code=303)

    return application


app = create_app()
