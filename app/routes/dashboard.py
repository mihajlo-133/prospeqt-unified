import math
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.cache import get_cache
from app.services.poller import get_scanning_workspace_names, trigger_qa_all, trigger_qa_campaign, trigger_qa_workspace
from app.services.workspace import list_workspaces

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# ---------------------------------------------------------------------------
# Utility functions (used by routes and templates)
# ---------------------------------------------------------------------------

LEAD_STATUS_LABELS = {1: "Active", 2: "Paused", 3: "Completed", -1: "Bounced"}


def health_class(broken: int, total: int) -> str:
    """Return CSS modifier class for traffic light dot. Per D-08/D-09."""
    if broken == 0:
        return "green"
    if total == 0:
        return "green"
    pct = broken / total
    if pct < 0.02:
        return "green"
    elif pct <= 0.10:
        return "yellow"
    return "red"


def health_pct(broken: int, total: int) -> str:
    """Return percentage string like '3.2%' or '0%'. Guards zero division."""
    if total == 0:
        return "0%"
    return f"{broken / total * 100:.1f}%"


def freshness_class(ts: datetime | None) -> str:
    """Return CSS class for freshness indicator. Per D-18."""
    if ts is None:
        return "gray"
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age < 300:
        return "green"
    elif age < 900:
        return "amber"
    return "gray"


