"""Async Instantly v2 API client.

Provides:
  - list_campaigns: fetch all draft/active campaigns with cursor pagination
  - fetch_all_leads: fetch all active leads for a campaign with cursor pagination
  - extract_copy_from_campaign: extract subject+body variants from inline sequences
"""
import asyncio

import httpx

INSTANTLY_BASE = "https://api.instantly.ai/api/v2"

# Per-workspace semaphores — Semaphore(5) limits concurrent requests per workspace
_semaphores: dict[str, asyncio.Semaphore] = {}


def _get_semaphore(workspace_name: str) -> asyncio.Semaphore:
    """Return (creating if needed) the rate-limit semaphore for a workspace."""
    if workspace_name not in _semaphores:
        _semaphores[workspace_name] = asyncio.Semaphore(5)
    return _semaphores[workspace_name]


async def list_campaigns(
    client: httpx.AsyncClient,
    api_key: str,
    workspace_name: str = "default",
) -> list[dict]:
    """Fetch all draft (status=0) and active (status=1) campaigns from a workspace.

    Uses cursor pagination: loops until next_starting_after is None or page
    is smaller than the requested limit.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    all_campaigns: list[dict] = []
    params: dict = {"limit": 100}
    sem = _get_semaphore(workspace_name)

    while True:
        async with sem:
            response = await client.get(
                f"{INSTANTLY_BASE}/campaigns",
                headers=headers,
                params=params,
            )
        response.raise_for_status()
        data = response.json()

        items: list[dict] = data.get("items", [])
        all_campaigns.extend(items)

        cursor = data.get("next_starting_after")
        if not cursor:
            break

        params = {"limit": 100, "starting_after": cursor}
        await asyncio.sleep(0.1)

    # Only draft and active campaigns — paused (2) and completed (3) are excluded
    return [c for c in all_campaigns if c.get("status") in (0, 1)]


async def fetch_all_leads(
    client: httpx.AsyncClient,
    api_key: str,
    campaign_id: str,
    workspace_name: str = "default",
) -> list[dict]:
    """Fetch all active (status=1) leads for a campaign.

    Uses POST /leads/list with cursor pagination.
    # Lead variables are in lead["payload"] — verified 2026-04-04
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    all_leads: list[dict] = []
    body: dict = {"campaign": campaign_id, "limit": 100}
    sem = _get_semaphore(workspace_name)

    while True:
        async with sem:
            response = await client.post(
                f"{INSTANTLY_BASE}/leads/list",
                headers=headers,
                json=body,
            )
        response.raise_for_status()
        data = response.json()

        items: list[dict] = data.get("items", [])
        all_leads.extend(items)

        cursor = data.get("next_starting_after")
        if not cursor:
            break

        body = {"campaign": campaign_id, "limit": 100, "starting_after": cursor}
        await asyncio.sleep(0.1)

    # Only active leads — contacted (3) and bounced/error (-1) are excluded
    return [lead for lead in all_leads if lead.get("status") == 1]


def extract_copy_from_campaign(campaign: dict) -> list[dict]:
    """Extract all subject+body variants from a campaign's inline sequences.

    Iterates campaign["sequences"][*]["steps"][*]["variants"] and returns
    a flat list of {"subject": str, "body": str} dicts.

    No additional API call — copy is inline in the campaign response (per API-03).
    """
    variants: list[dict] = []
    for sequence in campaign.get("sequences", []):
        for step in sequence.get("steps", []):
            for variant in step.get("variants", []):
                variants.append(
                    {
                        "subject": variant.get("subject", ""),
                        "body": variant.get("body", ""),
                    }
                )
    return variants
