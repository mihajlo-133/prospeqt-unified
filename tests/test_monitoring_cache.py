"""Tests for app/services/monitoring_cache.py.

Covers:
  - should_refresh: TTL logic, never-fetched always-stale, fresh after write
  - get_generation: default 0, increments on write_client_data
  - write_client_data: strips _nc_backfill/_nc_api_key, increments generation,
      clears prior error, returns (gen, backfill_ids, api_key)
  - write_error: stores friendly error, bumps timestamp, leaves stale data
  - write_backfill: generation guard (stale → False), per-campaign NC update,
      client-level NC/in_progress recomputation, re-classification, missing
      client → False
  - get_all_monitoring_data: unseen client → loading, error client → error,
      cached client → enriched with kpi/thresholds/status/fetched_at,
      always reclassifies on read
  - mock mode: set_mock_mode, is_mock_mode, _get_all_mock via fixture,
      _error fixture entry → error card, missing fixture entry → error card
  - invalidate_all / invalidate_client: force should_refresh → True
  - init_cache / _on_config_saved: hook wired to config save
  - _reset_for_tests: full state clear including mock flag
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services import monitoring_cache as mc
from app.services import monitoring_config as cfg
from app.services import registry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Reset cache + config before and after every test."""
    # Reset cache
    mc._reset_for_tests()
    # Reset config with factory defaults
    cfg._reset_for_tests()
    cfg.load_config(config_path=tmp_path / "no_config.json")
    # Populate registry with all 9 clients (no real API keys needed)
    registry.load_from_env()
    yield
    mc._reset_for_tests()
    cfg._reset_for_tests()


@pytest.fixture
def mock_data() -> dict:
    path = Path(__file__).parent / "fixtures" / "mock_monitoring_data.json"
    return json.loads(path.read_text())


def _minimal_data(**overrides) -> dict:
    """A minimal valid ClientData dict that classifies green with factory thresholds.

    Uses Enavra-style values: active=2, total=2, sent=2000 (vs KPI=2000),
    reply_rate=2.0, bounce_rate=1.0, not_contacted=50000, avg_sent_7d=2000.
    """
    base = {
        "platform": "instantly",
        "active_campaigns": 2,
        "total_campaigns": 2,
        "sent_today": 2000,
        "reply_rate_today": 2.0,
        "bounce_rate": 1.0,
        "not_contacted": 50000,
        "avg_sent_7d": 2000.0,
        "campaigns": [],
    }
    base.update(overrides)
    return base


# ===========================================================================
# should_refresh
# ===========================================================================

class TestShouldRefresh:
    def test_never_seen_is_stale(self):
        assert mc.should_refresh("UnknownClient") is True

    def test_fresh_after_write(self):
        mc.write_client_data("Enavra", _minimal_data())
        assert mc.should_refresh("Enavra") is False

    def test_stale_after_ttl_expires(self):
        mc.write_client_data("Enavra", _minimal_data())
        # Backdate the timestamp so TTL has expired
        with mc._lock:
            mc._ts["Enavra"] = time.time() - mc.CACHE_TTL - 1
        assert mc.should_refresh("Enavra") is True

    def test_still_fresh_just_inside_ttl(self):
        mc.write_client_data("Enavra", _minimal_data())
        with mc._lock:
            mc._ts["Enavra"] = time.time() - mc.CACHE_TTL + 30
        assert mc.should_refresh("Enavra") is False

    def test_error_write_bumps_timestamp(self):
        mc.write_error("Enavra", "HTTP 500 error")
        # After write_error the timestamp is set → fresh (won't hammer failing client)
        assert mc.should_refresh("Enavra") is False


# ===========================================================================
# get_generation
# ===========================================================================

