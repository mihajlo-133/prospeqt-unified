import json
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def campaign_response(fixtures_dir: Path) -> dict:
    with open(fixtures_dir / "campaign_response.json") as f:
        return json.load(f)


@pytest.fixture
def leads_response(fixtures_dir: Path) -> dict:
    with open(fixtures_dir / "leads_response.json") as f:
        return json.load(f)


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("WORKSPACE_TESTCLIENT_API_KEY", "test-key-1234")
    monkeypatch.setenv("WORKSPACE_ANOTHER_WS_API_KEY", "test-key-5678")
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    return monkeypatch


@pytest.fixture
async def app_client(mock_env):
    from app.main import create_app

    app_instance = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app_instance), base_url="http://test"
    ) as client:
        yield client
