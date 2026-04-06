"""Tests for the background discovery poller and manual QA triggers.

Tests verify:
- discovery_poll updates campaign cache per workspace (OPS-04)
- error isolation: one failing workspace doesn't block others (OPS-05)
- last_refresh timestamp updated after poll (OPS-06)
- trigger_qa_all/workspace/campaign return immediately (OPS-01, OPS-02, OPS-03)
- deduplication of concurrent scans for the same scope (D-18)
- completed tasks are cleaned from _running_scans
"""
import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.api.instantly import INSTANTLY_BASE


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_campaign(campaign_id: str) -> dict:
    return {"id": campaign_id, "name": f"Campaign {campaign_id}", "status": 1, "sequences": []}


WORKSPACE_LIST = [
    {"name": "ws1", "key_preview": "...ey1"},
    {"name": "ws2", "key_preview": "...ey2"},
]


def _api_key_side_effect(name: str) -> str | None:
    """Simulates get_api_key returning a key for known workspaces."""
    mapping = {"ws1": "key-ws1", "ws2": "key-ws2"}
    return mapping.get(name)


# ---------------------------------------------------------------------------
# discovery_poll tests
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_discovery_poll_updates_cache():
    """After discovery_poll(), cache has campaign lists for all workspaces."""
    campaigns_ws1 = {"items": [_make_campaign("c1"), _make_campaign("c2")], "next_starting_after": None}
    campaigns_ws2 = {"items": [_make_campaign("c3")], "next_starting_after": None}

    # Mock Instantly API
    call_count = {"ws1": 0, "ws2": 0}

    def campaign_side_effect(request):
        auth_header = request.headers.get("Authorization", "")
        if "key-ws1" in auth_header:
            call_count["ws1"] += 1
            return httpx.Response(200, json=campaigns_ws1)
        elif "key-ws2" in auth_header:
            call_count["ws2"] += 1
            return httpx.Response(200, json=campaigns_ws2)
        return httpx.Response(401)

    respx.get(f"{INSTANTLY_BASE}/campaigns").mock(side_effect=campaign_side_effect)

    from app.services.cache import QACache

    fresh_cache = QACache()

    with (
        patch("app.services.poller.list_workspaces", return_value=WORKSPACE_LIST),
        patch("app.services.poller.get_api_key", side_effect=_api_key_side_effect),
        patch("app.services.poller.get_cache", return_value=fresh_cache),
    ):
        from app.services import poller
        await poller.discovery_poll()

    ws1_campaigns = await fresh_cache.get_campaigns("ws1")
    ws2_campaigns = await fresh_cache.get_campaigns("ws2")

    assert len(ws1_campaigns) == 2
    assert len(ws2_campaigns) == 1
    assert {c["id"] for c in ws1_campaigns} == {"c1", "c2"}
    assert ws2_campaigns[0]["id"] == "c3"


@respx.mock
@pytest.mark.asyncio
async def test_discovery_poll_error_isolation():
    """When ws1 raises, ws2 still completes and its campaigns are cached (OPS-05)."""
    campaigns_ws2 = {"items": [_make_campaign("c-ws2")], "next_starting_after": None}

    def campaign_side_effect(request):
        auth_header = request.headers.get("Authorization", "")
        if "key-ws1" in auth_header:
            raise httpx.ConnectError("Connection refused")
        return httpx.Response(200, json=campaigns_ws2)

    respx.get(f"{INSTANTLY_BASE}/campaigns").mock(side_effect=campaign_side_effect)

    from app.services.cache import QACache

    fresh_cache = QACache()

    with (
        patch("app.services.poller.list_workspaces", return_value=WORKSPACE_LIST),
        patch("app.services.poller.get_api_key", side_effect=_api_key_side_effect),
        patch("app.services.poller.get_cache", return_value=fresh_cache),
    ):
        from app.services import poller
        # Should NOT raise even though ws1 fails
        await poller.discovery_poll()

    ws2_campaigns = await fresh_cache.get_campaigns("ws2")
    assert len(ws2_campaigns) == 1
    assert ws2_campaigns[0]["id"] == "c-ws2"


@pytest.mark.asyncio
async def test_discovery_poll_updates_last_refresh():
    """After discovery_poll(), get_all().last_refresh is set (OPS-06)."""
    from app.services.cache import QACache

    fresh_cache = QACache()

    # Confirm it's None before poll
    result_before = await fresh_cache.get_all()
    assert result_before.last_refresh is None

    with (
        patch("app.services.poller.list_workspaces", return_value=[{"name": "ws1", "key_preview": "...ey1"}]),
        patch("app.services.poller.get_api_key", return_value="key-ws1"),
        patch("app.services.poller.get_cache", return_value=fresh_cache),
        patch("app.api.instantly.list_campaigns", new_callable=AsyncMock, return_value=[]),
    ):
        from app.services import poller
        await poller.discovery_poll()

    result_after = await fresh_cache.get_all()
    assert result_after.last_refresh is not None


