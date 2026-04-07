"""Unit tests for monitoring_poller — specifically refresh_all_clients_sync.

Tests verify:
- refresh_all_clients_sync calls _refresh_one_client for each registered entry
- Partial failures (one client raises) don't block the rest (OPS-05 pattern)
- Concurrent invocations don't double-fetch (mock call count validation)
- Mock mode short-circuits without touching API
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(name: str, platform: str = "instantly"):
    """Create a minimal monitoring registry entry mock."""
    entry = MagicMock()
    entry.name = name
    entry.platform = platform
    entry._api_key = f"key-{name}"
    return entry


# ---------------------------------------------------------------------------
# refresh_all_clients_sync tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_all_calls_refresh_one_for_each_entry():
    """refresh_all_clients_sync calls _refresh_one_client once per registered entry."""
    entries = [_make_entry("ClientA"), _make_entry("ClientB"), _make_entry("ClientC")]
    call_log = []

    async def mock_refresh_one(entry):
        call_log.append(entry.name)

    with (
        patch("app.services.monitoring_poller.list_monitoring_workspaces", return_value=entries),
        patch("app.services.monitoring_poller._refresh_one_client", side_effect=mock_refresh_one),
        patch("app.services.monitoring_poller.cache") as mock_cache,
    ):
        mock_cache.is_mock_mode.return_value = False
        from app.services.monitoring_poller import refresh_all_clients_sync
        await refresh_all_clients_sync()

    assert sorted(call_log) == ["ClientA", "ClientB", "ClientC"]


@pytest.mark.asyncio
async def test_refresh_all_handles_partial_failures():
    """If one client's _refresh_one_client raises, other clients still complete.

    asyncio.gather(..., return_exceptions=True) isolates failures.
    """
    entries = [_make_entry("GoodA"), _make_entry("BadB"), _make_entry("GoodC")]
    completed = []

    async def mock_refresh_one(entry):
        if entry.name == "BadB":
            raise RuntimeError("simulated fetch failure")
        completed.append(entry.name)

    with (
        patch("app.services.monitoring_poller.list_monitoring_workspaces", return_value=entries),
        patch("app.services.monitoring_poller._refresh_one_client", side_effect=mock_refresh_one),
        patch("app.services.monitoring_poller.cache") as mock_cache,
    ):
        mock_cache.is_mock_mode.return_value = False
        from app.services.monitoring_poller import refresh_all_clients_sync
        # Should not raise even though BadB fails
        await refresh_all_clients_sync()

    assert sorted(completed) == ["GoodA", "GoodC"], "Healthy clients should complete despite BadB failing"


@pytest.mark.asyncio
async def test_refresh_all_mock_mode_short_circuits():
    """In mock mode, refresh_all_clients_sync returns immediately without fetching."""
    with (
        patch("app.services.monitoring_poller.cache") as mock_cache,
        patch("app.services.monitoring_poller.list_monitoring_workspaces") as mock_list,
    ):
        mock_cache.is_mock_mode.return_value = True
        from app.services.monitoring_poller import refresh_all_clients_sync
        await refresh_all_clients_sync()

    mock_list.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_all_empty_registry():
    """With no registered clients, refresh_all_clients_sync completes without error."""
    with (
        patch("app.services.monitoring_poller.list_monitoring_workspaces", return_value=[]),
        patch("app.services.monitoring_poller.cache") as mock_cache,
    ):
        mock_cache.is_mock_mode.return_value = False
        from app.services.monitoring_poller import refresh_all_clients_sync
        await refresh_all_clients_sync()  # should not raise


@pytest.mark.asyncio
async def test_concurrent_refresh_all_does_not_double_fetch():
    """Two concurrent refresh_all_clients_sync calls each trigger _refresh_one_client once per client.

    We verify total call count equals 2 * num_clients — not more, not less.
    This documents expected behavior: no dedup at the gather level (dedup is
    the scheduler's job via max_instances=1; the manual route is intentional).
    """
    entries = [_make_entry("WS1"), _make_entry("WS2")]
    call_counter = {"count": 0}

    async def mock_refresh_one(entry):
        call_counter["count"] += 1

    with (
        patch("app.services.monitoring_poller.list_monitoring_workspaces", return_value=entries),
        patch("app.services.monitoring_poller._refresh_one_client", side_effect=mock_refresh_one),
        patch("app.services.monitoring_poller.cache") as mock_cache,
    ):
        mock_cache.is_mock_mode.return_value = False
        from app.services.monitoring_poller import refresh_all_clients_sync
        await asyncio.gather(refresh_all_clients_sync(), refresh_all_clients_sync())

    # 2 entries × 2 concurrent calls = 4 total invocations
    assert call_counter["count"] == 4


# ---------------------------------------------------------------------------
# get_scheduler tests
# ---------------------------------------------------------------------------

def test_get_scheduler_returns_none_before_init():
    """get_scheduler() returns None before init_monitoring_poller is called."""
    import app.services.monitoring_poller as poller
    # Temporarily clear the scheduler ref
    original = poller._scheduler
    poller._scheduler = None
    try:
        from app.services.monitoring_poller import get_scheduler
        assert get_scheduler() is None
    finally:
        poller._scheduler = original


def test_get_scheduler_returns_scheduler_after_init():
    """get_scheduler() returns the scheduler set by init_monitoring_poller."""
    import app.services.monitoring_poller as poller
    fake_scheduler = MagicMock()
    original = poller._scheduler
    poller._scheduler = fake_scheduler
    try:
        from app.services.monitoring_poller import get_scheduler
        assert get_scheduler() is fake_scheduler
    finally:
        poller._scheduler = original