class TestGetGeneration:
    def test_zero_for_unknown_client(self):
        assert mc.get_generation("Nobody") == 0

    def test_increments_on_write(self):
        assert mc.get_generation("Enavra") == 0
        mc.write_client_data("Enavra", _minimal_data())
        assert mc.get_generation("Enavra") == 1
        mc.write_client_data("Enavra", _minimal_data())
        assert mc.get_generation("Enavra") == 2

    def test_independent_per_client(self):
        mc.write_client_data("Enavra", _minimal_data())
        mc.write_client_data("Enavra", _minimal_data())
        mc.write_client_data("MyPlace", _minimal_data())
        assert mc.get_generation("Enavra") == 2
        assert mc.get_generation("MyPlace") == 1


# ===========================================================================
# write_client_data
# ===========================================================================

class TestWriteClientData:
    def test_returns_generation_1_on_first_write(self):
        gen, _, _ = mc.write_client_data("Enavra", _minimal_data())
        assert gen == 1

    def test_strips_nc_backfill(self):
        data = _minimal_data(_nc_backfill=["camp-001", "camp-002"])
        mc.write_client_data("Enavra", data)
        with mc._lock:
            stored = mc._data["Enavra"]
        assert "_nc_backfill" not in stored

    def test_strips_nc_api_key(self):
        data = _minimal_data(_nc_api_key="secret-key")
        mc.write_client_data("Enavra", data)
        with mc._lock:
            stored = mc._data["Enavra"]
        assert "_nc_api_key" not in stored

    def test_returns_backfill_campaign_ids(self):
        data = _minimal_data(_nc_backfill=["camp-001", "camp-002"])
        _, backfill_ids, _ = mc.write_client_data("Enavra", data)
        assert backfill_ids == ["camp-001", "camp-002"]

    def test_returns_api_key(self):
        data = _minimal_data(_nc_api_key="my-api-key")
        _, _, api_key = mc.write_client_data("Enavra", data)
        assert api_key == "my-api-key"

    def test_returns_none_api_key_when_absent(self):
        _, _, api_key = mc.write_client_data("Enavra", _minimal_data())
        assert api_key is None

    def test_returns_empty_list_when_no_backfill(self):
        _, backfill_ids, _ = mc.write_client_data("Enavra", _minimal_data())
        assert backfill_ids == []

    def test_clears_prior_error(self):
        mc.write_error("Enavra", "connection error")
        with mc._lock:
            assert "Enavra" in mc._errors
        mc.write_client_data("Enavra", _minimal_data())
        with mc._lock:
            assert "Enavra" not in mc._errors

    def test_does_not_mutate_caller_dict(self):
        data = _minimal_data(_nc_backfill=["camp-001"])
        original_keys = set(data.keys())
        mc.write_client_data("Enavra", data)
        # Caller's dict should still have _nc_backfill
        assert "_nc_backfill" in data
        assert set(data.keys()) == original_keys

    def test_stored_data_has_correct_fields(self):
        data = _minimal_data(sent_today=1500)
        mc.write_client_data("Enavra", data)
        with mc._lock:
            stored = mc._data["Enavra"]
        assert stored["sent_today"] == 1500


# ===========================================================================
# write_error
# ===========================================================================

class TestWriteError:
    def test_stores_friendly_error(self):
        mc.write_error("Enavra", "HTTP 401: Unauthorized")
        with mc._lock:
            assert mc._errors["Enavra"] == "Authentication failed — API key may be invalid"

    def test_bumps_timestamp(self):
        before = time.time()
        mc.write_error("Enavra", "connection error")
        after = time.time()
        with mc._lock:
            ts = mc._ts["Enavra"]
        assert before <= ts <= after + 1

    def test_preserves_stale_data(self):
        # Write good data first, then an error
        mc.write_client_data("Enavra", _minimal_data(sent_today=999))
        mc.write_error("Enavra", "timeout")
        with mc._lock:
            # Data still present
            assert mc._data["Enavra"]["sent_today"] == 999
            # Error also present
            assert "Enavra" in mc._errors

    def test_accepts_exception_objects(self):
        exc = ValueError("something went wrong")
        mc.write_error("Enavra", exc)
        with mc._lock:
            assert mc._errors["Enavra"]  # non-empty friendly message

    def test_unknown_error_produces_fallback_message(self):
        mc.write_error("Enavra", "completely unrecognized error xyz")
        with mc._lock:
            assert mc._errors["Enavra"] == "Data temporarily unavailable — retrying"


