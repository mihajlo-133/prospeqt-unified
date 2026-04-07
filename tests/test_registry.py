"""Tests for the unified client registry (app.services.registry).

Covers:
- Loading from env vars (monitoring-style and legacy QA-style)
- list_monitoring_workspaces() and list_qa_workspaces() filtering
- get_api_key() lookups
- Backward compat with WORKSPACE_{SLUG}_API_KEY format
"""
import pytest

from app.services import registry as registry_module
from app.services.registry import (
    ClientEntry,
    _CLIENT_DEFS,
    get_api_key,
    get_client,
    list_monitoring_workspaces,
    list_qa_workspaces,
    list_workspaces,
    load_from_env,
)


@pytest.fixture(autouse=True)
def reset_registry():
    """Clear the in-memory registry before and after each test."""
    registry_module._registry.clear()
    yield
    registry_module._registry.clear()


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------


def test_load_from_env_creates_all_client_entries(mock_env):
    """load_from_env() creates one entry per _CLIENT_DEFS definition."""
    load_from_env()
    assert len(registry_module._registry) == len(_CLIENT_DEFS)


def test_load_from_env_sets_key_from_monitoring_env_var(mock_env):
    """load_from_env() reads INSTANTLY_MYPLACE and sets it on MyPlace."""
    load_from_env()
    entry = next(e for e in registry_module._registry if e.slug == "myplace")
    assert entry._api_key == "test-key-1234"


def test_load_from_env_sets_key_for_emailbison_client(mock_env, monkeypatch):
    """load_from_env() reads EMAILBISON_RANKZERO and sets it on RankZero."""
    monkeypatch.setenv("EMAILBISON_RANKZERO", "eb-key-789")
    load_from_env()
    assert get_api_key("RankZero") == "eb-key-789"


def test_load_from_env_backward_compat_legacy_key(mock_env, monkeypatch):
    """load_from_env() falls back to WORKSPACE_{SLUG}_API_KEY when primary env var absent."""
    monkeypatch.delenv("INSTANTLY_MYPLACE", raising=False)
    monkeypatch.setenv("WORKSPACE_MYPLACE_API_KEY", "legacy-key-xyz")
    load_from_env()
    assert get_api_key("MyPlace") == "legacy-key-xyz"


def test_load_from_env_primary_takes_precedence_over_legacy(mock_env, monkeypatch):
    """Primary env var (INSTANTLY_*) wins over legacy WORKSPACE_*_API_KEY."""
    monkeypatch.setenv("WORKSPACE_MYPLACE_API_KEY", "legacy-key-xyz")
    # mock_env already sets INSTANTLY_MYPLACE = "test-key-1234"
    load_from_env()
    assert get_api_key("MyPlace") == "test-key-1234"


def test_load_from_env_clears_previous_registry(mock_env):
    """Calling load_from_env() twice replaces the old registry (no duplication)."""
    load_from_env()
    count_after_first = len(registry_module._registry)
    load_from_env()
    assert len(registry_module._registry) == count_after_first


