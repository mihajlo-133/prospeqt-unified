from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.auth import check_password, create_session_token, require_admin
from app.services.workspace import add_workspace, list_workspaces, remove_workspace

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


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
        context={"error": "Incorrect password. Try again."},
        status_code=200,
    )


@router.get("", response_class=HTMLResponse)
async def admin_panel(request: Request, _: None = Depends(require_admin)):
    workspaces = list_workspaces()
    return templates.TemplateResponse(request, "admin.html", context={"workspaces": workspaces})


@router.post("/workspaces")
async def add_workspace_route(
    request: Request,
    workspace_name: str = Form(...),
    api_key: str = Form(...),
    _: None = Depends(require_admin),
):
    name = workspace_name.strip()
    key = api_key.strip()
    if not name or not key:
        workspaces = list_workspaces()
        return templates.TemplateResponse(
            request,
            "admin.html",
            context={"workspaces": workspaces, "error": "Both Workspace Name and API Key are required."},
            status_code=400,
        )
    add_workspace(name.lower().replace(" ", "-"), key)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/workspaces/{name}/delete")
async def remove_workspace_route(
    name: str,
    request: Request,
    _: None = Depends(require_admin),
):
    remove_workspace(name)
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/logout")
async def admin_logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(key="admin_session", path="/admin")
    return response