# ===========================================================================
# write_backfill
# ===========================================================================

class TestWriteBackfill:
    def _setup_client_with_campaigns(self, name: str = "Enavra") -> int:
        """Write a client with two active campaigns; return generation."""
        data = _minimal_data(
            not_contacted=10000,
            campaigns=[
                {
                    "id": "camp-001", "status": "active",
                    "not_contacted": 5000, "in_progress": 1000,
                    "total_leads": 20000, "total_completed": 10000, "total_bounced": 500,
                },
                {
                    "id": "camp-002", "status": "active",
                    "not_contacted": 5000, "in_progress": 1000,
                    "total_leads": 18000, "total_completed": 9000, "total_bounced": 400,
                },
            ],
        )
        gen, _, _ = mc.write_client_data(name, data)
        return gen

    def test_returns_true_on_success(self):
        gen = self._setup_client_with_campaigns()
        assert mc.write_backfill("Enavra", gen, {"camp-001": 4000, "camp-002": 3000}) is True

    def test_returns_false_when_generation_stale(self):
        gen = self._setup_client_with_campaigns()
        mc.write_client_data("Enavra", _minimal_data())  # gen now = 2
        assert mc.write_backfill("Enavra", gen, {"camp-001": 4000}) is False

    def test_returns_false_when_client_not_in_cache(self):
        assert mc.write_backfill("NoSuchClient", 1, {"camp-001": 100}) is False

    def test_updates_per_campaign_not_contacted(self):
        gen = self._setup_client_with_campaigns()
        mc.write_backfill("Enavra", gen, {"camp-001": 3500, "camp-002": 2800})
        with mc._lock:
            camps = mc._data["Enavra"]["campaigns"]
        nc_by_id = {c["id"]: c["not_contacted"] for c in camps}
        assert nc_by_id["camp-001"] == 3500
        assert nc_by_id["camp-002"] == 2800

    def test_updates_client_level_not_contacted(self):
        gen = self._setup_client_with_campaigns()
        mc.write_backfill("Enavra", gen, {"camp-001": 3500, "camp-002": 2800})
        with mc._lock:
            assert mc._data["Enavra"]["not_contacted"] == 6300  # 3500 + 2800

    def test_recomputes_in_progress(self):
        gen = self._setup_client_with_campaigns()
        # camp-001: leads=20000, completed=10000, bounced=500, nc=3500
        # camp-002: leads=18000, completed=9000,  bounced=400, nc=2800
        # total_leads=38000, total_completed=19000, total_bounced=900, nc=6300
        # in_progress = max(0, 38000 - 19000 - 900 - 6300) = 11800
        mc.write_backfill("Enavra", gen, {"camp-001": 3500, "camp-002": 2800})
        with mc._lock:
            assert mc._data["Enavra"]["in_progress"] == 11800

    def test_in_progress_clamped_at_zero(self):
        # Set up a case where arithmetic would go negative
        data = _minimal_data(
            campaigns=[{
                "id": "camp-001", "status": "active",
                "not_contacted": 5000,
                "total_leads": 100, "total_completed": 90, "total_bounced": 5,
            }]
        )
        gen, _, _ = mc.write_client_data("Enavra", data)
        # nc=90 would make in_progress = 100 - 90 - 5 - 90 = -85 → clamped to 0
        mc.write_backfill("Enavra", gen, {"camp-001": 90})
        with mc._lock:
            assert mc._data["Enavra"]["in_progress"] == 0

    def test_reclassifies_after_backfill(self):
        # Write data where backfill will push pool_days below warn threshold
        data = _minimal_data(
            not_contacted=50000,  # high NC → green before backfill
            sent_today=2000,
            campaigns=[{
                "id": "camp-001", "status": "active",
                "not_contacted": 50000,
                "total_leads": 100000, "total_completed": 40000, "total_bounced": 1000,
            }],
        )
        gen, _, _ = mc.write_client_data("Enavra", data)
        # Backfill with very low NC → pool_days = 4000/2000 = 2.0 < pool_days_red=3 → RED
        mc.write_backfill("Enavra", gen, {"camp-001": 4000})
        with mc._lock:
            status = mc._data["Enavra"].get("status")
        assert status == "red"

    def test_ignores_campaign_ids_not_in_data(self):
        gen = self._setup_client_with_campaigns()
        # Extra unknown campaign ID should be silently ignored
        result = mc.write_backfill("Enavra", gen, {"camp-999": 1000})
        assert result is True  # didn't crash

    def test_only_active_campaigns_count_toward_nc(self):
        data = _minimal_data(
            campaigns=[
                {
                    "id": "camp-active", "status": "active",
                    "not_contacted": 5000,
                    "total_leads": 10000, "total_completed": 4000, "total_bounced": 200,
                },
                {
                    "id": "camp-paused", "status": "paused",
                    "not_contacted": 9999,
                    "total_leads": 5000, "total_completed": 2000, "total_bounced": 100,
                },
            ]
        )
        gen, _, _ = mc.write_client_data("Enavra", data)
        mc.write_backfill("Enavra", gen, {"camp-active": 3000, "camp-paused": 8000})
        with mc._lock:
            # Client NC = active only = 3000 (paused camp-paused is excluded)
            assert mc._data["Enavra"]["not_contacted"] == 3000


