"""Async Instantly v2 monitoring API client.

Port of `fetch_instantly_data` from
`gtm/prospeqt-outreach-dashboard/server.py` (lines 460-690) to async httpx.

Six endpoints:
    GET /campaigns                       — cursor-paginated list
    GET /campaigns/analytics             — cursor-paginated workspace analytics
    GET /campaigns/analytics/daily
        ?campaign_id=&start_date&end_date
        &include_opportunities_count=true — per-campaign today (parallel fan-out)
    GET /campaigns/analytics/daily
        ?start_date&end_date
        &include_opportunities_count=true — workspace-wide 7-day window
    GET /campaigns/analytics/steps       — step analytics (15-min cache)
    POST /leads/list
        filter=FILTER_VAL_NOT_CONTACTED  — Phase 2 backfill (T3 helper)

Concurrency: module-owned `asyncio.Semaphore(5)` per workspace in
`_monitoring_inst_semaphores`, deliberately separate from
`app/api/instantly.py::_semaphores` (QA) and
`app/api/monitoring_emailbison.py::_monitoring_eb_semaphores`.

Step analytics have their own 15-min TTL cache (`STEP_CACHE_TTL = 900`)
per GOAL.md 4.4.6.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.services.monitoring import (
    _count_not_contacted_from_analytics,
    _safe_num,
    _trend,
)

logger = logging.getLogger(__name__)

INSTANTLY_BASE = "https://api.instantly.ai/api/v2"

#: Eastern Time — "today" for per-campaign metrics matches Instantly's UI
#: business-day boundary (GOAL.md 4.5.1).
EASTERN = ZoneInfo("America/New_York")

#: Step analytics cache TTL (seconds). GOAL.md 4.4.6.
STEP_CACHE_TTL = 900

#: Per-workspace semaphores — separate from QA and EB monitoring.
_monitoring_inst_semaphores: dict[str, asyncio.Semaphore] = {}

#: Step-analytics cache: client_name → (timestamp, cleaned_steps).
_step_cache: dict[str, tuple[float, list[dict]]] = {}
_step_cache_lock = asyncio.Lock()


def _get_semaphore(workspace_name: str) -> asyncio.Semaphore:
    """Return (creating if needed) the per-workspace monitoring semaphore."""
    sem = _monitoring_inst_semaphores.get(workspace_name)
    if sem is None:
        sem = asyncio.Semaphore(5)
        _monitoring_inst_semaphores[workspace_name] = sem
    return sem


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _inst_get(
    client: httpx.AsyncClient,
    path: str,
    api_key: str,
    *,
    params: dict | None = None,
) -> Any:
    """Async GET against `INSTANTLY_BASE` with bearer auth. Raises on non-2xx."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent":    "ProspeqtDashboard/monitoring",
    }
    resp = await client.get(f"{INSTANTLY_BASE}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


async def _paginate(
    client: httpx.AsyncClient,
    path: str,
    api_key: str,
    *,
    limit: int = 100,
    extra_params: dict | None = None,
) -> list[dict]:
    """Cursor-paginate an Instantly v2 endpoint to exhaustion.

    Instantly returns either:
      * A dict `{"items": [...], "next_starting_after": "..."}` for paginated
        endpoints like `/campaigns` and `/campaigns/analytics`.
      * A bare list for non-paginated analytics endpoints (daily, steps).

    Returns a flat list either way. Stops when `next_starting_after` is
    missing or the page is smaller than `limit`.
    """
    results: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": limit}
        if extra_params:
            params.update(extra_params)
        if cursor:
            params["starting_after"] = cursor
        data = await _inst_get(client, path, api_key, params=params)
        if isinstance(data, list):
            # Non-paginated (plain array) — single fetch, done.
            results.extend(data)
            return results
        items = data.get("items", []) if isinstance(data, dict) else []
        results.extend(items)
        next_cursor = data.get("next_starting_after") if isinstance(data, dict) else None
        if not next_cursor or len(items) < limit:
            return results
        cursor = next_cursor


# ---------------------------------------------------------------------------
# Per-campaign helpers
# ---------------------------------------------------------------------------

async def _fetch_campaign_daily(
    client: httpx.AsyncClient,
    api_key: str,
    campaign_id: str,
    date_str: str,
) -> dict[str, int]:
    """Fetch per-campaign daily analytics for a single date.

    Returns `{sent, first_touch, followups, replies, opps}`. Errors return
    zeros — a single bad campaign must not break the workspace fetch.
    """
    try:
        data = await _inst_get(
            client,
            "/campaigns/analytics/daily",
            api_key,
            params={
                "campaign_id": campaign_id,
                "start_date": date_str,
                "end_date": date_str,
                "include_opportunities_count": "true",
                "limit": 10,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Instantly daily fetch failed for campaign %s: %s", campaign_id, exc)
        return {"sent": 0, "first_touch": 0, "followups": 0, "replies": 0, "opps": 0}

    rows = data if isinstance(data, list) else []
    row = next((d for d in rows if d.get("date") == date_str), {}) if rows else {}
    sent = _safe_num(row.get("sent"))
    first_touch = _safe_num(row.get("new_leads_contacted"))
    return {
        "sent":        sent,
        "first_touch": first_touch,
        "followups":   max(0, sent - first_touch),
        "replies":     _safe_num(row.get("replies")),
        "opps":        _safe_num(row.get("opportunities")),
    }


async def _get_step_analytics(
    client: httpx.AsyncClient,
    client_name: str,
    api_key: str,
) -> list[dict]:
    """Fetch step analytics with 15-min cache (GOAL.md 4.4.6).

    Cache key is the client name. Filters out junk rows where `step` is
    `None` or the literal string "null" (a known Instantly response quirk).
    """
    now = time.time()
    async with _step_cache_lock:
        cached = _step_cache.get(client_name)
        if cached and (now - cached[0]) < STEP_CACHE_TTL:
            return list(cached[1])

    try:
        steps = await _inst_get(client, "/campaigns/analytics/steps", api_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Instantly steps fetch failed for %s: %s", client_name, exc)
        steps = []

    if not isinstance(steps, list):
        steps = []
    clean_steps = [
        s for s in steps
        if isinstance(s, dict)
        and s.get("step") is not None
        and str(s.get("step")) != "null"
    ]

    async with _step_cache_lock:
        _step_cache[client_name] = (time.time(), clean_steps)

    return clean_steps


# ---------------------------------------------------------------------------
# Public fetcher
# ---------------------------------------------------------------------------

async def fetch_instantly_data(
    client: httpx.AsyncClient,
    client_name: str,
    api_key: str,
) -> dict:
    """Fetch a monitoring snapshot for a single Instantly workspace.

    Returns a dict matching the ClientData contract in
    `tests/fixtures/mock_monitoring_data.json`.

    Raises `RuntimeError` if the initial `/campaigns` or `/campaigns/analytics`
    call fails — the caller (T6 cache) converts to a friendly error. Errors
    on downstream per-campaign daily fetches and the workspace daily window
    are caught locally and degrade to zeros.

    The returned dict also contains two internal keys consumed by T7's
    background backfill:
        `_nc_backfill`: list[str] — all campaign ids eligible for Phase 2
        `_nc_api_key`:  str       — the bearer token to reuse
    These keys are stripped from the dict before caching by T6 (so the
    shape handed to the template/UI matches the contract exactly).
    """
    sem = _get_semaphore(client_name)

    async with sem:
        today = datetime.now(EASTERN).date()
        seven_ago = today - timedelta(days=7)
        today_str = today.isoformat()

        # 1. Campaigns list (for status/active count)
        try:
            campaigns = await _paginate(client, "/campaigns", api_key)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Instantly /campaigns failed for {client_name}: {exc}") from exc

        active_campaigns = [c for c in campaigns if c.get("status") == 1]
        active_ids: set[str] = {c.get("id") for c in active_campaigns if c.get("id")}

        # 2. All-time analytics (for lead counts, bounce, pipeline)
        try:
            analytics = await _paginate(client, "/campaigns/analytics", api_key)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Instantly /campaigns/analytics failed for {client_name}: {exc}") from exc

        analytics_by_id = {a.get("campaign_id"): a for a in analytics if a.get("campaign_id")}
        active_analytics = [a for a in analytics if a.get("campaign_id") in active_ids]

        active_sent    = sum(_safe_num(a.get("emails_sent_count")) for a in active_analytics)
        active_bounced = sum(_safe_num(a.get("bounced_count")) for a in active_analytics)

    # 3. Per-campaign daily for TODAY — fan out across active campaigns
    # Each _fetch_campaign_daily call re-acquires the semaphore so the
    # concurrency limit applies to the flood of per-campaign calls.
    async def _fetch_daily(cid: str) -> tuple[str, dict]:
        async with sem:
            row = await _fetch_campaign_daily(client, api_key, cid, today_str)
        return cid, row

    daily_by_campaign: dict[str, dict] = {}
    active_cids_ordered = [c.get("id") for c in active_campaigns if c.get("id")]
    if active_cids_ordered:
        results = await asyncio.gather(
            *(_fetch_daily(cid) for cid in active_cids_ordered),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Instantly per-campaign daily fetch failed for %s: %s", client_name, r)
                continue
            cid, row = r
            daily_by_campaign[cid] = row

    sent_today        = sum(d.get("sent", 0) for d in daily_by_campaign.values())
    first_touch_today = sum(d.get("first_touch", 0) for d in daily_by_campaign.values())
    followup_today    = sum(d.get("followups", 0) for d in daily_by_campaign.values())
    replies_today     = sum(d.get("replies", 0) for d in daily_by_campaign.values())
    opps_today        = sum(d.get("opps", 0) for d in daily_by_campaign.values())

    # 4. Workspace-wide 7-day daily window (for trends / averages)
    async with sem:
        try:
            daily_raw = await _inst_get(
                client,
                "/campaigns/analytics/daily",
                api_key,
                params={
                    "start_date": seven_ago.isoformat(),
                    "end_date":   today_str,
                    "include_opportunities_count": "true",
                    "limit": 100,
                },
            )
            daily_data = daily_raw if isinstance(daily_raw, list) else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Instantly workspace daily fetch failed for %s: %s", client_name, exc)
            daily_data = []

    past_days = [d for d in daily_data if d.get("date", "") < today_str]
    if past_days:
        avg_sent_7d    = sum(_safe_num(d.get("sent")) for d in past_days) / len(past_days)
        avg_replies_7d = sum(_safe_num(d.get("replies")) for d in past_days) / len(past_days)
        avg_opps_7d    = sum(_safe_num(d.get("opportunities")) for d in past_days) / len(past_days)
    else:
        avg_sent_7d = avg_replies_7d = avg_opps_7d = 0.0

    reply_rate_today = (replies_today / sent_today * 100) if sent_today > 0 else 0.0
    reply_rate_7d    = (avg_replies_7d / avg_sent_7d * 100) if avg_sent_7d > 0 else 0.0
    bounce_rate      = (active_bounced / active_sent * 100) if active_sent > 0 else 0.0

    # 5. Not-contacted per campaign (Phase 1 fast estimate from analytics)
    nc_by_campaign: dict[str, int] = {}
    for a in analytics:
        cid = a.get("campaign_id", "")
        if cid:
            nc_by_campaign[cid] = _count_not_contacted_from_analytics(a)

    not_contacted = sum(nc_by_campaign.get(cid, 0) for cid in active_ids if cid)
    in_progress = sum(
        max(
            0,
            _safe_num(a.get("leads_count"))
            - _safe_num(a.get("completed_count"))
            - _safe_num(a.get("bounced_count"))
        ) - nc_by_campaign.get(a.get("campaign_id", ""), 0)
        for a in active_analytics
    )

    opp_trend   = _trend(opps_today, avg_opps_7d)
    reply_trend = _trend(reply_rate_today, reply_rate_7d)
    sent_trend  = _trend(sent_today, avg_sent_7d)

    # 6. Per-campaign breakdown
    campaigns_list: list[dict] = []
    for c in campaigns:
        cid = c.get("id", "")
        is_active = c.get("status") == 1
        a = analytics_by_id.get(cid, {})
        daily = daily_by_campaign.get(cid, {})
        nc = nc_by_campaign.get(cid, 0)
        leads = _safe_num(a.get("leads_count"))
        completed = _safe_num(a.get("completed_count"))
        bounced = _safe_num(a.get("bounced_count"))
        contacted = _safe_num(a.get("new_leads_contacted_count"))
        camp_sent_today = daily.get("sent", 0)
        camp_replies_today = daily.get("replies", 0)

        campaigns_list.append({
            "name":            c.get("name", "Unknown"),
            "id":              cid,
            "status":          "active" if is_active else "paused",
            "sent_today":      camp_sent_today,
            "first_touch":     daily.get("first_touch", 0),
            "followups":       daily.get("followups", 0),
            "replies_today":   camp_replies_today,
            "opps_today":      daily.get("opps", 0),
            "reply_rate":      round(camp_replies_today / camp_sent_today * 100, 2) if camp_sent_today > 0 else 0.0,
            "not_contacted":   nc,
            "in_progress":     max(0, contacted - completed - bounced),
            "total_sent":      _safe_num(a.get("emails_sent_count")),
            "total_leads":     leads,
            "total_completed": completed,
            "total_bounced":   bounced,
        })

    return {
        "platform":          "instantly",
        "active_campaigns":  len(active_campaigns),
        "total_campaigns":   len(campaigns),

        "sent_today":        sent_today,
        "first_touch_today": first_touch_today,
        "followup_today":    followup_today,
        "replies_today":     replies_today,
        "opps_today":        opps_today,

        "reply_rate_today":  round(reply_rate_today, 2),
        "reply_rate_7d":     round(reply_rate_7d, 2),
        "bounce_rate":       round(bounce_rate, 2),

        "not_contacted":     not_contacted,
        "in_progress":       in_progress,

        "avg_sent_7d":       round(avg_sent_7d, 1),
        "avg_replies_7d":    round(avg_replies_7d, 1),
        "avg_opps_7d":       round(avg_opps_7d, 1),

        "opp_trend":         opp_trend,
        "reply_trend":       reply_trend,
        "sent_trend":        sent_trend,

        "campaigns":         campaigns_list,

        "daily": [
            {
                "date":    d.get("date"),
                "sent":    _safe_num(d.get("sent")),
                "opps":    _safe_num(d.get("opportunities")),
                "replies": _safe_num(d.get("replies")),
            }
            for d in sorted(daily_data, key=lambda x: x.get("date", ""))
        ],

        # Internal keys for T7 Phase-2 backfill. T6 strips these before
        # exposing via get_all_monitoring_data().
        "_nc_backfill": [c.get("id") for c in campaigns if c.get("id")],
        "_nc_api_key":  api_key,
    }


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _reset_semaphores_for_tests() -> None:
    """Clear the per-workspace semaphore map. Test-only."""
    _monitoring_inst_semaphores.clear()


def _reset_step_cache_for_tests() -> None:
    """Clear the step analytics cache. Test-only."""
    _step_cache.clear()


__all__ = [
    "INSTANTLY_BASE",
    "EASTERN",
    "STEP_CACHE_TTL",
    "fetch_instantly_data",
    "_get_step_analytics",
    "_fetch_campaign_daily",
    "_paginate",
    "_reset_semaphores_for_tests",
    "_reset_step_cache_for_tests",
]
