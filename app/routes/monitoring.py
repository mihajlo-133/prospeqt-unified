from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.monitoring_cache import get_all_monitoring_data, invalidate_all
from app.services.monitoring_view import (
    SORT_OPTIONS,
    aggregate_summary,
    build_card_view,
    normalize_sort,
    sort_clients,
)
from app.services.registry import get_client, list_monitoring_workspaces

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _format_updated_at() -> str:
    """Return current local time as HH:MM:SS (24h, en-GB)."""
    return datetime.now().strftime("%H:%M:%S")


def _build_card_views(data: dict, sort: str) -> list[dict]:
    """Sort clients then build a flat view-model dict per card.

    Looks up the registry entry for each client to get `workspace_id` for
    the "View in Instantly" link. Returns the list the template iterates.
    """
    sorted_clients = sort_clients(data, sort)
    cards: list[dict] = []
    for name, entry in sorted_clients:
        reg = get_client(name)
        workspace_id = reg.workspace_id if reg else None
        slug = reg.slug if reg else None
        cards.append(build_card_view(name, entry, workspace_id, slug=slug))
    return cards


@router.get("/monitoring", response_class=HTMLResponse)
async def monitoring_overview(request: Request, sort: str = "status"):
    """Monitoring overview — full client cards view."""
    sort = normalize_sort(sort)
    data = get_all_monitoring_data()
    summary = aggregate_summary(data)
    cards = _build_card_views(data, sort)
    return templates.TemplateResponse(
        request,
        "monitoring.html",
        {
            "active_tab":   "monitoring",
            "cards":        cards,
            "summary":      summary,
            "sort":         sort,
            "sort_options": SORT_OPTIONS,
            "updated_at":   _format_updated_at(),
        },
    )


@router.get("/api/monitoring/cards", response_class=HTMLResponse)
async def monitoring_cards_partial(request: Request, sort: str = "status"):
    """Return the card grid partial — used by HTMX sort + auto-refresh swaps."""
    sort = normalize_sort(sort)
    data = get_all_monitoring_data()
    cards = _build_card_views(data, sort)
    return templates.TemplateResponse(
        request,
        "_monitoring_cards.html",
        {"cards": cards, "sort": sort},
    )


@router.get("/monitoring/{slug}", response_class=HTMLResponse)
async def monitoring_drill_down(request: Request, slug: str):
    """Per-client drill-down — STUB for Phase 3a (T6).

    Phase 3b will replace this with a full per-client view (campaign table,
    daily timeseries chart, sending account list, breakdowns). The contract
    below is the data envelope that view will receive — it's documented in
    the template too so Phase 3b doesn't need to re-derive it.

    ## Drill-down data contract (the dict passed to the template)

    | Key             | Type    | Source                                | Notes |
    |-----------------|---------|---------------------------------------|-------|
    | `active_tab`    | str     | "monitoring"                          | nav highlight |
    | `slug`          | str     | URL path param                        | URL identity |
    | `name`          | str     | registry display name                 | page title |
    | `entry`         | dict    | get_all_monitoring_data()[name]       | full enriched ClientData (status, kpi, thresholds, campaigns[], daily[], etc.) |
    | `card`          | dict    | build_card_view(name, entry, ws_id)   | same view-model the overview card uses — reuse for the hero zones at the top of the drill-down |
    | `platform`      | str     | "instantly" | "emailbison"             | conditional rendering (Instantly link, EmailBison-only fields) |
    | `workspace_id`  | str|None| registry                              | for "View in Instantly" link |
    | `instantly_url` | str|None| Instantly analytics URL or None       | platform-aware |
    | `updated_at`    | str     | HH:MM:SS local                        | freshness indicator |
    | `not_found`     | bool    | True if slug doesn't match a client   | drives 404 placeholder |
    | `error_msg`     | str|None| friendly error if entry.status=='error' | drives error state |

    Phase 3b will additionally compute:
    - `campaigns_view`   — list[dict] one per campaign with QA-style health
    - `daily_series`     — 7-day [date,sent,replies,opps] for sparkline/chart
    - `breakdown_first_touch_vs_followup` — chart data
    - `accounts`         — sending account list (Instantly only)
    """
    # Look up by slug, case-insensitive. Registry slugs are canonical.
    slug_norm = slug.lower()
    entry_def = next(
        (e for e in list_monitoring_workspaces() if e.slug.lower() == slug_norm),
        None,
    )

    if entry_def is None:
        # Phase 3b may treat 404 differently (real not-found page); for the
        # stub we return a friendly message in the placeholder template.
        return templates.TemplateResponse(
            request,
            "monitoring_drilldown.html",
            {
                "active_tab": "monitoring",
                "slug":       slug,
                "name":       slug,
                "not_found":  True,
                "entry":      None,
                "card":       None,
                "platform":   None,
                "workspace_id": None,
                "instantly_url": None,
                "updated_at": _format_updated_at(),
                "error_msg":  None,
            },
            status_code=404,
        )

    name = entry_def.name
    data = get_all_monitoring_data()
    entry = data.get(name) or {}
    card = build_card_view(name, entry, entry_def.workspace_id, slug=entry_def.slug)

    return templates.TemplateResponse(
        request,
        "monitoring_drilldown.html",
        {
            "active_tab":    "monitoring",
            "slug":          entry_def.slug,
            "name":          name,
            "entry":         entry,
            "card":          card,
            "platform":      entry_def.platform,
            "workspace_id":  entry_def.workspace_id,
            "instantly_url": card.get("instantly_url"),
            "updated_at":    _format_updated_at(),
            "not_found":     False,
            "error_msg":     entry.get("error") if entry.get("status") == "error" else None,
        },
    )


@router.post("/api/monitoring/refresh", response_class=HTMLResponse)
async def monitoring_refresh(request: Request, sort: str = "status"):
    """Manual refresh — invalidate cache TS so the next scheduler cycle
    re-fetches, then immediately return the cards partial. Stale data stays
    visible until the scheduler swaps it out, so users see no skeleton
    flash (GOAL.md 6.1).
    """
    invalidate_all()
    sort = normalize_sort(sort)
    data = get_all_monitoring_data()
    cards = _build_card_views(data, sort)
    return templates.TemplateResponse(
        request,
        "_monitoring_cards.html",
        {"cards": cards, "sort": sort},
    )