# ===========================================================================
# get_all_monitoring_data — live mode
# ===========================================================================

class TestGetAllMonitoringDataLive:
    def test_unseen_client_returns_loading(self):
        result = mc.get_all_monitoring_data()
        # No data written yet → all clients should be "loading"
        for name, data in result.items():
            assert data["status"] == "loading"
            assert data["error"] == "Loading..."
            assert data["fetched_at"] is None

    def test_error_client_returns_error_status(self):
        mc.write_error("Enavra", "HTTP 401 Unauthorized")
        result = mc.get_all_monitoring_data()
        enavra = result["Enavra"]
        assert enavra["status"] == "error"
        assert "Authentication" in enavra["error"]

    def test_cached_client_returns_enriched_data(self):
        mc.write_client_data("Enavra", _minimal_data(sent_today=2000))
        result = mc.get_all_monitoring_data()
        enavra = result["Enavra"]
        assert enavra["error"] is None
        assert "kpi" in enavra
        assert "thresholds" in enavra
        assert "status" in enavra
        assert enavra["fetched_at"] is not None

    def test_status_set_on_read(self):
        mc.write_client_data("Enavra", _minimal_data())
        result = mc.get_all_monitoring_data()
        assert result["Enavra"]["status"] in ("green", "amber", "red")

    def test_green_client_classification(self):
        mc.write_client_data("Enavra", _minimal_data(
            sent_today=2000, reply_rate_today=2.0, bounce_rate=1.0, not_contacted=50000
        ))
        result = mc.get_all_monitoring_data()
        assert result["Enavra"]["status"] == "green"

    def test_platform_injected_from_registry(self):
        mc.write_client_data("Enavra", _minimal_data())
        result = mc.get_all_monitoring_data()
        assert result["Enavra"]["platform"] == "instantly"

    def test_emailbison_platform_injected(self):
        mc.write_client_data("RankZero", _minimal_data(platform="emailbison"))
        result = mc.get_all_monitoring_data()
        assert result["RankZero"]["platform"] == "emailbison"

    def test_all_9_clients_present(self):
        result = mc.get_all_monitoring_data()
        expected_names = {
            "MyPlace", "SwishFunding", "SmartMatchApp", "HeyReach",
            "Kayse", "Prosperly", "Enavra", "RankZero", "SwishFunding (EB)"
        }
        assert set(result.keys()) == expected_names

    def test_error_takes_precedence_when_no_data(self):
        mc.write_error("Enavra", "connection error")
        result = mc.get_all_monitoring_data()
        assert result["Enavra"]["status"] == "error"

    def test_fetched_at_is_iso_timestamp(self):
        mc.write_client_data("Enavra", _minimal_data())
        result = mc.get_all_monitoring_data()
        fetched_at = result["Enavra"]["fetched_at"]
        assert fetched_at is not None
        # Should parse as ISO 8601
        from datetime import datetime
        datetime.fromisoformat(fetched_at)  # raises if invalid

    def test_reclassifies_on_read_with_new_thresholds(self):
        # Write data that would be green
        mc.write_client_data("Enavra", _minimal_data(bounce_rate=3.5))
        result = mc.get_all_monitoring_data()
        assert result["Enavra"]["status"] == "amber"  # bounce_rate 3.5 > warn=3.0

    def test_kpi_matches_factory(self):
        mc.write_client_data("Enavra", _minimal_data())
        result = mc.get_all_monitoring_data()
        from app.services.monitoring_config import get_client_kpi
        assert result["Enavra"]["kpi"] == get_client_kpi("Enavra")


