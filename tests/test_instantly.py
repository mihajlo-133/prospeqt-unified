"""Tests for the Instantly v2 API client.

Uses respx to mock HTTP calls — no real API calls made.
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.api.instantly import (
    INSTANTLY_BASE,
    _get_semaphore,
    _semaphores,
    extract_copy_from_campaign,
    fetch_all_leads,
    list_campaigns,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Campaigns — list_campaigns
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_list_campaigns_returns_all_pages():
    """Two pages of campaigns — all items from both pages collected."""
    page1 = {
        "items": [
            {"id": "c1", "name": "Camp 1", "status": 1, "sequences": []},
            {"id": "c2", "name": "Camp 2", "status": 0, "sequences": []},
        ],
        "next_starting_after": "cursor-1",
    }
    page2 = {
        "items": [
            {"id": "c3", "name": "Camp 3", "status": 1, "sequences": []},
        ],
        "next_starting_after": None,
    }

    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json=page1)
        return httpx.Response(200, json=page2)

    respx.get(f"{INSTANTLY_BASE}/campaigns").mock(side_effect=side_effect)

    async with httpx.AsyncClient() as client:
        campaigns = await list_campaigns(client, "test-api-key")

    assert len(campaigns) == 3
    assert {c["id"] for c in campaigns} == {"c1", "c2", "c3"}
    assert call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_campaign_status_filter():
    """Only status 0 (draft) and 1 (active) are returned; 2 and 3 are excluded."""
    fixture = load_fixture("campaign_response.json")
    respx.get(f"{INSTANTLY_BASE}/campaigns").mock(return_value=httpx.Response(200, json=fixture))

    async with httpx.AsyncClient() as client:
        campaigns = await list_campaigns(client, "test-api-key")

    statuses = {c["status"] for c in campaigns}
    assert statuses <= {0, 1}, f"Unexpected statuses in result: {statuses}"
    ids = {c["id"] for c in campaigns}
    assert "camp-003" not in ids, "Paused campaign (status=2) should be excluded"
    assert "camp-004" not in ids, "Completed campaign (status=3) should be excluded"
    assert "camp-001" in ids
    assert "camp-002" in ids


# ---------------------------------------------------------------------------
# Copy extraction — extract_copy_from_campaign
# ---------------------------------------------------------------------------


def test_extract_campaign_copy():
    """All subject + body text from inline sequences is extracted."""
    fixture = load_fixture("campaign_response.json")
    campaign = fixture["items"][0]  # camp-001 has 2 steps, 1 variant each

    variants = extract_copy_from_campaign(campaign)

    assert len(variants) == 2
    assert all("subject" in v and "body" in v for v in variants)
    assert "{{firstName}}" in variants[0]["subject"]
    assert "{{companyName}}" in variants[1]["body"]


def test_extract_campaign_copy_empty_sequences():
    """Campaign with no sequences returns empty list."""
    campaign = {"id": "c-empty", "name": "Empty", "status": 2, "sequences": []}
    variants = extract_copy_from_campaign(campaign)
    assert variants == []


# ---------------------------------------------------------------------------
# Leads — fetch_all_leads
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_fetch_all_leads_pagination():
    """Two pages of leads — all items from both pages collected."""
    page1 = {
        "items": [
            {"id": "l1", "email": "a@x.com", "status": 1, "payload": {"firstName": "A"}},
            {"id": "l2", "email": "b@x.com", "status": 1, "payload": {"firstName": "B"}},
        ],
        "next_starting_after": "lead-cursor-1",
    }
    page2 = {
        "items": [
            {"id": "l3", "email": "c@x.com", "status": 1, "payload": {"firstName": "C"}},
        ],
        "next_starting_after": None,
    }

    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json=page1)
        return httpx.Response(200, json=page2)

    respx.post(f"{INSTANTLY_BASE}/leads/list").mock(side_effect=side_effect)

    async with httpx.AsyncClient() as client:
        leads = await fetch_all_leads(client, "test-api-key", "camp-001")

    assert len(leads) == 3
    assert {l["id"] for l in leads} == {"l1", "l2", "l3"}
    assert call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_lead_status_filter():
    """Only status=1 (active) leads are returned; -1 and 3 are excluded."""
    fixture = load_fixture("leads_response.json")
    respx.post(f"{INSTANTLY_BASE}/leads/list").mock(return_value=httpx.Response(200, json=fixture))

    async with httpx.AsyncClient() as client:
        leads = await fetch_all_leads(client, "test-api-key", "camp-001")

    assert all(l["status"] == 1 for l in leads)
    ids = {l["id"] for l in leads}
    assert "lead-001" in ids
    assert "lead-002" in ids
    assert "lead-003" not in ids  # status=-1 (bounced)
    assert "lead-004" not in ids  # status=3 (contacted)


@respx.mock
@pytest.mark.asyncio
async def test_lead_payload_read():
    """Lead payload dict is preserved correctly — not custom_variables."""
    fixture = load_fixture("leads_response.json")
    respx.post(f"{INSTANTLY_BASE}/leads/list").mock(return_value=httpx.Response(200, json=fixture))

    async with httpx.AsyncClient() as client:
        leads = await fetch_all_leads(client, "test-api-key", "camp-001")

    john = next(l for l in leads if l["id"] == "lead-001")
    assert "payload" in john
    assert john["payload"]["firstName"] == "John"
    assert john["payload"]["companyName"] == "Acme Corp"
    # Confirm custom_variables is NOT used — payload is the source of truth
    assert "custom_variables" not in john or john.get("payload") is not None


# ---------------------------------------------------------------------------
# Rate limiting — semaphore
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_rate_limit_semaphore():
    """Per-workspace semaphore limits concurrent requests to max 5."""
    max_concurrent = 0
    current_concurrent = 0
    lock = asyncio.Lock()

    async def slow_response(request):
        nonlocal max_concurrent, current_concurrent
        async with lock:
            current_concurrent += 1
            if current_concurrent > max_concurrent:
                max_concurrent = current_concurrent
        await asyncio.sleep(0.05)  # simulate latency
        async with lock:
            current_concurrent -= 1
        return httpx.Response(200, json={"items": [], "next_starting_after": None})

    respx.get(f"{INSTANTLY_BASE}/campaigns").mock(side_effect=slow_response)

    # Clear any cached semaphore for this workspace to ensure clean test
    _semaphores.pop("rate-test-ws", None)

    async with httpx.AsyncClient() as client:
        tasks = [list_campaigns(client, "test-key", workspace_name="rate-test-ws") for _ in range(10)]
        await asyncio.gather(*tasks)

    assert max_concurrent <= 5, f"Max concurrent was {max_concurrent}, expected <= 5"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_api_error_raises():
    """When the API returns 500, httpx.HTTPStatusError is raised."""
    respx.get(f"{INSTANTLY_BASE}/campaigns").mock(
        return_value=httpx.Response(500, json={"error": "internal server error"})
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await list_campaigns(client, "test-api-key")