def freshness_text(ts: datetime | None) -> str:
    """Return human-readable freshness string."""
    if ts is None:
        return "Never scanned"
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age < 60:
        return "Just now"
    elif age < 3600:
        mins = int(age // 60)
        return f"{mins} min ago"
    hours = int(age // 3600)
    return f"{hours}h ago"


def total_leads_for_workspace(ws) -> int:
    """Sum total_leads across all campaigns in a workspace."""
    return sum(c.total_leads for c in ws.campaigns)


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Overview page: all workspaces with health status. Per VIEW-01, D-04/D-05/D-06."""
    data = await get_cache().get_all()
    # Build a set of workspace names that have cache data
    cached_names = {ws.workspace_name for ws in data.workspaces}
    ws_display = []
    for ws in data.workspaces:
        ws_total = total_leads_for_workspace(ws)
        ws_display.append({
            "name": ws.workspace_name,
            "broken": ws.total_broken,
            "total": ws_total,
            "health": health_class(ws.total_broken, ws_total),
            "pct": health_pct(ws.total_broken, ws_total),
            "campaign_count": len(ws.campaigns),
            "freshness_cls": freshness_class(ws.last_checked),
            "freshness_txt": freshness_text(ws.last_checked),
            "error": ws.error,
        })
    # Show registered workspaces that have no cache data yet (not-scanned state)
    for ws_info in list_workspaces():
        if ws_info["name"] not in cached_names:
            ws_display.append({
                "name": ws_info["name"],
                "broken": 0,
                "total": 0,
                "health": "not-scanned",
                "pct": 0,
                "campaign_count": 0,
                "freshness_cls": "stale",
                "freshness_txt": "Not scanned",
                "error": None,
            })
    # Sort alphabetically for stable order across refreshes
    ws_display.sort(key=lambda w: w["name"].lower())

    # Check for in-flight scans so the grid shows scanning state on initial load
    scanning_now = get_scanning_workspace_names()
    for ws in ws_display:
        ws["scanning"] = ws["name"] in scanning_now
    has_scanning = bool(scanning_now)

    # Aggregate stats for summary chips
    red_count = sum(1 for w in ws_display if w["health"] == "red")
    amber_count = sum(1 for w in ws_display if w["health"] == "yellow")
    green_count = sum(1 for w in ws_display if w["health"] == "green")
    total_campaigns = sum(w["campaign_count"] for w in ws_display)
    total_broken = sum(w["broken"] for w in ws_display)

    return templates.TemplateResponse(request, "dashboard.html", {
        "workspaces": ws_display,
        "ws_count": len(ws_display),
        "red_count": red_count,
        "amber_count": amber_count,
        "green_count": green_count,
        "total_campaigns": total_campaigns,
        "total_broken": total_broken,
        "polling": has_scanning,
    })


@router.get("/ws/{ws_name}", response_class=HTMLResponse)
async def workspace_detail(request: Request, ws_name: str):
    """Workspace detail page: campaign table. Per VIEW-02, VIEW-05, VIEW-06, D-03."""
    # Build sidebar workspace list with health + scanning status
    all_data = await get_cache().get_all()
    cached_map = {ws.workspace_name: ws for ws in all_data.workspaces}
    scanning_now = get_scanning_workspace_names()
    sidebar_ws = []
    for ws_info in list_workspaces():
        name = ws_info["name"]
        ws_obj = cached_map.get(name)
        if ws_obj:
            ws_t = total_leads_for_workspace(ws_obj)
            sidebar_ws.append({"name": name, "health": health_class(ws_obj.total_broken, ws_t), "active": name == ws_name, "scanning": name in scanning_now})
        else:
            sidebar_ws.append({"name": name, "health": "not-scanned", "active": name == ws_name, "scanning": name in scanning_now})
    sidebar_ws.sort(key=lambda w: w["name"].lower())
    current_ws_scanning = ws_name in scanning_now

    result = cached_map.get(ws_name)
    if result is None:
        return templates.TemplateResponse(request, "workspace.html", {
            "ws_name": ws_name,
            "campaigns": [],
            "ws_broken": 0,
            "ws_total": 0,
            "ws_health": "gray",
            "ws_pct": "0%",
            "not_scanned": True,
            "sidebar_workspaces": sidebar_ws,
            "ws_scanning": current_ws_scanning,
            "polling": current_ws_scanning,
        })
    ws_total = total_leads_for_workspace(result)
    campaigns_display = []
    for c in result.campaigns:
        affected_vars = list(c.issues_by_variable.keys())
        if len(affected_vars) > 2:
            var_text = ", ".join(affected_vars[:2]) + f" +{len(affected_vars) - 2} more"
        elif affected_vars:
            var_text = ", ".join(affected_vars)
        else:
            var_text = ""
        campaigns_display.append({
            "id": c.campaign_id,
            "name": c.campaign_name,
            "status": "active" if c.total_leads > 0 else "draft",
            "broken": c.broken_count,
            "total": c.total_leads,
            "health": health_class(c.broken_count, c.total_leads),
            "pct": health_pct(c.broken_count, c.total_leads),
            "var_text": var_text,
            "freshness_cls": freshness_class(c.last_checked),
            "freshness_txt": freshness_text(c.last_checked),
        })
    return templates.TemplateResponse(request, "workspace.html", {
        "ws_name": ws_name,
        "campaigns": campaigns_display,
        "ws_broken": result.total_broken,
        "ws_total": ws_total,
        "ws_health": health_class(result.total_broken, ws_total),
        "ws_pct": health_pct(result.total_broken, ws_total),
        "not_scanned": False,
        "sidebar_workspaces": sidebar_ws,
        "ws_scanning": current_ws_scanning,
        "polling": current_ws_scanning,
    })


@router.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Scan API routes (HTMX endpoints, return partial HTML)
# ---------------------------------------------------------------------------


async def _build_workspace_grid_response(
    request: Request,
    polling: bool = False,
    scanning_names: set | None = None,
):
    """Build the workspace grid partial response.

    Args:
        polling: If True, grid auto-refreshes via HTMX until scanned workspaces arrive.
        scanning_names: Set of workspace names currently being scanned.
            Only these show the "Scanning..." animation. If None and polling=True,
            all not-scanned workspaces are treated as scanning (scan-all behavior).
    """
    data = await get_cache().get_all()
    cached_names = {ws.workspace_name for ws in data.workspaces}
    ws_display = []
    for ws in data.workspaces:
        ws_total = total_leads_for_workspace(ws)
        ws_display.append({
            "name": ws.workspace_name,
            "broken": ws.total_broken,
            "total": ws_total,
            "health": health_class(ws.total_broken, ws_total),
            "pct": health_pct(ws.total_broken, ws_total),
            "campaign_count": len(ws.campaigns),
            "freshness_cls": freshness_class(ws.last_checked),
            "freshness_txt": freshness_text(ws.last_checked),
            "error": ws.error,
        })
    for ws_info in list_workspaces():
        if ws_info["name"] not in cached_names:
            ws_display.append({
                "name": ws_info["name"],
                "broken": 0,
                "total": 0,
                "health": "not-scanned",
                "pct": 0,
                "campaign_count": 0,
                "freshness_cls": "stale",
                "freshness_txt": "Not scanned",
                "error": None,
            })
    # Sort alphabetically for stable order across refreshes
    ws_display.sort(key=lambda w: w["name"].lower())

    # Determine which workspaces are actively scanning
    if scanning_names is None and polling:
        # scan-all: every not-scanned workspace is scanning
        active_scanning = {w["name"] for w in ws_display if w["health"] == "not-scanned"}
    elif scanning_names:
        active_scanning = scanning_names
    else:
        active_scanning = set()

    # Mark each workspace individually
    for ws in ws_display:
        ws["scanning"] = ws["name"] in active_scanning

    has_scanning = bool(active_scanning)
    return templates.TemplateResponse(request, "_workspace_grid.html", {
        "workspaces": ws_display,
        "ws_count": len(ws_display),
        "polling": polling and has_scanning,
    })


@router.post("/api/scan/all", response_class=HTMLResponse)
async def scan_all(request: Request, background_tasks: BackgroundTasks):
    """Trigger QA across all workspaces and redirect to overview to show scan progress."""
    background_tasks.add_task(trigger_qa_all)
    from fastapi.responses import Response
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/"
    return response


@router.get("/api/workspace-grid", response_class=HTMLResponse)
async def workspace_grid_partial(request: Request):
    """Return the workspace grid partial for HTMX polling. Only shows scanning for workspaces with in-flight scans."""
    active = get_scanning_workspace_names()
    return await _build_workspace_grid_response(
        request, polling=bool(active), scanning_names=active,
    )


@router.get("/api/ws-campaigns/{ws_name}", response_class=HTMLResponse)
async def workspace_campaigns_partial(request: Request, ws_name: str):
    """Return campaign table partial for HTMX polling on workspace detail page."""
    return await _build_campaign_table_response(request, ws_name, polling=True)


# ---------------------------------------------------------------------------
# Campaign detail page and scan endpoint
# ---------------------------------------------------------------------------

PAGE_SIZE = 25


@router.get("/ws/{ws_name}/campaign/{campaign_id}", response_class=HTMLResponse)
async def campaign_detail(request: Request, ws_name: str, campaign_id: str, page: int = 1):
    """Campaign detail page: variable breakdown + broken leads table. Per VIEW-03, VIEW-04, D-11/D-12/D-13."""
    result = await get_cache().get_workspace(ws_name)
    campaign = None
    if result:
        campaign = next((c for c in result.campaigns if c.campaign_id == campaign_id), None)

    if campaign is None:
        # Campaign not found in QA results — show not-scanned state
        return templates.TemplateResponse(request, "campaign.html", {
            "ws_name": ws_name,
            "campaign_id": campaign_id,
            "campaign_name": campaign_id,
            "not_scanned": True,
            "campaign_status": "draft",
            "total_leads": 0,
            "broken_count": 0,
            "health": "gray",
            "pct": "0%",
            "freshness_cls": "gray",
            "freshness_txt": "Never scanned",
            "variables": [],
            "page_leads": [],
            "page": 1,
            "total_pages": 1,
            "total_broken_count": 0,
            "lead_status_labels": LEAD_STATUS_LABELS,
        })

    # Variable summary sorted by count descending (per D-12, UI-SPEC)
    variables = sorted(
        [
            {"name": var_name, "count": count, "pct": count / campaign.total_leads * 100 if campaign.total_leads > 0 else 0}
            for var_name, count in campaign.issues_by_variable.items()
        ],
        key=lambda v: v["count"],
        reverse=True,
    )

    # Pagination (per D-15: 25 per page)
    total_broken_count = len(campaign.broken_leads)
    total_pages = max(1, math.ceil(total_broken_count / PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    page_leads = campaign.broken_leads[start:start + PAGE_SIZE]

    # Format broken_vars for display: value -> display string
    def format_var_value(value):
        if value is None:
            return "[missing]"
        elif value == "":
            return "[empty]"
        elif value == "NO":
            return "NO"
        return value

    display_leads = []
    for bl in page_leads:
        formatted_vars = {k: format_var_value(v) for k, v in bl.broken_vars.items()}
        display_leads.append({
            "email": bl.email,
            "lead_status": LEAD_STATUS_LABELS.get(bl.lead_status, f"Unknown ({bl.lead_status})"),
            "broken_vars": formatted_vars,
        })

    return templates.TemplateResponse(request, "campaign.html", {
        "ws_name": ws_name,
        "campaign_id": campaign_id,
        "campaign_name": campaign.campaign_name,
        "not_scanned": False,
        "campaign_status": "active" if campaign.total_leads > 0 else "draft",
        "total_leads": campaign.total_leads,
        "broken_count": campaign.broken_count,
        "health": health_class(campaign.broken_count, campaign.total_leads),
        "pct": health_pct(campaign.broken_count, campaign.total_leads),
        "freshness_cls": freshness_class(campaign.last_checked),
        "freshness_txt": freshness_text(campaign.last_checked),
        "variables": variables,
        "page_leads": display_leads,
        "page": page,
        "total_pages": total_pages,
        "total_broken_count": total_broken_count,
        "lead_status_labels": LEAD_STATUS_LABELS,
    })


@router.post("/api/scan/ws/{ws_name}/campaign/{campaign_id}", response_class=HTMLResponse)
async def scan_campaign(request: Request, ws_name: str, campaign_id: str, background_tasks: BackgroundTasks):
    """Trigger QA for one campaign and return current state immediately. Scan runs in background."""
    result = await get_cache().get_workspace(ws_name)
    campaign_dict = None
    if result:
        for c in result.campaigns:
            if c.campaign_id == campaign_id:
                campaign_dict = {"id": campaign_id, "name": c.campaign_name}
                break

    if campaign_dict is None:
        discovered = await get_cache().get_campaigns(ws_name)
        campaign_dict = next((c for c in discovered if c.get("id") == campaign_id), None)

    if campaign_dict:
        background_tasks.add_task(trigger_qa_campaign, campaign_id, campaign_dict, ws_name)

    return await campaign_detail(request, ws_name, campaign_id)


@router.post("/api/scan/ws/{ws_name}", response_class=HTMLResponse)
async def scan_workspace(request: Request, ws_name: str, background_tasks: BackgroundTasks):
    """Trigger QA for one workspace and return partial immediately. Scan runs in background."""
    background_tasks.add_task(trigger_qa_workspace, ws_name)

    # If called from overview page (hx-target is workspace-grid), return full grid
    hx_target = request.headers.get("hx-target", "")
    if hx_target == "workspace-grid":
        # Include all in-flight scans so clicking multiple Scan buttons
        # keeps the animation on all of them, not just the latest click.
        active = get_scanning_workspace_names() | {ws_name}
        return await _build_workspace_grid_response(
            request, polling=True, scanning_names=active,
        )

    # Otherwise return campaign table partial (workspace detail page)
    return await _build_campaign_table_response(request, ws_name, polling=True)


async def _build_campaign_table_response(request: Request, ws_name: str, polling: bool = False):
    """Build campaign table partial for a workspace. Polling adds auto-refresh."""
    result = await get_cache().get_workspace(ws_name)
    if result is None:
        return templates.TemplateResponse(request, "_campaign_table.html", {
            "ws_name": ws_name,
            "campaigns": [],
            "not_scanned": True,
            "polling": polling,
        })
    campaigns_display = []
    for c in result.campaigns:
        affected_vars = list(c.issues_by_variable.keys())
        if len(affected_vars) > 2:
            var_text = ", ".join(affected_vars[:2]) + f" +{len(affected_vars) - 2} more"
        elif affected_vars:
            var_text = ", ".join(affected_vars)
        else:
            var_text = ""
        campaigns_display.append({
            "id": c.campaign_id,
            "name": c.campaign_name,
            "status": "active" if c.total_leads > 0 else "draft",
            "broken": c.broken_count,
            "total": c.total_leads,
            "health": health_class(c.broken_count, c.total_leads),
            "pct": health_pct(c.broken_count, c.total_leads),
            "var_text": var_text,
            "freshness_cls": freshness_class(c.last_checked),
            "freshness_txt": freshness_text(c.last_checked),
        })
    # Keep polling active until data is fresh (< 30s old means scan just completed)
    from datetime import datetime, timezone
    newest = max((c.last_checked for c in result.campaigns if c.last_checked), default=None)
    is_fresh = newest and (datetime.now(timezone.utc) - newest).total_seconds() < 30
    return templates.TemplateResponse(request, "_campaign_table.html", {
        "ws_name": ws_name,
        "campaigns": campaigns_display,
        "not_scanned": False,
        "polling": polling and not is_fresh,
    })