# ===========================================================================
# Mock mode
# ===========================================================================

class TestMockMode:
    def test_set_mock_mode_true(self):
        mc.set_mock_mode(True)
        assert mc.is_mock_mode() is True

    def test_set_mock_mode_false(self):
        mc.set_mock_mode(True)
        mc.set_mock_mode(False)
        assert mc.is_mock_mode() is False

    def test_default_is_not_mock(self):
        # _reset_for_tests called in fixture — should be False
        assert mc.is_mock_mode() is False

    def test_mock_mode_returns_all_9_clients(self):
        mc.set_mock_mode(True)
        result = mc.get_all_monitoring_data()
        assert len(result) == 9

    def test_mock_mode_swishfunding_is_green(self):
        mc.set_mock_mode(True)
        result = mc.get_all_monitoring_data()
        assert result["SwishFunding"]["status"] == "green"

    def test_mock_mode_smartmatchapp_is_red(self):
        mc.set_mock_mode(True)
        result = mc.get_all_monitoring_data()
        assert result["SmartMatchApp"]["status"] == "red"

    def test_mock_mode_myplace_is_amber(self):
        mc.set_mock_mode(True)
        result = mc.get_all_monitoring_data()
        assert result["MyPlace"]["status"] == "amber"

    def test_mock_mode_prosperly_is_error_card(self):
        # Prosperly fixture has _error field → served as error card
        mc.set_mock_mode(True)
        result = mc.get_all_monitoring_data()
        prosperly = result["Prosperly"]
        assert prosperly["status"] == "error"
        assert prosperly["error"] == "Invalid API key"

    def test_mock_mode_enriches_with_kpi_and_thresholds(self):
        mc.set_mock_mode(True)
        result = mc.get_all_monitoring_data()
        enavra = result["Enavra"]
        assert "kpi" in enavra
        assert "thresholds" in enavra

    def test_mock_mode_fetched_at_is_set(self):
        mc.set_mock_mode(True)
        result = mc.get_all_monitoring_data()
        from datetime import datetime
        for name, data in result.items():
            if data["status"] != "error":
                assert data["fetched_at"] is not None
                datetime.fromisoformat(data["fetched_at"])

    def test_disable_mock_clears_fixture_cache(self):
        mc.set_mock_mode(True)
        mc.get_all_monitoring_data()  # loads fixture into _mock_fixture
        mc.set_mock_mode(False)
        assert mc._mock_fixture is None

    def test_mock_missing_fixture_entry_produces_error_card(self, monkeypatch):
        # Add a fake client to the list returned by list_monitoring_workspaces.
        # Must patch on monitoring_cache (which imported the function directly).
        from app.services.registry import ClientEntry
        fake_entry = ClientEntry(
            name="FakeClient", slug="fakeclient", platform="instantly",
            env_var="INSTANTLY_FAKE", monitoring_enabled=True
        )
        original_fn = mc.list_monitoring_workspaces
        monkeypatch.setattr(
            mc, "list_monitoring_workspaces",
            lambda: original_fn() + [fake_entry]
        )
        mc.set_mock_mode(True)
        result = mc.get_all_monitoring_data()
        assert result["FakeClient"]["status"] == "error"
        assert "Missing from mock fixture" in result["FakeClient"]["error"]


