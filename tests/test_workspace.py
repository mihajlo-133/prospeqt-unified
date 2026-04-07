"""Unit tests for the workspace registry service (app.services.registry)."""

import os

import pytest

from app.services import registry as ws_module
from app.services.registry import (
    ClientEntry,
    add_workspace,
    get_api_key,
    get_client,
    list_monitoring_workspaces,
    list_qa_workspaces,
    list_workspaces,
    load_from_env,
    remove_workspace,
)


@pytest.fixture(autouse=True)
def reset_registry():
    """Clear the in-memory registry before and after each test."""
    ws_module._registry.clear()
    yield
    ws_module._registry.clear()


def test_load_from_env_populates_static_clients(mock_env):
    """load_from_env() populates all 9 static client entries from _CLIENT_DEFS."""
    load_from_env()
    # All 9 clients should be loaded regardless of env vars
    assert len(ws_module._registry) == 9


def test_load_from_env_picks_up_env_var_key(mock_env):
    """load_from_env() reads INSTANTLY_MYPLACE and sets key on MyPlace entry."""
    load_from_env()
    assert get_api_key("MyPlace") == "test-key-1234"


def test_load_from_env_picks_up_second_env_var_key(mock_env):
    """load_from_env() reads INSTANTLY_SWISHFUNDING and sets key on SwishFunding entry."""
    load_from_env()
    assert get_api_key("SwishFunding") == "test-key-5678"


def test_load_from_env_legacy_workspace_key(mock_env, monkeypatch):
    """load_from_env() accepts legacy WORKSPACE_{SLUG}_API_KEY format as fallback."""
    # MyPlace slug = "myplace" -> legacy var = WORKSPACE_MYPLACE_API_KEY
    # Override INSTANTLY_MYPLACE so we test the fallback path
    monkeypatch.delenv("INSTANTLY_MYPLACE", raising=False)
    monkeypatch.setenv("WORKSPACE_MYPLACE_API_KEY", "legacy-key-abc")
    load_from_env()
    assert get_api_key("MyPlace") == "legacy-key-abc"


def test_load_from_env_no_key_entry_still_present(monkeypatch):
    """Clients with no matching env var are still in the registry (key is None)."""
    # Don't set any env vars at all
    for entry_key in ["INSTANTLY_MYPLACE", "INSTANTLY_SWISHFUNDING", "INSTANTLY_SMARTMATCHAPP",
                       "INSTANTLY_HEYREACH", "INSTANTLY_KAYSE", "INSTANTLY_PROSPERLY",
                       "INSTANTLY_ENAVRA", "EMAILBISON_RANKZERO", "EMAILBISON_SWISHFUNDING"]:
        monkeypatch.delenv(entry_key, raising=False)
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    load_from_env()
    # All 9 entries loaded
    assert len(ws_module._registry) == 9
    # Keys are all None
    for entry in ws_module._registry:
        assert entry._api_key is None


def test_get_api_key_case_insensitive(mock_env):
    """get_api_key() matches by display name, case-insensitive."""
    load_from_env()
    assert get_api_key("myplace") == "test-key-1234"
    assert get_api_key("MYPLACE") == "test-key-1234"
    assert get_api_key("MyPlace") == "test-key-1234"


def test_get_api_key_returns_none_for_unknown(mock_env):
    """get_api_key() returns None for a name not in the registry."""
    load_from_env()
    assert get_api_key("nonexistent") is None


def test_list_workspaces_returns_qa_enabled_only(mock_env):
    """list_workspaces() returns only qa_enabled=True entries (backward compat)."""
    load_from_env()
    workspaces = list_workspaces()
    names = [w["name"] for w in workspaces]
    # QA-enabled: all 7 Instantly clients
    assert "MyPlace" in names
    assert "SwishFunding" in names
    # QA-disabled: EmailBison clients
    assert "RankZero" not in names
    assert "SwishFunding (EB)" not in names


def test_list_workspaces_key_preview_format(mock_env):
    """list_workspaces() shows key preview '...XXXX' for entries with keys."""
    load_from_env()
    workspaces = list_workspaces()
    myplace = next(w for w in workspaces if w["name"] == "MyPlace")
    assert myplace["key_preview"] == "...1234"


def test_list_workspaces_no_key_shows_not_set(monkeypatch):
    """list_workspaces() shows 'not set' for entries without a key."""
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    for k in ["INSTANTLY_MYPLACE", "INSTANTLY_SWISHFUNDING", "INSTANTLY_SMARTMATCHAPP",
              "INSTANTLY_HEYREACH", "INSTANTLY_KAYSE", "INSTANTLY_PROSPERLY", "INSTANTLY_ENAVRA"]:
        monkeypatch.delenv(k, raising=False)
    load_from_env()
    workspaces = list_workspaces()
    for w in workspaces:
        assert w["key_preview"] == "not set"


def test_add_workspace_updates_existing_client(mock_env):
    """add_workspace() updates api key for an existing static client."""
    load_from_env()
    add_workspace("MyPlace", "new-key-999")
    assert get_api_key("MyPlace") == "new-key-999"


def test_add_workspace_adds_dynamic_client(mock_env):
    """add_workspace() adds a brand-new client not in _CLIENT_DEFS."""
    load_from_env()
    add_workspace("BrandNewClient", "dynamic-key-abc")
    assert get_api_key("BrandNewClient") == "dynamic-key-abc"


def test_remove_workspace_removes_from_registry(mock_env):
    """remove_workspace() removes the entry and returns True if it existed."""
    load_from_env()
    result = remove_workspace("MyPlace")
    assert result is True
    assert get_api_key("MyPlace") is None


def test_remove_nonexistent_returns_false(mock_env):
    """remove_workspace() returns False when the workspace does not exist."""
    load_from_env()
    result = remove_workspace("ghost")
    assert result is False
