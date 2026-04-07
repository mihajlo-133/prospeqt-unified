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
    # Use real client env var names from _CLIENT_DEFS so registry.load_from_env() picks them up
    monkeypatch.setenv("INSTANTLY_MYPLACE", "test-key-1234")
    monkeypatch.setenv("INSTANTLY_SWISHFUNDING", "test-key-5678")
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    # Initialise monitoring config to factory defaults so routes that call
    # get_config() work without going through the app lifespan.
    from app.services import monitoring_config
    monitoring_config._reset_for_tests()
    monitoring_config.load_config()

    yield monkeypatch

    # Clean up after each test to avoid cross-test contamination.
    monitoring_config._reset_for_tests()


@pytest.fixture
async def app_client(mock_env):
    from app.main import create_app

    app_instance = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app_instance), base_url="http://test"
    ) as client:
        yield client