# ===========================================================================
# invalidate_all / invalidate_client
# ===========================================================================

class TestInvalidation:
    def test_invalidate_all_makes_all_clients_stale(self):
        mc.write_client_data("Enavra", _minimal_data())
        mc.write_client_data("MyPlace", _minimal_data())
        assert mc.should_refresh("Enavra") is False
        assert mc.should_refresh("MyPlace") is False
        mc.invalidate_all()
        assert mc.should_refresh("Enavra") is True
        assert mc.should_refresh("MyPlace") is True

    def test_invalidate_client_only_affects_target(self):
        mc.write_client_data("Enavra", _minimal_data())
        mc.write_client_data("MyPlace", _minimal_data())
        mc.invalidate_client("Enavra")
        assert mc.should_refresh("Enavra") is True
        assert mc.should_refresh("MyPlace") is False

    def test_invalidate_client_noop_for_unknown(self):
        # Should not raise
        mc.invalidate_client("DoesNotExist")


# ===========================================================================
# init_cache / config-save hook
# ===========================================================================

class TestInitCache:
    def test_init_cache_registers_hook(self):
        mc.init_cache()
        # Write some fresh data
        mc.write_client_data("Enavra", _minimal_data())
        assert mc.should_refresh("Enavra") is False
        # Saving config should trigger invalidation
        from app.services.monitoring_config import save_config
        save_config({})
        assert mc.should_refresh("Enavra") is True

    def test_init_cache_is_idempotent(self):
        mc.init_cache()
        mc.init_cache()
        # Only one hook should be registered (monitoring_config deduplicates)
        mc.write_client_data("Enavra", _minimal_data())
        from app.services.monitoring_config import save_config
        save_config({})
        assert mc.should_refresh("Enavra") is True


# ===========================================================================
# _reset_for_tests
# ===========================================================================

class TestResetForTests:
    def test_clears_data(self):
        mc.write_client_data("Enavra", _minimal_data())
        mc._reset_for_tests()
        with mc._lock:
            assert mc._data == {}

    def test_clears_timestamps(self):
        mc.write_client_data("Enavra", _minimal_data())
        mc._reset_for_tests()
        with mc._lock:
            assert mc._ts == {}

    def test_clears_errors(self):
        mc.write_error("Enavra", "connection error")
        mc._reset_for_tests()
        with mc._lock:
            assert mc._errors == {}

    def test_clears_generation(self):
        mc.write_client_data("Enavra", _minimal_data())
        mc._reset_for_tests()
        assert mc.get_generation("Enavra") == 0

    def test_clears_mock_fixture(self):
        mc.set_mock_mode(True)
        mc.get_all_monitoring_data()  # populates _mock_fixture
        mc._reset_for_tests()
        assert mc._mock_fixture is None

    def test_resets_mock_mode_to_false(self):
        mc.set_mock_mode(True)
        mc._reset_for_tests()
        assert mc.is_mock_mode() is False

    def test_client_appears_stale_after_reset(self):
        mc.write_client_data("Enavra", _minimal_data())
        mc._reset_for_tests()
        cfg.load_config(config_path=Path("/tmp/no_config_cache_test.json"))
        assert mc.should_refresh("Enavra") is True
