"""Smoke tests for dashboard and health routes."""
import pytest
from httpx import ASGITransport, AsyncClient


async def test_health_check(mock_env):
    from app.main import create_app
    from app.services import workspace as ws_module

    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_dashboard_returns_html(mock_env):
    from app.main import create_app
    from app.services import workspace as ws_module

    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


async def test_nonexistent_route_returns_404(mock_env):
    from app.main import create_app
    from app.services import workspace as ws_module

    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/nonexistent")
    assert response.status_code == 404


async def test_overview_page_has_workspace_grid(mock_env):
    """GET / returns HTML containing workspace-grid element. Per VIEW-01."""
    from app.main import create_app
    from app.services import workspace as ws_module
    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert "workspace-grid" in response.text


async def test_overview_page_shows_empty_state(mock_env):
    """GET / with no QA data shows empty state message. Per VIEW-01."""
    from app.main import create_app
    from app.services import workspace as ws_module
    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == 200
    # Either workspace cards or empty state should be present
    assert "workspace-grid" in response.text


async def test_workspace_page_returns_html(mock_env):
    """GET /ws/{name} returns HTML with breadcrumb. Per VIEW-02, VIEW-07."""
    from app.main import create_app
    from app.services import workspace as ws_module
    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ws/TestClient")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "All Workspaces" in response.text
    assert "TestClient" in response.text


async def test_workspace_page_has_campaign_table(mock_env):
    """GET /ws/{name} returns HTML with campaign-table element. Per VIEW-02."""
    from app.main import create_app
    from app.services import workspace as ws_module
    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ws/TestClient")
    assert response.status_code == 200
    assert "campaign-table" in response.text


async def test_scan_all_endpoint(mock_env):
    """POST /api/scan/all triggers scan and redirects to overview. Per OPS-01."""
    from app.main import create_app
    from app.services import workspace as ws_module
    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/scan/all")
    assert response.status_code == 200
    assert response.headers.get("hx-redirect") == "/"


async def test_health_class_thresholds():
    """health_class returns correct color based on percentage thresholds. Per VIEW-06, D-08/D-09."""
    from app.routes.dashboard import health_class
    assert health_class(0, 100) == "green"      # 0% < 2%
    assert health_class(1, 100) == "green"      # 1% < 2%
    assert health_class(2, 100) == "yellow"     # 2% in 2-10%
    assert health_class(10, 100) == "yellow"    # 10% in 2-10%
    assert health_class(11, 100) == "red"       # 11% > 10%
    assert health_class(50, 100) == "red"       # 50% > 10%
    assert health_class(0, 0) == "green"        # no leads = clean


async def test_freshness_class_thresholds():
    """freshness_class returns correct color based on age. Per D-18."""
    from datetime import datetime, timezone, timedelta
    from app.routes.dashboard import freshness_class
    now = datetime.now(timezone.utc)
    assert freshness_class(None) == "gray"
    assert freshness_class(now) == "green"                                    # just now
    assert freshness_class(now - timedelta(minutes=3)) == "green"            # 3 min < 5
    assert freshness_class(now - timedelta(minutes=10)) == "amber"           # 10 min in 5-15
    assert freshness_class(now - timedelta(minutes=20)) == "gray"            # 20 min > 15


async def test_overview_has_scan_all_button(mock_env):
    """GET / has Scan All in the topbar (from base.html). Per D-07."""
    from app.main import create_app
    from app.services import workspace as ws_module
    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")
    assert "Scan All" in response.text


async def test_mobile_meta_tag(mock_env):
    """Pages include viewport meta tag for responsive layout. Per UX-03."""
    from app.main import create_app
    from app.services import workspace as ws_module
    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")
    assert 'name="viewport"' in response.text


async def test_campaign_page_returns_html(mock_env):
    """GET /ws/{name}/campaign/{id} returns HTML with breadcrumb. Per VIEW-03, VIEW-07."""
    from app.main import create_app
    from app.services import workspace as ws_module
    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ws/TestClient/campaign/camp-001")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "All Workspaces" in response.text
    assert "TestClient" in response.text


async def test_campaign_page_has_results_container(mock_env):
    """GET /ws/{name}/campaign/{id} has campaign-results element. Per VIEW-03."""
    from app.main import create_app
    from app.services import workspace as ws_module
    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ws/TestClient/campaign/camp-001")
    assert response.status_code == 200
    assert "campaign-results" in response.text