@pytest.mark.asyncio
async def test_discovery_poll_no_workspaces():
    """With empty workspace registry, discovery_poll() returns without error or side effects."""
    from app.services.cache import QACache

    fresh_cache = QACache()

    with (
        patch("app.services.poller.list_workspaces", return_value=[]),
        patch("app.services.poller.get_cache", return_value=fresh_cache),
    ):
        from app.services import poller
        # Should not raise
        await poller.discovery_poll()

    result = await fresh_cache.get_all()
    # last_refresh stays None when no workspaces
    assert result.last_refresh is None


# ---------------------------------------------------------------------------
# trigger_qa_all tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_qa_all_returns_immediately():
    """trigger_qa_all() returns {status: started, workspaces_triggered: N} without awaiting QA."""
    slow_qa = AsyncMock(side_effect=lambda *args, **kwargs: asyncio.sleep(100))

    with (
        patch("app.services.poller.list_workspaces", return_value=WORKSPACE_LIST),
        patch("app.services.poller.get_api_key", side_effect=_api_key_side_effect),
        patch("app.services.poller.run_workspace_qa", new=slow_qa),
    ):
        from app.services import poller
        # Clear any leftover tasks from prior tests
        poller._running_scans.clear()

        result = await poller.trigger_qa_all()

    assert result["status"] == "started"
    assert result["workspaces_triggered"] == 2

    # Cleanup: cancel background tasks to avoid warnings
    for task in list(poller._running_scans.values()):
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    poller._running_scans.clear()


# ---------------------------------------------------------------------------
# trigger_qa_workspace tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_qa_workspace_returns_immediately():
    """trigger_qa_workspace() returns immediately with status=started."""
    slow_qa = AsyncMock(side_effect=lambda *args, **kwargs: asyncio.sleep(100))

    with (
        patch("app.services.poller.get_api_key", return_value="key-ws1"),
        patch("app.services.poller.run_workspace_qa", new=slow_qa),
    ):
        from app.services import poller
        poller._running_scans.clear()

        result = await poller.trigger_qa_workspace("ws1")

    assert result["status"] == "started"
    assert "workspace:ws1" in result["scope"]

    # Cleanup
    for task in list(poller._running_scans.values()):
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    poller._running_scans.clear()


# ---------------------------------------------------------------------------
# trigger_qa_campaign tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_qa_campaign_returns_immediately():
    """trigger_qa_campaign() returns immediately with status=started."""
    slow_qa = AsyncMock(side_effect=lambda *args, **kwargs: asyncio.sleep(100))
    campaign = _make_campaign("camp-1")

    with (
        patch("app.services.poller.get_api_key", return_value="key-ws1"),
        patch("app.services.poller.run_campaign_qa", new=slow_qa),
    ):
        from app.services import poller
        poller._running_scans.clear()

        result = await poller.trigger_qa_campaign("camp-1", campaign, "ws1")

    assert result["status"] == "started"
    assert "campaign:camp-1" in result["scope"]

    # Cleanup
    for task in list(poller._running_scans.values()):
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    poller._running_scans.clear()


# ---------------------------------------------------------------------------
# Deduplication tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_deduplication():
    """Second trigger_qa_workspace call while first is running returns already_running."""
    # Use a real asyncio.Event to control when the background task finishes
    finish_event = asyncio.Event()

    async def slow_qa_job(ws_name: str) -> None:
        await finish_event.wait()

    with (
        patch("app.services.poller.get_api_key", return_value="key-ws1"),
        patch("app.services.poller._run_workspace_qa_job", side_effect=slow_qa_job),
    ):
        from app.services import poller
        poller._running_scans.clear()

        result1 = await poller.trigger_qa_workspace("ws1")
        # Yield control to let the asyncio task start
        await asyncio.sleep(0)

        result2 = await poller.trigger_qa_workspace("ws1")

    assert result1["status"] == "started"
    assert result2["status"] == "already_running"

    # Cleanup: unblock and cancel
    finish_event.set()
    for task in list(poller._running_scans.values()):
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    poller._running_scans.clear()


@pytest.mark.asyncio
async def test_trigger_cleanup_done_tasks():
    """Completed tasks are removed from _running_scans on subsequent trigger calls."""
    call_count = {"n": 0}

    async def instant_qa_job(ws_name: str) -> None:
        call_count["n"] += 1
        # Finishes immediately

    with (
        patch("app.services.poller.get_api_key", return_value="key-ws1"),
        patch("app.services.poller._run_workspace_qa_job", side_effect=instant_qa_job),
    ):
        from app.services import poller
        poller._running_scans.clear()

        # First trigger — task starts and completes
        await poller.trigger_qa_workspace("ws1")
        # Let task run to completion
        await asyncio.sleep(0.05)

        # Second trigger — prune should run; task should start again (not deduplicated)
        result2 = await poller.trigger_qa_workspace("ws1")
        # Yield to let the new background task execute
        await asyncio.sleep(0.05)

    assert result2["status"] == "started"
    assert call_count["n"] == 2  # Both triggers actually ran QA

    # Cleanup
    for task in list(poller._running_scans.values()):
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    poller._running_scans.clear()
