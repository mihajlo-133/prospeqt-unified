"""Integration tests for admin authentication and workspace management."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient


async def login_and_get_cookies(client: AsyncClient, password: str = "testpass") -> dict:
    """Helper: POST to /admin/login and return cookies dict."""
    response = await client.post(
        "/admin/login",
        data={"password": password},
        follow_redirects=False,
    )
    return dict(response.cookies)


async def test_admin_login_page_renders(mock_env):
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/login")
    assert response.status_code == 200
    assert "Admin Access" in response.text


async def test_admin_login_correct_password(mock_env):
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
    assert response.status_code in (302, 303)
    assert "admin_session" in response.cookies


async def test_admin_login_wrong_password(mock_env):
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/admin/login",
            data={"password": "wrongpassword"},
            follow_redirects=False,
        )
    assert response.status_code == 200
    assert "Incorrect password" in response.text


async def test_admin_panel_requires_auth(mock_env):
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin", follow_redirects=False)
    # Should be 401 (unauthorized) or redirect to login
    assert response.status_code in (401, 302, 303)


async def test_admin_panel_with_auth(mock_env):
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Login first
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        assert login_resp.status_code in (302, 303)
        token = login_resp.cookies.get("admin_session")
        assert token is not None

        # Access admin panel with cookie
        response = await client.get(
            "/admin",
            cookies={"admin_session": token},
        )
    assert response.status_code == 200
    assert "Add Workspace" in response.text


async def test_add_workspace(mock_env):
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Login
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        # Add workspace with 2 fields only (per D-07: no platform field)
        add_resp = await client.post(
            "/admin/workspaces",
            data={"workspace_name": "NewClient", "api_key": "new-key-123"},
            cookies={"admin_session": token},
            follow_redirects=False,
        )
        assert add_resp.status_code in (302, 303)

        # Follow redirect to admin panel and verify workspace appears
        panel_resp = await client.get(
            "/admin",
            cookies={"admin_session": token},
        )
    assert "newclient" in panel_resp.text.lower() or "NewClient" in panel_resp.text


async def test_remove_workspace(mock_env):
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Login
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        # Remove the testclient workspace (set up by mock_env)
        del_resp = await client.post(
            "/admin/workspaces/testclient/delete",
            cookies={"admin_session": token},
            follow_redirects=False,
        )
        assert del_resp.status_code in (302, 303)

        # Verify workspace is removed from panel
        panel_resp = await client.get(
            "/admin",
            cookies={"admin_session": token},
        )
    assert "testclient" not in panel_resp.text.lower()


async def test_admin_logout(mock_env):
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Login
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        # Logout
        logout_resp = await client.get(
            "/admin/logout",
            cookies={"admin_session": token},
            follow_redirects=False,
        )
        assert logout_resp.status_code in (302, 303)

        # Try to access admin panel after logout — should fail (no cookie)
        response = await client.get("/admin", follow_redirects=False)
    assert response.status_code in (401, 302, 303)


async def test_dashboard_open_access(mock_env):
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # No cookies at all — QA dashboard must be open access per ADM-04
        response = await client.get("/qa")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Phase 4: Workspace registry fields
# ---------------------------------------------------------------------------


async def test_add_workspace_with_platform_and_flags(mock_env):
    """POST /admin/workspaces with all new fields → 303 → registry entry has correct platform + flags."""
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        add_resp = await client.post(
            "/admin/workspaces",
            data={
                "workspace_name": "NewInstantlyClient",
                "api_key": "inst-key-abc123",
                "platform": "instantly",
                "monitoring_enabled": "true",
                "qa_enabled": "true",
            },
            cookies={"admin_session": token},
            follow_redirects=False,
        )
    assert add_resp.status_code in (302, 303)
    entry = registry.get_client("NewInstantlyClient")
    assert entry is not None
    assert entry.platform == "instantly"
    assert entry.monitoring_enabled is True
    assert entry.qa_enabled is True


async def test_add_workspace_emailbison_platform(mock_env):
    """POST /admin/workspaces with emailbison platform → registry entry has correct platform + flags."""
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        add_resp = await client.post(
            "/admin/workspaces",
            data={
                "workspace_name": "NewEBClient",
                "api_key": "eb-key-xyz789",
                "platform": "emailbison",
                "monitoring_enabled": "true",
                # qa_enabled omitted → False
            },
            cookies={"admin_session": token},
            follow_redirects=False,
        )
    assert add_resp.status_code in (302, 303)
    entry = registry.get_client("NewEBClient")
    assert entry is not None
    assert entry.platform == "emailbison"
    assert entry.monitoring_enabled is True
    assert entry.qa_enabled is False


async def test_list_workspaces_shows_all_with_new_fields(mock_env):
    """GET /admin while authed → HTML contains platform and monitoring/qa indicators."""
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        panel_resp = await client.get(
            "/admin",
            cookies={"admin_session": token},
        )
    assert panel_resp.status_code == 200
    # Static clients loaded via mock_env (MyPlace + SwishFunding) must appear
    text = panel_resp.text.lower()
    assert "myplace" in text
    assert "swishfunding" in text
    # Admin panel must expose platform information
    assert "instantly" in text


# ---------------------------------------------------------------------------
# Phase 4: Global threshold save + validation
# ---------------------------------------------------------------------------


async def test_save_global_thresholds_valid(mock_env):
    """POST /admin/thresholds with valid values → 303 → config updated."""
    from app.main import create_app
    from app.services import monitoring_config, registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        resp = await client.post(
            "/admin/thresholds",
            data={
                "reply_rate_warn": "1.5",
                "reply_rate_red": "0.8",
                "sent_pct_warn": "0.9",
                "sent_pct_red": "0.6",
                "bounce_rate_warn": "3.0",
                "bounce_rate_red": "6.0",
                "opps_pct_warn": "0.5",
                "pool_days_warn": "7",
                "pool_days_red": "3",
            },
            cookies={"admin_session": token},
            follow_redirects=False,
        )
    assert resp.status_code in (302, 303)
    cfg = monitoring_config.get_config()
    assert cfg["global_thresholds"]["reply_rate_warn"] == 1.5
    assert cfg["global_thresholds"]["reply_rate_red"] == 0.8


async def test_save_global_thresholds_invalid_rejected(mock_env):
    """POST with reply_rate_warn < reply_rate_red → 400, config unchanged."""
    from app.main import create_app
    from app.services import monitoring_config, registry

    # Capture baseline BEFORE the POST — whatever is currently loaded
    original_warn = monitoring_config.get_config()["global_thresholds"].get("reply_rate_warn")

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        resp = await client.post(
            "/admin/thresholds",
            data={
                "reply_rate_warn": "0.5",   # violates warn > red
                "reply_rate_red": "1.0",
            },
            cookies={"admin_session": token},
            follow_redirects=False,
        )
    assert resp.status_code == 400
    # Config must be unchanged — reply_rate_warn still at pre-POST value
    cfg = monitoring_config.get_config()
    assert cfg["global_thresholds"].get("reply_rate_warn") == original_warn


async def test_save_global_thresholds_invalid_bounce(mock_env):
    """POST with bounce_rate_warn > bounce_rate_red → 400, config unchanged."""
    from app.main import create_app
    from app.services import monitoring_config, registry

    # Capture baseline BEFORE the POST — whatever is currently loaded
    original_bounce_warn = monitoring_config.get_config()["global_thresholds"].get("bounce_rate_warn")

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        resp = await client.post(
            "/admin/thresholds",
            data={
                "bounce_rate_warn": "5.0",   # violates warn < red
                "bounce_rate_red": "3.0",
            },
            cookies={"admin_session": token},
            follow_redirects=False,
        )
    assert resp.status_code == 400
    cfg = monitoring_config.get_config()
    assert cfg["global_thresholds"].get("bounce_rate_warn") == original_bounce_warn


# ---------------------------------------------------------------------------
# Phase 4: Per-client KPI save
# ---------------------------------------------------------------------------


async def test_save_client_kpi(mock_env):
    """POST /admin/clients/MyPlace/kpi → 303 → get_client_kpi returns updated values."""
    from app.main import create_app
    from app.services import monitoring_config, registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        resp = await client.post(
            "/admin/clients/MyPlace/kpi",
            data={
                "sent": "3000",
                "not_contacted": "1500",
                "opps_per_day": "5.0",
                "reply_rate": "2.0",
            },
            cookies={"admin_session": token},
            follow_redirects=False,
        )
    assert resp.status_code in (302, 303)

    kpi = monitoring_config.get_client_kpi("MyPlace")
    assert kpi["sent"] == 3000
    assert kpi["not_contacted"] == 1500
    assert kpi["opps_per_day"] == 5.0
    assert kpi["reply_rate"] == 2.0

    # Unrelated client must be unchanged
    swish_kpi = monitoring_config.get_client_kpi("SwishFunding")
    assert swish_kpi["sent"] == 10000  # factory default


# ---------------------------------------------------------------------------
# Phase 4: Per-client threshold overrides
# ---------------------------------------------------------------------------


async def test_save_client_threshold_override(mock_env):
    """POST /admin/clients/MyPlace/thresholds with one field → that field updated, rest inherit global."""
    from app.main import create_app
    from app.services import monitoring_config, registry
    from app.services.monitoring_config import FACTORY_THRESHOLDS

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        resp = await client.post(
            "/admin/clients/MyPlace/thresholds",
            data={"reply_rate_warn": "2.0"},
            cookies={"admin_session": token},
            follow_redirects=False,
        )
    assert resp.status_code in (302, 303)

    thresholds = monitoring_config.get_client_thresholds("MyPlace")
    assert thresholds["reply_rate_warn"] == 2.0
    # Other fields still equal global (factory since no global override set)
    assert thresholds["bounce_rate_warn"] == FACTORY_THRESHOLDS["bounce_rate_warn"]
    assert thresholds["pool_days_warn"] == FACTORY_THRESHOLDS["pool_days_warn"]


async def test_save_client_threshold_override_empty_inherits(mock_env):
    """POST /admin/clients/MyPlace/thresholds with all empty fields → no crash, inherits global."""
    from app.main import create_app
    from app.services import monitoring_config, registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        resp = await client.post(
            "/admin/clients/MyPlace/thresholds",
            data={
                "reply_rate_warn": "",
                "reply_rate_red": "",
                "sent_pct_warn": "",
                "sent_pct_red": "",
                "bounce_rate_warn": "",
                "bounce_rate_red": "",
                "opps_pct_warn": "",
                "pool_days_warn": "",
                "pool_days_red": "",
            },
            cookies={"admin_session": token},
            follow_redirects=False,
        )
    # Must not crash — 200 (re-render with message) or 303 redirect both acceptable
    assert resp.status_code in (200, 302, 303)

    # Empty fields = inherit global. Effective thresholds must match the current global thresholds.
    cfg = monitoring_config.get_config()
    global_thresholds = cfg["global_thresholds"]
    client_thresholds = monitoring_config.get_client_thresholds("MyPlace")
    for key in global_thresholds:
        assert client_thresholds[key] == global_thresholds[key], (
            f"{key}: expected global value {global_thresholds[key]}, got {client_thresholds[key]}"
        )


# ---------------------------------------------------------------------------
# Phase 4: Config export
# ---------------------------------------------------------------------------


async def test_config_export_returns_json_attachment(mock_env):
    """GET /admin/config/export → JSON attachment with expected fields."""
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        resp = await client.get(
            "/admin/config/export",
            cookies={"admin_session": token},
        )
    assert resp.status_code == 200
    assert "application/json" in resp.headers.get("content-type", "")
    disposition = resp.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert "dashboard_config.json" in disposition

    body = json.loads(resp.text)
    assert "version" in body
    assert "global_thresholds" in body
    assert "clients" in body


async def test_config_export_requires_auth(mock_env):
    """GET /admin/config/export without auth cookie → 401 or redirect to login."""
    from app.main import create_app
    from app.services import registry

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/admin/config/export",
            follow_redirects=False,
        )
    assert resp.status_code in (401, 302, 303)


# ---------------------------------------------------------------------------
# Phase 4: Cache invalidation on config save
# ---------------------------------------------------------------------------


async def test_save_config_invalidates_cache(mock_env):
    """POST /admin/thresholds → save_config() fires registered hooks (cache invalidation path)."""
    from app.main import create_app
    from app.services import monitoring_config, registry

    # Register a MagicMock as a save hook — fires when save_config() is called.
    # mock_env already called load_config() so get_config() works; we just add our hook.
    mock_hook = MagicMock()
    monitoring_config.register_save_hook(mock_hook)

    registry.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_resp = await client.post(
            "/admin/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )
        token = login_resp.cookies["admin_session"]

        resp = await client.post(
            "/admin/thresholds",
            data={
                "reply_rate_warn": "1.5",
                "reply_rate_red": "0.8",
                "sent_pct_warn": "0.9",
                "sent_pct_red": "0.6",
                "bounce_rate_warn": "3.0",
                "bounce_rate_red": "6.0",
                "opps_pct_warn": "0.5",
                "pool_days_warn": "7",
                "pool_days_red": "3",
            },
            cookies={"admin_session": token},
            follow_redirects=False,
        )
    assert resp.status_code in (302, 303)
    assert mock_hook.called, "save_config() must invoke registered save hooks (cache invalidation)"