async def test_campaign_page_not_scanned_state(mock_env):
    """Campaign page shows not-scanned state when no QA data exists. Per VIEW-03."""
    from app.main import create_app
    from app.services import workspace as ws_module
    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ws/TestClient/campaign/nonexistent")
    assert response.status_code == 200
    assert "Not yet scanned" in response.text


async def test_campaign_page_with_data(mock_env):
    """Campaign page shows variable summary and broken leads when data exists. Per VIEW-03, VIEW-04."""
    from datetime import datetime, timezone
    from app.main import create_app
    from app.models.qa import BrokenLeadDetail, CampaignQAResult, WorkspaceQAResult
    from app.services import workspace as ws_module
    from app.services.cache import get_cache

    ws_module.load_from_env()
    app = create_app()

    # Populate cache with test data
    bl = BrokenLeadDetail(email="broken@test.com", lead_status=1, broken_vars={"cityName": ""})
    campaign_result = CampaignQAResult(
        campaign_id="camp-test-001",
        campaign_name="Test Campaign",
        total_leads=10,
        broken_count=1,
        issues_by_variable={"cityName": 1},
        broken_leads=[bl],
        last_checked=datetime.now(timezone.utc),
    )
    ws_result = WorkspaceQAResult(
        workspace_name="TestClient",
        campaigns=[campaign_result],
        total_broken=1,
        last_checked=datetime.now(timezone.utc),
    )
    await get_cache().set_workspace("TestClient", ws_result)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ws/TestClient/campaign/camp-test-001")

    assert response.status_code == 200
    assert "Variable Issues" in response.text
    assert "cityName" in response.text
    assert "broken@test.com" in response.text
    assert "[empty]" in response.text


async def test_campaign_page_clean_state(mock_env):
    """Campaign page shows 'All clear' when no broken leads. Per VIEW-03."""
    from datetime import datetime, timezone
    from app.main import create_app
    from app.models.qa import CampaignQAResult, WorkspaceQAResult
    from app.services import workspace as ws_module
    from app.services.cache import get_cache

    ws_module.load_from_env()
    app = create_app()

    campaign_result = CampaignQAResult(
        campaign_id="camp-clean-001",
        campaign_name="Clean Campaign",
        total_leads=10,
        broken_count=0,
        issues_by_variable={},
        broken_leads=[],
        last_checked=datetime.now(timezone.utc),
    )
    ws_result = WorkspaceQAResult(
        workspace_name="TestClient",
        campaigns=[campaign_result],
        total_broken=0,
        last_checked=datetime.now(timezone.utc),
    )
    await get_cache().set_workspace("TestClient", ws_result)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ws/TestClient/campaign/camp-clean-001")

    assert response.status_code == 200
    assert "All clear" in response.text


async def test_breadcrumb_three_levels(mock_env):
    """Campaign page breadcrumb has three levels: All Workspaces > WS > Campaign. Per VIEW-07, D-02."""
    from datetime import datetime, timezone
    from app.main import create_app
    from app.models.qa import CampaignQAResult, WorkspaceQAResult
    from app.services import workspace as ws_module
    from app.services.cache import get_cache

    ws_module.load_from_env()
    app = create_app()

    campaign_result = CampaignQAResult(
        campaign_id="camp-bc-001",
        campaign_name="Breadcrumb Campaign",
        total_leads=5,
        broken_count=0,
        last_checked=datetime.now(timezone.utc),
    )
    ws_result = WorkspaceQAResult(
        workspace_name="TestClient",
        campaigns=[campaign_result],
        last_checked=datetime.now(timezone.utc),
    )
    await get_cache().set_workspace("TestClient", ws_result)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ws/TestClient/campaign/camp-bc-001")

    text = response.text
    # All three breadcrumb segments present
    assert "All Workspaces" in text
    assert 'href="/"' in text
    assert 'href="/ws/TestClient"' in text
    assert "Breadcrumb Campaign" in text


async def test_scan_campaign_endpoint(mock_env):
    """POST /api/scan/ws/{name}/campaign/{id} returns HTML. Per OPS-03."""
    from app.main import create_app
    from app.services import workspace as ws_module
    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/scan/ws/TestClient/campaign/camp-001")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
