import copy
import json
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.auth import check_password, create_session_token, require_admin, verify_session_token
from app.services import monitoring_config
from app.services.monitoring_config import get_config, save_config, validate_config
from app.services.registry import (
    add_workspace,
    add_workspace_full,
    list_all_workspaces_detailed,
    list_monitoring_workspaces,
    remove_workspace,
)

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_THRESHOLD_FIELDS = (
    "reply_rate_warn", "reply_rate_red",
    "sent_pct_warn", "sent_pct_red",
    "bounce_rate_warn", "bounce_rate_red",
    "opps_pct_warn",
    "pool_days_warn", "pool_days_red",
)


def _admin_context(request: Request, **extra) -> dict:
    """Build the standard template context for admin.html."""
    cfg = get_config()
    return {
        "request": request,
        "active_tab": "admin",
        "workspaces": list_all_workspaces_detailed(),
        "config": cfg,
        "monitoring_clients": [e.name for e in list_monitoring_workspaces()],
        **extra,
    }


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(
    request: Request,
    admin_session: str | None = Cookie(default=None),
):
    # Redirect already-authenticated users straight to the admin panel
    if admin_session and verify_session_token(admin_session):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"active_tab": "admin"}
    )


@router.post("/login")
async def admin_login(request: Request, password: str = Form(...)):
    if check_password(password):
        token = create_session_token()
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie(
            key="admin_session",
            value=token,
            httponly=True,
            path="/admin",
            samesite="lax",
        )
        return response
    return templates.TemplateResponse(
        request,
        "login.html",
        context={"active_tab": "admin", "error": "Incorrect password. Try again."},
        status_code=200,
    )


@router.get("", response_class=HTMLResponse)
async def admin_panel(request: Request, _: None = Depends(require_admin)):
    return templates.TemplateResponse(
        request, "admin.html", context=_admin_context(request)
    )


# ---------------------------------------------------------------------------
# T1: Registry-aware workspace add/remove
# ---------------------------------------------------------------------------

@router.post("/workspaces")
async def add_workspace_route(
    request: Request,
    workspace_name: str = Form(...),
    api_key: str = Form(...),
    platform: str = Form(default="instantly"),
    monitoring_enabled: bool = Form(default=False),
    qa_enabled: bool = Form(default=False),
    _: None = Depends(require_admin),
):
    name = workspace_name.strip()
    key = api_key.strip()
    if not name or not key:
        return templates.TemplateResponse(
            request,
            "admin.html",
            context=_admin_context(
                request,
                error="Both Workspace Name and API Key are required.",
            ),
            status_code=400,
        )
    clean_name = name.lower().replace(" ", "-")
    add_workspace_full(
        name=clean_name,
        api_key=key,
        platform=platform,
        monitoring_enabled=monitoring_enabled,
        qa_enabled=qa_enabled,
    )
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/workspaces/{name}/delete")
async def remove_workspace_route(
    name: str,
    request: Request,
    _: None = Depends(require_admin),
):
    remove_workspace(name)
    return RedirectResponse(url="/admin", status_code=303)


# ---------------------------------------------------------------------------
# T2: Global thresholds save
# ---------------------------------------------------------------------------

@router.post("/thresholds")
async def save_thresholds(
    request: Request,
    reply_rate_warn: str = Form(default=""),
    reply_rate_red: str = Form(default=""),
    sent_pct_warn: str = Form(default=""),
    sent_pct_red: str = Form(default=""),
    bounce_rate_warn: str = Form(default=""),
    bounce_rate_red: str = Form(default=""),
    opps_pct_warn: str = Form(default=""),
    pool_days_warn: str = Form(default=""),
    pool_days_red: str = Form(default=""),
    _: None = Depends(require_admin),
):
    raw = {
        "reply_rate_warn": reply_rate_warn,
        "reply_rate_red": reply_rate_red,
        "sent_pct_warn": sent_pct_warn,
        "sent_pct_red": sent_pct_red,
        "bounce_rate_warn": bounce_rate_warn,
        "bounce_rate_red": bounce_rate_red,
        "opps_pct_warn": opps_pct_warn,
        "pool_days_warn": pool_days_warn,
        "pool_days_red": pool_days_red,
    }

    # Parse strings to float; collect parse errors
    parsed: dict[str, float] = {}
    parse_errors: list[str] = []
    for field, val in raw.items():
        val = val.strip()
        if val == "":
            continue
        try:
            parsed[field] = float(val)
        except ValueError:
            parse_errors.append(f"{field} must be a number (got {val!r})")

    if parse_errors:
        return templates.TemplateResponse(
            request,
            "admin.html",
            context=_admin_context(request, errors=parse_errors),
            status_code=400,
        )

    cfg = copy.deepcopy(get_config())
    cfg.setdefault("global_thresholds", {})
    cfg["global_thresholds"].update(parsed)

    is_valid, errors = validate_config(cfg)
    if not is_valid:
        return templates.TemplateResponse(
            request,
            "admin.html",
            context=_admin_context(request, errors=errors),
            status_code=400,
        )

    save_config(cfg)  # cache invalidation fires automatically via registered hook
    return RedirectResponse(url="/admin", status_code=303)


