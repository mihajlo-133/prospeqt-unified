"""Async EmailBison monitoring API client.

Port of `fetch_emailbison_data` from
`gtm/prospeqt-outreach-dashboard/server.py` (lines 700-874) to async httpx.

EmailBison has three endpoints we care about:
    GET /campaigns                                      — flat list under `data`
    GET /campaign-events/stats?start_date&end_date&
        campaign_ids[]=<id>...                          — time-series aggregate
    GET /leads?filters[lead_campaign_status]=
        never_contacted&page=1                          — meta.total for not_contacted

Key quirks versus Instantly:
  * Status field is capitalised ("Active", "Paused") — compare case-insensitively.
  * `campaign-events/stats` REQUIRES at least one `campaign_ids[]=` — an empty
    id list returns an error, not an empty series.
  * The stats response is a time-series of label buckets; we flatten it via
    `_parse_events_timeseries`.
  * No per-campaign `not_contacted`, no `in_progress`, no daily breakdown.
  * `first_touch_today` and `followup_today` are always 0 (not exposed).

Concurrency: the module owns its own per-workspace `asyncio.Semaphore(5)`
dict, deliberately separate from `app/api/instantly.py`'s semaphores so
monitoring polling can't starve QA scans (and vice versa).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.services.monitoring import _friendly_error, _safe_num, _trend

logger = logging.getLogger(__name__)

EB_BASE = "https://send.prospeqt.co/api"

#: Eastern Time (GOAL.md 4.5.1) — "today" for per-campaign metrics must use
#: the agency's business day, not UTC.
EASTERN = ZoneInfo("America/New_York")

#: Per-workspace concurrency limit for monitoring fetches. Deliberately
#: separate from `app.api.instantly._semaphores` so monitoring polling and
#: QA scans can't starve each other.
_monitoring_eb_semaphores: dict[str, asyncio.Semaphore] = {}


def _get_semaphore(workspace_name: str) -> asyncio.Semaphore:
    """Return (creating if needed) the monitoring semaphore for a workspace."""
    sem = _monitoring_eb_semaphores.get(workspace_name)
    if sem is None:
        sem = asyncio.Semaphore(5)
        _monitoring_eb_semaphores[workspace_name] = sem
    return sem


# ---------------------------------------------------------------------------
# Response shape helpers
# ---------------------------------------------------------------------------

#: Label strings returned by `/campaign-events/stats`, mapped to our internal
#: flat-dict keys. Unknown labels are dropped silently.
_EVENT_LABEL_MAP: dict[str, str] = {
    "Sent":          "sent",
    "Replied":       "replied",
    "Interested":    "interested",
    "Bounced":       "bounced",
    "Total Opens":   "opens",
    "Unique Opens":  "unique_opens",
    "Unsubscribed":  "unsubscribed",
}


def _parse_events_timeseries(series: list) -> dict[str, int]:
    """Flatten a `/campaign-events/stats` response to `{metric: total}`.

    Response shape:
        [{"label": "Sent", "dates": [["2026-03-23", 5], ["2026-03-24", 7]]}, ...]

    Returns e.g. `{"sent": 12, "replied": 0, ...}`. Missing labels are
    absent from the output (caller uses `.get(..., 0)`).
    """
    totals: dict[str, int] = {}
    for item in series:
        if not isinstance(item, dict):
            continue
        key = _EVENT_LABEL_MAP.get(item.get("label", ""))
        if key is None:
            continue
        totals[key] = sum(
            _safe_num(v)
            for pair in item.get("dates", [])
            if isinstance(pair, (list, tuple)) and len(pair) >= 2
            for _, v in [pair]
        )
    return totals


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

async def _eb_get(
    client: httpx.AsyncClient,
    path: str,
    api_key: str,
    *,
    params: dict | None = None,
) -> dict | list:
    """Async GET against `EB_BASE` with bearer auth. Raises on non-2xx."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept":        "application/json",
        "User-Agent":    "ProspeqtDashboard/monitoring",
    }
    resp = await client.get(f"{EB_BASE}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


async def _fetch_events_stats(
    client: httpx.AsyncClient,
    api_key: str,
    start: str,
    end: str,
    campaign_ids: list[str],
) -> dict[str, int]:
    """Fetch and flatten `/campaign-events/stats` for a date range + ids.

    Returns an empty dict if `campaign_ids` is empty (EB rejects the call
    otherwise) or if the request fails — EB errors here are not fatal to
    the overall fetch.
    """
    if not campaign_ids:
        return {}
    # httpx encodes list params as repeated `key=` by default — use the
    # explicit `campaign_ids[]` form EmailBison expects.
    params: list[tuple[str, str]] = [("start_date", start), ("end_date", end)]
    for cid in campaign_ids:
        params.append(("campaign_ids[]", str(cid)))
    try:
        resp = await _eb_get(client, "/campaign-events/stats", api_key, params=params)
    except Exception as exc:  # noqa: BLE001
        logger.warning("EB events stats failed (%s-%s, %d campaigns): %s", start, end, len(campaign_ids), exc)
        return {}
    series = resp.get("data", []) if isinstance(resp, dict) else []
    return _parse_events_timeseries(series)


# ---------------------------------------------------------------------------
# Per-campaign today's stats
# ---------------------------------------------------------------------------

async def _fetch_campaign_today(
    client: httpx.AsyncClient,
    api_key: str,
    campaign: dict,
    today_str: str,
) -> dict:
    """Build a `CampaignData`-shaped dict for a single EB campaign."""
    cid = campaign.get("id", "")
    camp_today = await _fetch_events_stats(client, api_key, today_str, today_str, [str(cid)] if cid else [])

    camp_sent    = _safe_num(camp_today.get("sent"))
    camp_replies = _safe_num(camp_today.get("replied"))
    camp_opps    = _safe_num(camp_today.get("interested"))
    camp_bounced = _safe_num(camp_today.get("bounced"))

    return {
        "name":          campaign.get("name", "Unknown"),
        "id":            cid,
        "status":        (campaign.get("status") or "unknown").lower(),
        "sent_today":    camp_sent,
        "first_touch":   0,  # EmailBison doesn't expose first-touch vs follow-up
        "followups":     0,
        "replies_today": camp_replies,
        "opps_today":    camp_opps,
        "reply_rate":    round(camp_replies / camp_sent * 100, 2) if camp_sent > 0 else 0.0,
        "not_contacted": 0,  # Not available per-campaign in EB
        "in_progress":   0,
        "total_sent":    0,
        "total_bounced": camp_bounced,
    }


# ---------------------------------------------------------------------------
# Public fetcher
# ---------------------------------------------------------------------------

async def fetch_emailbison_data(
    client: httpx.AsyncClient,
    client_name: str,
    api_key: str,
) -> dict:
    """Fetch a monitoring snapshot for a single EmailBison workspace.

    Returns a dict matching the ClientData contract defined in
    `tests/fixtures/mock_monitoring_data.json`. On fatal error (e.g. the
    initial `/campaigns` call fails), raises — the caller (T6 cache layer)
    is responsible for converting the exception to a friendly error entry
    via `_friendly_error`.

    Args:
        client: Shared `httpx.AsyncClient` owned by the caller.
        client_name: Display name, used for logging + semaphore keying.
        api_key: EmailBison bearer token.
    """
    sem = _get_semaphore(client_name)

    async with sem:
        today = datetime.now(EASTERN).date()
        seven_ago = today - timedelta(days=7)
        today_str = today.isoformat()
        seven_ago_str = seven_ago.isoformat()

        # 1. Campaign list — no pagination, flat array under `data`.
        try:
            raw = await _eb_get(client, "/campaigns", api_key)
        except Exception as exc:  # noqa: BLE001
            # Let the caller translate to friendly_error; include message for context.
            raise RuntimeError(f"EB /campaigns failed for {client_name}: {exc}") from exc

        campaigns_all: list[dict] = raw.get("data", []) if isinstance(raw, dict) else []

        active_campaigns = [c for c in campaigns_all if (c.get("status") or "").lower() == "active"]
        all_cids:        list[str] = [str(c["id"]) for c in campaigns_all if c.get("id") is not None]
        active_cids:     list[str] = [str(c["id"]) for c in active_campaigns if c.get("id") is not None]

        # Stats prefer active ids; fall back to all ids if no active campaigns
        # (EB /campaign-events/stats requires at least one campaign_ids[]=).
        stats_cids = active_cids if active_cids else all_cids

    # 2. Parallel: 7d stats, today stats, not_contacted lookup
    # These three are independent and can run concurrently. They each
    # re-acquire the semaphore via the helper calls below — that's
    # intentional: we want to enforce the per-workspace rate limit on
    # every outbound request.
    async def _nc_lookup() -> int:
        async with sem:
            try:
                nc_resp = await _eb_get(
                    client,
                    "/leads",
                    api_key,
                    params={"filters[lead_campaign_status]": "never_contacted", "page": 1},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("EB /leads not_contacted lookup failed for %s: %s", client_name, exc)
                return 0
            meta = nc_resp.get("meta", {}) if isinstance(nc_resp, dict) else {}
            return _safe_num(meta.get("total"))

    async def _stats_7d_call() -> dict[str, int]:
        async with sem:
            return await _fetch_events_stats(client, api_key, seven_ago_str, today_str, stats_cids)

    async def _stats_today_call() -> dict[str, int]:
        async with sem:
            return await _fetch_events_stats(client, api_key, today_str, today_str, stats_cids)

    stats_7d, stats_today, not_contacted = await asyncio.gather(
        _stats_7d_call(),
        _stats_today_call(),
        _nc_lookup(),
    )

    # 3. Aggregate metrics
    sent_today    = _safe_num(stats_today.get("sent"))
    replies_today = _safe_num(stats_today.get("replied"))
    opps_today    = _safe_num(stats_today.get("interested"))

    # 7-day window excluding today → 6 days
    sent_7d    = _safe_num(stats_7d.get("sent"))    - sent_today
    replies_7d = _safe_num(stats_7d.get("replied")) - replies_today
    opps_7d    = _safe_num(stats_7d.get("interested")) - opps_today
    days_in_range = 6

    avg_sent_7d  = sent_7d / days_in_range if days_in_range > 0 else 0.0
    avg_reply_7d = replies_7d / days_in_range if days_in_range > 0 else 0.0
    avg_opps_7d  = opps_7d / days_in_range if days_in_range > 0 else 0.0

    reply_rate_today = (replies_today / sent_today * 100) if sent_today > 0 else 0.0
    reply_rate_7d    = (avg_reply_7d / avg_sent_7d * 100) if avg_sent_7d > 0 else 0.0

    bounced_7d    = _safe_num(stats_7d.get("bounced"))
    total_sent_7d = _safe_num(stats_7d.get("sent"))
    bounce_rate   = (bounced_7d / total_sent_7d * 100) if total_sent_7d > 0 else 0.0

    opp_trend   = _trend(opps_today, avg_opps_7d)
    reply_trend = _trend(reply_rate_today, reply_rate_7d)
    sent_trend  = _trend(sent_today, avg_sent_7d)

    # 4. Per-campaign today's stats (active + up to 5 recent non-active)
    active_camp_objs = [c for c in campaigns_all if (c.get("status") or "").lower() == "active"]
    other_camp_objs  = [c for c in campaigns_all if (c.get("status") or "").lower() != "active"][:5]
    campaigns_to_fetch = active_camp_objs + other_camp_objs

    async def _fetch_one(c: dict) -> dict:
        async with sem:
            return await _fetch_campaign_today(client, api_key, c, today_str)

    campaigns_list: list[dict] = []
    if campaigns_to_fetch:
        results = await asyncio.gather(
            *(_fetch_one(c) for c in campaigns_to_fetch),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.warning("EB per-campaign fetch failed for %s: %s", client_name, r)
                continue
            campaigns_list.append(r)

    return {
        "platform":          "emailbison",
        "active_campaigns":  len(active_campaigns),
        "total_campaigns":   len(campaigns_all),

        "sent_today":        sent_today,
        "first_touch_today": 0,
        "followup_today":    0,
        "replies_today":     replies_today,
        "opps_today":        opps_today,

        "reply_rate_today":  round(reply_rate_today, 2),
        "reply_rate_7d":     round(reply_rate_7d, 2),
        "bounce_rate":       round(bounce_rate, 2),

        "not_contacted":     not_contacted,
        "in_progress":       None,  # not exposed by EB

        "avg_sent_7d":       round(avg_sent_7d, 1),
        "avg_replies_7d":    round(avg_reply_7d, 1),
        "avg_opps_7d":       round(avg_opps_7d, 1),

        "opp_trend":         opp_trend,
        "reply_trend":       reply_trend,
        "sent_trend":        sent_trend,

        "campaigns":         campaigns_list,
        "daily":             [],
    }


# ---------------------------------------------------------------------------
# Test helper
# ---------------------------------------------------------------------------

def _reset_semaphores_for_tests() -> None:
    """Clear the per-workspace semaphore map. Test-only."""
    _monitoring_eb_semaphores.clear()


# Re-export friendly_error for convenience so the cache layer can import it
# from either api module without having to know which one generated the error.
__all__ = [
    "EB_BASE",
    "EASTERN",
    "fetch_emailbison_data",
    "_parse_events_timeseries",
    "_friendly_error",
    "_reset_semaphores_for_tests",
]
