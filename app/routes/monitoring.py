from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.monitoring_cache import get_all_monitoring_data, invalidate_all
from app.services.monitoring_poller import refresh_all_clients_sync
from app.services.monitoring_view import (
    SORT_OPTIONS,
    TABLE_SORT_OPTIONS,
    aggregate_summary,
    build_card_view,
    build_drilldown_view,
    build_table_row_view,
    normalize_sort,
    normalize_table_dir,
    normalize_table_sort,
    sort_clients,
    sort_clients_table,
)
from app.services.registry import get_client, list_monitoring_workspaces

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _format_updated_at() -> str:
    """Return current local time as HH:MM:SS (24h, en-GB)."""
    return datetime.now().strftime("%H:%M:%S")


def _build_card_views(data: dict, sort: str) -> list[dict]:
    sorted_clients = sort_clients(data, sort)
    cards: list[dict] = []
    for name, entry in sorted_clients:
        reg = get_client(name)
        workspace_id = reg.workspace_id if reg else None
        slug = reg.slug if reg else None
        cards.append(build_card_view(name, entry, workspace_id, slug=slug))
    return cards


def _build_table_views(data: dict, sort: str, direction: str) -> list[dict]:
    sorted_clients = sort_clients_table(data, sort, direction)
    rows: list[dict] = []
    for name, entry in sorted_clients:
        reg = get_client(name)
        workspace_id = reg.workspace_id if reg else None
        slug = reg.slug if reg else None
        rows.append(build_table_row_view(name, entry, workspace_id, slug=slug))
    return rows


def _drilldown_context(slug: str) -> tuple[dict, int]:
    """Look up a client by slug and return (template_context, status_code)."""
    slug_norm = slug.lower()
    entry_def = next(
        (e for e in list_monitoring_workspaces() if e.slug.lower() == slug_norm),
        None,
    )

    if entry_def is None:
        return (
            {
                "active_tab":      "monitoring",
                "slug":            slug,
                "name":            slug,
                "not_found":       True,
                "entry":           None,
                "card":            None,
                "platform":        None,
                "workspace_id":    None,
                "instantly_url":   None,
                "updated_at":      _format_updated_at(),
                "error_msg":       None,
                "alerts":          [],
                "kpis":            None,
                "campaign_groups": [],
                "is_emailbison":   False,
            },
            404,
        )

    name = entry_def.name
    data = get_all_monitoring_data()
    entry = data.get(name) or {}
    view = build_drilldown_view(
        name=name,
        entry=entry,
        workspace_id=entry_def.workspace_id,
        platform=entry_def.platform,
        slug=entry_def.slug,
    )

    return (
        {
            "active_tab":      "monitoring",
            "slug":            entry_def.slug,
            "name":            name,
            "entry":           entry,
            "card":            view["card"],
            "platform":        entry_def.platform,
            "workspace_id":    entry_def.workspace_id,
            "instantly_url":   view["instantly_url"],
            "updated_at":      _format_updated_at(),
            "not_found":       False,
            "error_msg":       entry.get("error") if entry.get("status") == "error" else None,
            "alerts":          view["alerts"],
            "kpis":            view["kpis"],
            "campaign_groups": view["campaign_groups"],
            "is_emailbison":   view["is_emailbison"],
        },
        200,
    )


@router.get("/monitoring", response_class=HTMLResponse)
async def monitoring_overview(
    request: Request,
    sort: str = "status",
    table_sort: str = "status",
    table_dir: str = "desc",
):
    """Monitoring overview — cards (mobile) + desktop table."""
    sort = normalize_sort(sort)
    table_sort = normalize_table_sort(table_sort)
    table_dir = normalize_table_dir(table_dir)

    data = get_all_monitoring_data()
    summary = aggregate_summary(data)
    cards = _build_card_views(data, sort)
    rows = _build_table_views(data, table_sort, table_dir)

    return templates.TemplateResponse(
        request,
        "monitoring.html",
        {
            "active_tab":         "monitoring",
            "cards":              cards,
            "rows":               rows,
            "summary":            summary,
            "sort":               sort,
            "sort_options":       SORT_OPTIONS,
            "table_sort":         table_sort,
            "table_dir":          table_dir,
            "table_sort_options": TABLE_SORT_OPTIONS,
            "updated_at":         _format_updated_at(),
        },
    )


@router.get("/api/monitoring/cards", response_class=HTMLResponse)
async def monitoring_cards_partial(request: Request, sort: str = "status"):
    sort = normalize_sort(sort)
    data = get_all_monitoring_data()
    cards = _build_card_views(data, sort)
    return templates.TemplateResponse(
        request,
        "_monitoring_cards.html",
        {"cards": cards, "sort": sort},
    )


@router.get("/api/monitoring/table", response_class=HTMLResponse)
async def monitoring_table_partial(
    request: Request,
    sort: str = "status",
    dir: str = "desc",
):
    """Desktop table partial — used by HTMX sort header swaps + auto-refresh."""
    sort = normalize_table_sort(sort)
    direction = normalize_table_dir(dir)
    data = get_all_monitoring_data()
    rows = _build_table_views(data, sort, direction)
    return templates.TemplateResponse(
        request,
        "_monitoring_table.html",
        {"rows": rows, "sort": sort, "direction": direction},
    )


@router.get("/api/monitoring/drilldown/{slug}", response_class=HTMLResponse)
async def monitoring_drill_down_partial(request: Request, slug: str):
    """Drill-down partial — shared between card click and table row expand."""
    ctx, status_code = _drilldown_context(slug)
    return templates.TemplateResponse(
        request,
        "_monitoring_drilldown.html",
        ctx,
        status_code=status_code,
    )


@router.get("/monitoring/{slug}", response_class=HTMLResponse)
async def monitoring_drill_down(request: Request, slug: str):
    """Full-page drill-down. No-JS fallback; wraps _monitoring_drilldown.html."""
    ctx, status_code = _drilldown_context(slug)
    return templates.TemplateResponse(
        request,
        "monitoring_drilldown.html",
        ctx,
        status_code=status_code,
    )


@router.post("/api/monitoring/refresh", response_class=HTMLResponse)
async def monitoring_refresh(request: Request, sort: str = "status"):
    # Re-fetch all clients synchronously so the response contains fresh data.
    # refresh_all_clients_sync() fans out via asyncio.gather, reusing
    # _refresh_one_client, so dispatch logic stays in one place.
    # No scheduler reschedule needed: coalesce=True + max_instances=1 means
    # the next 60s poll tick will see nothing stale and skip naturally.
    await refresh_all_clients_sync()
    sort = normalize_sort(sort)
    data = get_all_monitoring_data()
    cards = _build_card_views(data, sort)
    return templates.TemplateResponse(
        request,
        "_monitoring_cards.html",
        {"cards": cards, "sort": sort},
    )