# ---------------------------------------------------------------------------
# T3: Per-client KPI targets save
# ---------------------------------------------------------------------------

@router.post("/clients/{name}/kpi")
async def save_client_kpi(
    name: str,
    request: Request,
    sent: str = Form(default=""),
    not_contacted: str = Form(default=""),
    opps_per_day: str = Form(default=""),
    reply_rate: str = Form(default=""),
    _: None = Depends(require_admin),
):
    raw = {
        "sent": sent,
        "not_contacted": not_contacted,
        "opps_per_day": opps_per_day,
        "reply_rate": reply_rate,
    }

    parsed: dict[str, float] = {}
    parse_errors: list[str] = []
    for field, val in raw.items():
        val = val.strip()
        if val == "":
            continue
        try:
            parsed[field] = float(val)
        except ValueError:
            parse_errors.append(f"{field} must be a number (got {val!r})")

    if parse_errors:
        return templates.TemplateResponse(
            request,
            "admin.html",
            context=_admin_context(request, errors=parse_errors),
            status_code=400,
        )

    cfg = copy.deepcopy(get_config())
    cfg.setdefault("clients", {})
    client_cfg = cfg["clients"].setdefault(name, {})
    # Preserve existing "thresholds" sub-key; only update KPI fields
    client_cfg.update(parsed)

    is_valid, errors = validate_config(cfg)
    if not is_valid:
        return templates.TemplateResponse(
            request,
            "admin.html",
            context=_admin_context(request, errors=errors),
            status_code=400,
        )

    save_config(cfg)
    return RedirectResponse(url="/admin", status_code=303)


# ---------------------------------------------------------------------------
# T4: Per-client threshold overrides save
# ---------------------------------------------------------------------------

@router.post("/clients/{name}/thresholds")
async def save_client_thresholds(
    name: str,
    request: Request,
    reply_rate_warn: str = Form(default=""),
    reply_rate_red: str = Form(default=""),
    sent_pct_warn: str = Form(default=""),
    sent_pct_red: str = Form(default=""),
    bounce_rate_warn: str = Form(default=""),
    bounce_rate_red: str = Form(default=""),
    opps_pct_warn: str = Form(default=""),
    pool_days_warn: str = Form(default=""),
    pool_days_red: str = Form(default=""),
    _: None = Depends(require_admin),
):
    raw = {
        "reply_rate_warn": reply_rate_warn,
        "reply_rate_red": reply_rate_red,
        "sent_pct_warn": sent_pct_warn,
        "sent_pct_red": sent_pct_red,
        "bounce_rate_warn": bounce_rate_warn,
        "bounce_rate_red": bounce_rate_red,
        "opps_pct_warn": opps_pct_warn,
        "pool_days_warn": pool_days_warn,
        "pool_days_red": pool_days_red,
    }

    parsed: dict[str, float] = {}
    parse_errors: list[str] = []
    for field, val in raw.items():
        val = val.strip()
        if val == "":
            continue  # empty = delete override = inherit global
        try:
            parsed[field] = float(val)
        except ValueError:
            parse_errors.append(f"{field} must be a number (got {val!r})")

    if parse_errors:
        return templates.TemplateResponse(
            request,
            "admin.html",
            context=_admin_context(request, errors=parse_errors),
            status_code=400,
        )

    cfg = copy.deepcopy(get_config())
    cfg.setdefault("clients", {})
    client_cfg = cfg["clients"].setdefault(name, {})

    # Rebuild thresholds: start from existing, remove keys not submitted
    # (empty submission = inherit global = delete from overrides)
    submitted_keys = {k for k, v in raw.items() if v.strip() != ""}
    existing_thresholds = client_cfg.get("thresholds", {})
    # Keep only keys that were submitted (non-empty); update with parsed values
    new_thresholds = {k: v for k, v in existing_thresholds.items() if k in submitted_keys}
    new_thresholds.update(parsed)
    client_cfg["thresholds"] = new_thresholds

    is_valid, errors = validate_config(cfg)
    if not is_valid:
        return templates.TemplateResponse(
            request,
            "admin.html",
            context=_admin_context(request, errors=errors),
            status_code=400,
        )

    save_config(cfg)
    return RedirectResponse(url="/admin", status_code=303)


# ---------------------------------------------------------------------------
# T5: Config export
# ---------------------------------------------------------------------------

@router.get("/config/export")
async def export_config(_: None = Depends(require_admin)):
    cfg = get_config()
    payload = json.dumps(cfg, indent=2)
    return JSONResponse(
        content=cfg,
        headers={"Content-Disposition": 'attachment; filename="dashboard_config.json"'},
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.get("/logout")
async def admin_logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(key="admin_session", path="/admin")
    return response