def test_load_from_env_entry_with_no_key_has_none(monkeypatch):
    """Entries with no matching env var have _api_key=None."""
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    for var in ["INSTANTLY_KAYSE", "WORKSPACE_KAYSE_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    load_from_env()
    assert get_api_key("Kayse") is None


# ---------------------------------------------------------------------------
# list_monitoring_workspaces()
# ---------------------------------------------------------------------------


def test_list_monitoring_workspaces_returns_monitoring_enabled(mock_env):
    """list_monitoring_workspaces() returns all entries with monitoring_enabled=True."""
    load_from_env()
    entries = list_monitoring_workspaces()
    # All 9 clients have monitoring_enabled=True per _CLIENT_DEFS
    assert len(entries) == 9
    for entry in entries:
        assert entry.monitoring_enabled is True


def test_list_monitoring_workspaces_returns_client_entries(mock_env):
    """list_monitoring_workspaces() returns ClientEntry objects (not dicts)."""
    load_from_env()
    entries = list_monitoring_workspaces()
    for entry in entries:
        assert isinstance(entry, ClientEntry)
        assert hasattr(entry, "platform")
        assert hasattr(entry, "workspace_id")


def test_list_monitoring_workspaces_includes_emailbison(mock_env):
    """list_monitoring_workspaces() includes EmailBison clients (monitoring-only)."""
    load_from_env()
    entries = list_monitoring_workspaces()
    slugs = [e.slug for e in entries]
    assert "rankzero" in slugs
    assert "swishfunding_eb" in slugs


# ---------------------------------------------------------------------------
# list_qa_workspaces()
# ---------------------------------------------------------------------------


def test_list_qa_workspaces_returns_qa_enabled_only(mock_env):
    """list_qa_workspaces() returns only entries where qa_enabled=True."""
    load_from_env()
    entries = list_qa_workspaces()
    for entry in entries:
        assert entry.qa_enabled is True


def test_list_qa_workspaces_excludes_emailbison(mock_env):
    """list_qa_workspaces() excludes EmailBison clients (qa_enabled=False)."""
    load_from_env()
    entries = list_qa_workspaces()
    slugs = [e.slug for e in entries]
    assert "rankzero" not in slugs
    assert "swishfunding_eb" not in slugs


def test_list_qa_workspaces_count(mock_env):
    """list_qa_workspaces() returns the 7 Instantly clients (qa_enabled)."""
    load_from_env()
    entries = list_qa_workspaces()
    assert len(entries) == 7


def test_list_qa_workspaces_contains_expected_instantly_clients(mock_env):
    """list_qa_workspaces() includes all 7 Instantly workspace slugs."""
    load_from_env()
    entries = list_qa_workspaces()
    slugs = {e.slug for e in entries}
    expected = {"myplace", "swishfunding", "smartmatchapp", "heyreach", "kayse", "prosperly", "enavra"}
    assert expected == slugs


# ---------------------------------------------------------------------------
# get_api_key()
# ---------------------------------------------------------------------------


def test_get_api_key_returns_correct_key(mock_env):
    """get_api_key() returns the key for a known client by display name."""
    load_from_env()
    assert get_api_key("MyPlace") == "test-key-1234"
    assert get_api_key("SwishFunding") == "test-key-5678"


def test_get_api_key_case_insensitive(mock_env):
    """get_api_key() is case-insensitive on display name."""
    load_from_env()
    assert get_api_key("myplace") == "test-key-1234"
    assert get_api_key("MYPLACE") == "test-key-1234"
    assert get_api_key("MyPlace") == "test-key-1234"


def test_get_api_key_returns_none_for_unknown(mock_env):
    """get_api_key() returns None for names not in the registry."""
    load_from_env()
    assert get_api_key("nonexistent") is None
    assert get_api_key("") is None


def test_get_api_key_returns_none_for_no_key_set(monkeypatch):
    """get_api_key() returns None when entry exists but has no key."""
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("INSTANTLY_KAYSE", raising=False)
    monkeypatch.delenv("WORKSPACE_KAYSE_API_KEY", raising=False)
    load_from_env()
    assert get_api_key("Kayse") is None


# ---------------------------------------------------------------------------
# get_client()
# ---------------------------------------------------------------------------


def test_get_client_returns_client_entry(mock_env):
    """get_client() returns the full ClientEntry with all fields."""
    load_from_env()
    entry = get_client("MyPlace")
    assert entry is not None
    assert isinstance(entry, ClientEntry)
    assert entry.slug == "myplace"
    assert entry.platform == "instantly"
    assert entry.workspace_id == "3c4b6833-22ba-4cee-8215-021ab01da35e"


def test_get_client_returns_none_for_unknown(mock_env):
    """get_client() returns None for names not in the registry."""
    load_from_env()
    assert get_client("nonexistent") is None


def test_get_client_case_insensitive(mock_env):
    """get_client() is case-insensitive on display name."""
    load_from_env()
    assert get_client("myplace") is not None
    assert get_client("MYPLACE") is not None


# ---------------------------------------------------------------------------
# ClientEntry dataclass
# ---------------------------------------------------------------------------


def test_client_entry_fields():
    """ClientEntry has all required fields with correct types."""
    entry = ClientEntry(
        name="TestClient",
        slug="testclient",
        platform="instantly",
        env_var="INSTANTLY_TESTCLIENT",
        workspace_id="some-uuid",
        monitoring_enabled=True,
        qa_enabled=True,
        _api_key="secret-key",
    )
    assert entry.name == "TestClient"
    assert entry.slug == "testclient"
    assert entry.platform == "instantly"
    assert entry.env_var == "INSTANTLY_TESTCLIENT"
    assert entry.workspace_id == "some-uuid"
    assert entry.monitoring_enabled is True
    assert entry.qa_enabled is True
    assert entry._api_key == "secret-key"


def test_client_entry_api_key_not_in_repr():
    """ClientEntry._api_key is excluded from repr (repr=False)."""
    entry = ClientEntry(
        name="TestClient",
        slug="testclient",
        platform="instantly",
        env_var="INSTANTLY_TESTCLIENT",
        _api_key="super-secret",
    )
    assert "super-secret" not in repr(entry)
