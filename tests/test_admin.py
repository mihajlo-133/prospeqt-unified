"""Integration tests for admin authentication and workspace management."""
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
    from app.services import workspace as ws_module

    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/login")
    assert response.status_code == 200
    assert "Admin Access" in response.text


async def test_admin_login_correct_password(mock_env):
    from app.main import create_app
    from app.services import workspace as ws_module

    ws_module.load_from_env()
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
    from app.services import workspace as ws_module

    ws_module.load_from_env()
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
    from app.services import workspace as ws_module

    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin", follow_redirects=False)
    # Should be 401 (unauthorized) or redirect to login
    assert response.status_code in (401, 302, 303)


async def test_admin_panel_with_auth(mock_env):
    from app.main import create_app
    from app.services import workspace as ws_module

    ws_module.load_from_env()
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
    assert "Workspace Admin" in response.text


async def test_add_workspace(mock_env):
    from app.main import create_app
    from app.services import workspace as ws_module

    ws_module.load_from_env()
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
    from app.services import workspace as ws_module

    ws_module.load_from_env()
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
    from app.services import workspace as ws_module

    ws_module.load_from_env()
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
    from app.services import workspace as ws_module

    ws_module.load_from_env()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # No cookies at all — dashboard must be open access per ADM-04
        response = await client.get("/")
    assert response.status_code == 200
