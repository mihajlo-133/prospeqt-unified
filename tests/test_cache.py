"""Tests for the QACache in-memory result store.

Tests verify:
- set/get per workspace with correct field values
- aggregation in get_all() across multiple workspaces
- per-workspace error tracking
- campaign list storage (discovery poll results)
- singleton pattern via get_cache()
"""
from datetime import datetime, timezone

import pytest

from app.models.qa import CampaignQAResult, WorkspaceQAResult


def make_campaign(campaign_id: str, broken_count: int = 0, total_leads: int = 10) -> CampaignQAResult:
    """Build a CampaignQAResult fixture."""
    return CampaignQAResult(
        campaign_id=campaign_id,
        campaign_name=f"Campaign {campaign_id}",
        total_leads=total_leads,
        broken_count=broken_count,
        issues_by_variable={"firstName": broken_count} if broken_count else {},
    )


def make_workspace(name: str, broken_count: int = 0, campaign_count: int = 1) -> WorkspaceQAResult:
    """Build a WorkspaceQAResult fixture."""
    campaigns = [make_campaign(f"c{i}", broken_count // campaign_count) for i in range(campaign_count)]
    return WorkspaceQAResult(
        workspace_name=name,
        campaigns=campaigns,
        total_broken=broken_count,
    )


# ---------------------------------------------------------------------------
# Basic set/get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_set_and_get_workspace():
    """set_workspace stores result; get_workspace returns it with correct fields."""
    from app.services.cache import QACache

    cache = QACache()
    result = make_workspace("ws1", broken_count=5)
    await cache.set_workspace("ws1", result)

    fetched = await cache.get_workspace("ws1")
    assert fetched is not None
    assert fetched.workspace_name == "ws1"
    assert fetched.total_broken == 5
    assert len(fetched.campaigns) == 1


@pytest.mark.asyncio
async def test_cache_get_workspace_not_found():
    """get_workspace returns None for a workspace that was never set."""
    from app.services.cache import QACache

    cache = QACache()
    result = await cache.get_workspace("nonexistent")
    assert result is None


# ---------------------------------------------------------------------------
# Aggregation via get_all()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_get_all_aggregates():
    """get_all() returns GlobalQAResult with total_broken and total_campaigns_checked summed."""
    from app.services.cache import QACache

    cache = QACache()
    ws1 = WorkspaceQAResult(
        workspace_name="ws1",
        campaigns=[
            CampaignQAResult(campaign_id="c1", campaign_name="Camp 1", total_leads=100, broken_count=5,
                             issues_by_variable={"firstName": 5})
        ],
        total_broken=5,
    )
    ws2 = WorkspaceQAResult(
        workspace_name="ws2",
        campaigns=[
            CampaignQAResult(campaign_id="c2", campaign_name="Camp 2", total_leads=50, broken_count=3,
                             issues_by_variable={"cityName": 3}),
            CampaignQAResult(campaign_id="c3", campaign_name="Camp 3", total_leads=80, broken_count=2,
                             issues_by_variable={"companyName": 2}),
        ],
        total_broken=5,
    )
    await cache.set_workspace("ws1", ws1)
    await cache.set_workspace("ws2", ws2)

    global_result = await cache.get_all()

    assert global_result.total_broken == 10  # 5 + 5
    assert global_result.total_campaigns_checked == 3  # 1 + 2
    assert len(global_result.workspaces) == 2


@pytest.mark.asyncio
async def test_cache_get_all_empty():
    """get_all() on fresh cache returns GlobalQAResult with zeros and no workspaces."""
    from app.services.cache import QACache

    cache = QACache()
    result = await cache.get_all()

    assert result.total_broken == 0
    assert result.total_campaigns_checked == 0
    assert result.workspaces == []
    assert result.errors == {}
    assert result.last_refresh is None


# ---------------------------------------------------------------------------
# Per-workspace error tracking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_set_workspace_error():
    """set_workspace_error records error per workspace; get_all() surfaces it in errors dict."""
    from app.services.cache import QACache

    cache = QACache()
    await cache.set_workspace_error("ws1", "timeout after 30s")

    result = await cache.get_all()
    assert result.errors == {"ws1": "timeout after 30s"}


@pytest.mark.asyncio
async def test_cache_set_workspace_clears_error():
    """After set_workspace_error, setting a real result removes the error entry."""
    from app.services.cache import QACache

    cache = QACache()
    await cache.set_workspace_error("ws1", "timeout")

    # Now provide a real result — error should be cleared
    result = make_workspace("ws1", broken_count=2)
    await cache.set_workspace(name="ws1", result=result)

    global_result = await cache.get_all()
    assert "ws1" not in global_result.errors


# ---------------------------------------------------------------------------
# Last refresh timestamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_last_refresh():
    """set_last_refresh stores timestamp; get_all().last_refresh returns it."""
    from app.services.cache import QACache

    cache = QACache()
    ts = datetime(2026, 4, 4, 12, 0, 0, tzinfo=timezone.utc)
    await cache.set_last_refresh(ts)

    result = await cache.get_all()
    assert result.last_refresh == ts


# ---------------------------------------------------------------------------
# Campaign list storage (discovery poll namespace)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_set_campaigns():
    """set_campaigns stores campaign dicts; get_campaigns returns them."""
    from app.services.cache import QACache

    cache = QACache()
    campaigns = [
        {"id": "c1", "name": "Campaign 1", "status": 1},
        {"id": "c2", "name": "Campaign 2", "status": 0},
    ]
    await cache.set_campaigns("ws1", campaigns)

    fetched = await cache.get_campaigns("ws1")
    assert len(fetched) == 2
    assert fetched[0]["id"] == "c1"
    assert fetched[1]["id"] == "c2"


@pytest.mark.asyncio
async def test_cache_get_campaigns_empty():
    """get_campaigns returns empty list for workspace with no cached campaigns."""
    from app.services.cache import QACache

    cache = QACache()
    result = await cache.get_campaigns("never-set")
    assert result == []


# ---------------------------------------------------------------------------
# Singleton pattern
# ---------------------------------------------------------------------------


def test_get_cache_returns_singleton():
    """get_cache() always returns the same QACache instance."""
    from app.services.cache import get_cache

    instance_a = get_cache()
    instance_b = get_cache()
    assert instance_a is instance_b
