"""Unit tests for the workspace registry service."""

import os

import pytest

from app.services import workspace as ws_module
from app.services.workspace import (
    add_workspace,
    get_api_key,
    list_workspaces,
    load_from_env,
    remove_workspace,
)


@pytest.fixture(autouse=True)
def reset_registry():
    """Clear the in-memory registry and any leaked env vars before and after each test."""
    ws_module._registry.clear()
    # Clean up any WORKSPACE_*_API_KEY env vars leaked from other test modules
    leaked = [k for k in os.environ if k.startswith("WORKSPACE_") and k.endswith("_API_KEY")]
    for k in leaked:
        del os.environ[k]
    yield
    ws_module._registry.clear()
    leaked = [k for k in os.environ if k.startswith("WORKSPACE_") and k.endswith("_API_KEY")]
    for k in leaked:
        del os.environ[k]


def test_load_from_env_reads_workspace_vars(mock_env):
    """load_from_env() reads WORKSPACE_*_API_KEY env vars and populates registry."""
    load_from_env()
    workspaces = list_workspaces()
    names = [w["name"] for w in workspaces]
    assert "Testclient" in names
    assert "Another Ws" in names
    assert len(workspaces) == 2


def test_get_api_key_returns_key(mock_env):
    """get_api_key() returns the correct key for a known workspace."""
    load_from_env()
    assert get_api_key("testclient") == "test-key-1234"


def test_get_api_key_returns_none_for_unknown(mock_env):
    """get_api_key() returns None for an unknown workspace name."""
    load_from_env()
    assert get_api_key("nonexistent") is None


def test_add_workspace_adds_to_registry(mock_env, monkeypatch):
    """add_workspace() makes the workspace available via get_api_key."""
    # Pre-declare the env var via monkeypatch so cleanup is automatic on test teardown
    monkeypatch.setenv("WORKSPACE_NEWCLIENT_API_KEY", "")
    add_workspace("newclient", "new-key")
    assert get_api_key("newclient") == "new-key"


def test_remove_workspace_removes_from_registry(mock_env):
    """remove_workspace() removes the workspace and returns True if it existed."""
    load_from_env()
    result = remove_workspace("testclient")
    assert result is True
    assert get_api_key("testclient") is None


def test_remove_nonexistent_returns_false(mock_env):
    """remove_workspace() returns False when the workspace does not exist."""
    result = remove_workspace("ghost")
    assert result is False


def test_env_pattern_ignores_non_workspace_vars(mock_env, monkeypatch):
    """load_from_env() only picks up WORKSPACE_*_API_KEY vars, not others."""
    monkeypatch.setenv("SOME_OTHER_VAR", "foo")
    load_from_env()
    names = [w["name"] for w in list_workspaces()]
    assert "some-other" not in names
    # Only the two workspace vars from mock_env should be present
    assert len(names) == 2
