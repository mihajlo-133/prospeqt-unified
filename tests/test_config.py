"""Tests for app/services/monitoring_config.py.

Covers:
- KPI_TARGETS and FACTORY_THRESHOLDS match GOAL.md 5.1 + 5.2 exactly
- load_config() resolution priority: file → env → factory
- save_config() atomic write (.tmp + rename)
- validate_config() consistency rules (5.4.1-5.4.3)
- get_client_kpi() merges config overrides on top of factory
- get_client_thresholds() merges factory → global → per-client overrides
"""
import json
import os
from pathlib import Path

import pytest

from app.services.monitoring_config import (
    FACTORY_THRESHOLDS,
    KPI_TARGETS,
    _reset_for_tests,
    get_client_kpi,
    get_client_thresholds,
    load_config,
    save_config,
    validate_config,
)


@pytest.fixture(autouse=True)
def reset_config():
    """Reset module-level config state before and after every test."""
    _reset_for_tests()
    yield
    _reset_for_tests()


# ---------------------------------------------------------------------------
# Factory defaults (GOAL.md 5.1 + 5.2)
# ---------------------------------------------------------------------------


class TestKpiTargets:
    def test_all_nine_clients_present(self):
        expected = {
            "MyPlace", "SwishFunding", "SmartMatchApp", "HeyReach",
            "Kayse", "Prosperly", "Enavra", "RankZero", "SwishFunding (EB)",
        }
        assert set(KPI_TARGETS.keys()) == expected

    def test_swishfunding_kpi(self):
        kpi = KPI_TARGETS["SwishFunding"]
        assert kpi["sent"] == 10000
        assert kpi["not_contacted"] == 10000
        assert kpi["opps_per_day"] == 9.0
        assert kpi["reply_rate"] == 1.3

    def test_myplace_kpi(self):
        kpi = KPI_TARGETS["MyPlace"]
        assert kpi["sent"] == 2000
        assert kpi["not_contacted"] == 1000
        assert kpi["opps_per_day"] == 4.0
        assert kpi["reply_rate"] == 1.5

    def test_standard_clients_kpi(self):
        """SmartMatchApp, HeyReach, Kayse, Prosperly, Enavra, RankZero, SwishFunding (EB)
        all share the same default KPI structure."""
        standard_clients = [
            "SmartMatchApp", "HeyReach", "Kayse", "Prosperly",
            "Enavra", "RankZero", "SwishFunding (EB)",
        ]
        for name in standard_clients:
            kpi = KPI_TARGETS[name]
            assert kpi["sent"] == 2000, f"{name}: sent should be 2000"
            assert kpi["not_contacted"] == 2000, f"{name}: not_contacted should be 2000"
            assert kpi["opps_per_day"] == 2.0, f"{name}: opps_per_day should be 2.0"
            assert kpi["reply_rate"] == 1.5, f"{name}: reply_rate should be 1.5"

    def test_kpi_has_required_keys(self):
        required = {"sent", "not_contacted", "opps_per_day", "reply_rate"}
        for name, kpi in KPI_TARGETS.items():
            assert set(kpi.keys()) >= required, f"{name} missing KPI keys"


class TestFactoryThresholds:
    def test_all_nine_threshold_keys_present(self):
        expected = {
            "reply_rate_warn", "reply_rate_red",
            "sent_pct_warn", "sent_pct_red",
            "bounce_rate_warn", "bounce_rate_red",
            "opps_pct_warn",
            "pool_days_warn", "pool_days_red",
        }
        assert set(FACTORY_THRESHOLDS.keys()) == expected

    def test_reply_rate_thresholds(self):
        assert FACTORY_THRESHOLDS["reply_rate_warn"] == 1.0
        assert FACTORY_THRESHOLDS["reply_rate_red"] == 0.5

    def test_sent_pct_thresholds(self):
        assert FACTORY_THRESHOLDS["sent_pct_warn"] == 0.8
        assert FACTORY_THRESHOLDS["sent_pct_red"] == 0.5

    def test_bounce_rate_thresholds(self):
        assert FACTORY_THRESHOLDS["bounce_rate_warn"] == 3.0
        assert FACTORY_THRESHOLDS["bounce_rate_red"] == 5.0

    def test_pool_days_thresholds(self):
        assert FACTORY_THRESHOLDS["pool_days_warn"] == 7
        assert FACTORY_THRESHOLDS["pool_days_red"] == 3

    def test_opps_pct_warn(self):
        assert FACTORY_THRESHOLDS["opps_pct_warn"] == 0.5

    def test_factory_thresholds_pass_their_own_consistency_rules(self):
        """The factory defaults themselves must not violate the validation rules."""
        fake_cfg = {"global_thresholds": FACTORY_THRESHOLDS}
        is_valid, errors = validate_config(fake_cfg)
        assert is_valid, f"Factory thresholds violate their own consistency rules: {errors}"


# ---------------------------------------------------------------------------
# load_config() — resolution priority
# ---------------------------------------------------------------------------


class TestLoadConfigPriority:
    def test_factory_defaults_when_no_file_no_env(self):
        """With no config file and no env var, load_config() returns factory defaults."""
        cfg = load_config(config_path=Path("/tmp/nonexistent_prospeqt_config_xyz.json"))
        assert cfg["global_thresholds"]["reply_rate_warn"] == FACTORY_THRESHOLDS["reply_rate_warn"]
        assert "MyPlace" in cfg["clients"]

    def test_env_var_takes_priority_over_factory(self, monkeypatch):
        """DASHBOARD_CONFIG env var is loaded when no config file exists."""
        env_cfg = {
            "global_thresholds": {"reply_rate_warn": 2.5, "reply_rate_red": 1.0},
            "clients": {},
        }
        monkeypatch.setenv("DASHBOARD_CONFIG", json.dumps(env_cfg))
        cfg = load_config(config_path=Path("/tmp/nonexistent_prospeqt_config_xyz.json"))
        assert cfg["global_thresholds"]["reply_rate_warn"] == 2.5

    def test_file_takes_priority_over_env_var(self, monkeypatch, tmp_path):
        """Config file takes priority over DASHBOARD_CONFIG env var."""
        file_cfg = {
            "global_thresholds": {"reply_rate_warn": 3.0, "reply_rate_red": 1.5},
            "clients": {},
        }
        env_cfg = {
            "global_thresholds": {"reply_rate_warn": 99.0},
            "clients": {},
        }
        config_file = tmp_path / "dashboard_config.json"
        config_file.write_text(json.dumps(file_cfg), encoding="utf-8")
        monkeypatch.setenv("DASHBOARD_CONFIG", json.dumps(env_cfg))

        cfg = load_config(config_path=config_file)
        # File value (3.0) should win over env var value (99.0)
        assert cfg["global_thresholds"]["reply_rate_warn"] == 3.0

    def test_invalid_file_falls_through_to_env(self, monkeypatch, tmp_path):
        """If config file fails validation, env var is tried next."""
        bad_cfg = {
            "global_thresholds": {
                "reply_rate_warn": 0.1,  # < reply_rate_red — invalid
                "reply_rate_red": 0.5,
            },
            "clients": {},
        }
        env_cfg = {
            "global_thresholds": {"reply_rate_warn": 2.0, "reply_rate_red": 0.5},
            "clients": {},
        }
        config_file = tmp_path / "dashboard_config.json"
        config_file.write_text(json.dumps(bad_cfg), encoding="utf-8")
        monkeypatch.setenv("DASHBOARD_CONFIG", json.dumps(env_cfg))

        cfg = load_config(config_path=config_file)
        assert cfg["global_thresholds"]["reply_rate_warn"] == 2.0

    def test_invalid_env_falls_through_to_factory(self, monkeypatch):
        """If DASHBOARD_CONFIG fails validation, factory defaults are used."""
        bad_cfg = {
            "global_thresholds": {
                "reply_rate_warn": 0.1,  # < reply_rate_red — invalid
                "reply_rate_red": 0.5,
            },
            "clients": {},
        }
        monkeypatch.setenv("DASHBOARD_CONFIG", json.dumps(bad_cfg))
        cfg = load_config(config_path=Path("/tmp/nonexistent_prospeqt_config_xyz.json"))
        assert cfg["global_thresholds"]["reply_rate_warn"] == FACTORY_THRESHOLDS["reply_rate_warn"]

    def test_malformed_json_in_env_falls_through_to_factory(self, monkeypatch):
        """Malformed JSON in DASHBOARD_CONFIG falls through to factory defaults."""
        monkeypatch.setenv("DASHBOARD_CONFIG", "not valid json {{")
        cfg = load_config(config_path=Path("/tmp/nonexistent_prospeqt_config_xyz.json"))
        assert cfg["global_thresholds"]["reply_rate_warn"] == FACTORY_THRESHOLDS["reply_rate_warn"]

    def test_load_config_is_idempotent(self, tmp_path):
        """Calling load_config() twice replaces in-memory state cleanly."""
        cfg1 = {
            "global_thresholds": {"reply_rate_warn": 1.5, "reply_rate_red": 0.5},
            "clients": {},
        }
        cfg2 = {
            "global_thresholds": {"reply_rate_warn": 2.0, "reply_rate_red": 1.0},
            "clients": {},
        }
        p1 = tmp_path / "cfg1.json"
        p1.write_text(json.dumps(cfg1), encoding="utf-8")
        load_config(config_path=p1)

        p2 = tmp_path / "cfg2.json"
        p2.write_text(json.dumps(cfg2), encoding="utf-8")
        result = load_config(config_path=p2)
        assert result["global_thresholds"]["reply_rate_warn"] == 2.0


# ---------------------------------------------------------------------------
# save_config() — atomic write
# ---------------------------------------------------------------------------


class TestSaveConfig:
    def test_save_writes_to_disk(self, tmp_path):
        config_path = tmp_path / "dashboard_config.json"
        cfg = {"global_thresholds": {}, "clients": {}}
        load_config(config_path=Path("/tmp/nonexistent_prospeqt_config_xyz.json"))
        save_config(cfg, config_path=config_path)
        assert config_path.exists()

    def test_save_no_tmp_file_left(self, tmp_path):
        """After save, no .tmp file should remain."""
        config_path = tmp_path / "dashboard_config.json"
        tmp_file = config_path.with_suffix(".tmp")
        cfg = {"global_thresholds": {}, "clients": {}}
        load_config(config_path=Path("/tmp/nonexistent_prospeqt_config_xyz.json"))
        save_config(cfg, config_path=config_path)
        assert not tmp_file.exists()

    def test_save_content_is_readable_json(self, tmp_path):
        config_path = tmp_path / "dashboard_config.json"
        cfg = {
            "global_thresholds": {"reply_rate_warn": 1.5, "reply_rate_red": 0.5},
            "clients": {"TestClient": {"sent": 3000}},
        }
        load_config(config_path=Path("/tmp/nonexistent_prospeqt_config_xyz.json"))
        save_config(cfg, config_path=config_path)

        written = json.loads(config_path.read_text(encoding="utf-8"))
        assert written["global_thresholds"]["reply_rate_warn"] == 1.5
        assert written["clients"]["TestClient"]["sent"] == 3000

    def test_save_sets_updated_at(self, tmp_path):
        config_path = tmp_path / "dashboard_config.json"
        cfg = {"global_thresholds": {}, "clients": {}}
        load_config(config_path=Path("/tmp/nonexistent_prospeqt_config_xyz.json"))
        save_config(cfg, config_path=config_path)

        written = json.loads(config_path.read_text(encoding="utf-8"))
        assert "updated_at" in written
        assert written["updated_at"] != ""

    def test_save_invokes_registered_hook(self, tmp_path):
        config_path = tmp_path / "dashboard_config.json"
        called_with = []

        from app.services.monitoring_config import register_save_hook
        register_save_hook(lambda c: called_with.append(c))

        cfg = {"global_thresholds": {}, "clients": {}}
        load_config(config_path=Path("/tmp/nonexistent_prospeqt_config_xyz.json"))
        save_config(cfg, config_path=config_path)

        assert len(called_with) == 1
        assert called_with[0]["clients"] == {}


# ---------------------------------------------------------------------------
# validate_config() — type + consistency rules
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_valid_config_passes(self):
        cfg = {
            "global_thresholds": {
                "reply_rate_warn": 1.0,
                "reply_rate_red": 0.5,
                "sent_pct_warn": 0.8,
                "sent_pct_red": 0.5,
                "bounce_rate_warn": 3.0,
                "bounce_rate_red": 5.0,
                "pool_days_warn": 7,
                "pool_days_red": 3,
            },
            "clients": {"TestClient": {"sent": 2000, "not_contacted": 1000}},
        }
        is_valid, errors = validate_config(cfg)
        assert is_valid is True
        assert errors == []

    def test_empty_config_is_valid(self):
        is_valid, errors = validate_config({})
        assert is_valid is True
        assert errors == []

    def test_non_dict_is_invalid(self):
        is_valid, errors = validate_config("not a dict")
        assert is_valid is False
        assert any("JSON object" in e for e in errors)

    def test_non_numeric_threshold_fails(self):
        cfg = {
            "global_thresholds": {"reply_rate_warn": "bad_string"},
            "clients": {},
        }
        is_valid, errors = validate_config(cfg)
        assert is_valid is False
        assert any("reply_rate_warn" in e and "numeric" in e for e in errors)

    def test_reply_rate_warn_must_exceed_red(self):
        cfg = {
            "global_thresholds": {"reply_rate_warn": 0.3, "reply_rate_red": 0.5},
            "clients": {},
        }
        is_valid, errors = validate_config(cfg)
        assert is_valid is False
        assert any("reply_rate_warn" in e and "reply_rate_red" in e for e in errors)

    def test_sent_pct_warn_must_exceed_red(self):
        cfg = {
            "global_thresholds": {"sent_pct_warn": 0.4, "sent_pct_red": 0.5},
            "clients": {},
        }
        is_valid, errors = validate_config(cfg)
        assert is_valid is False
        assert any("sent_pct_warn" in e and "sent_pct_red" in e for e in errors)

    def test_bounce_rate_warn_must_be_less_than_red(self):
        cfg = {
            "global_thresholds": {"bounce_rate_warn": 5.0, "bounce_rate_red": 5.0},
            "clients": {},
        }
        is_valid, errors = validate_config(cfg)
        assert is_valid is False
        assert any("bounce_rate_warn" in e and "bounce_rate_red" in e for e in errors)

    def test_pool_days_warn_must_exceed_red(self):
        cfg = {
            "global_thresholds": {"pool_days_warn": 2, "pool_days_red": 3},
            "clients": {},
        }
        is_valid, errors = validate_config(cfg)
        assert is_valid is False
        assert any("pool_days_warn" in e and "pool_days_red" in e for e in errors)

    def test_equal_reply_rate_warn_red_is_invalid(self):
        """warn == red should also fail (must be strictly greater)."""
        cfg = {
            "global_thresholds": {"reply_rate_warn": 0.5, "reply_rate_red": 0.5},
            "clients": {},
        }
        is_valid, errors = validate_config(cfg)
        assert is_valid is False

    def test_client_non_numeric_kpi_fails(self):
        cfg = {
            "global_thresholds": {},
            "clients": {"BadClient": {"sent": "not_a_number"}},
        }
        is_valid, errors = validate_config(cfg)
        assert is_valid is False
        assert any("BadClient" in e and "sent" in e for e in errors)

    def test_client_threshold_override_non_numeric_fails(self):
        cfg = {
            "global_thresholds": {},
            "clients": {"BadClient": {"thresholds": {"reply_rate_warn": "bad"}}},
        }
        is_valid, errors = validate_config(cfg)
        assert is_valid is False
        assert any("BadClient" in e and "reply_rate_warn" in e for e in errors)

    def test_clients_not_dict_fails(self):
        cfg = {"global_thresholds": {}, "clients": "not_a_dict"}
        is_valid, errors = validate_config(cfg)
        assert is_valid is False
        assert any("clients" in e and "object" in e for e in errors)

    def test_multiple_errors_returned(self):
        """validate_config returns ALL errors, not just the first."""
        cfg = {
            "global_thresholds": {
                "reply_rate_warn": "bad",   # non-numeric
                "sent_pct_warn": "bad",     # non-numeric
            },
            "clients": {},
        }
        is_valid, errors = validate_config(cfg)
        assert is_valid is False
        assert len(errors) >= 2

    def test_non_numeric_blocks_consistency_check_for_that_pair(self):
        """If one side of a pair is non-numeric, no consistency error for that pair."""
        cfg = {
            "global_thresholds": {
                "reply_rate_warn": "bad",  # non-numeric — consistency check must not fire
                "reply_rate_red": 0.5,
            },
            "clients": {},
        }
        is_valid, errors = validate_config(cfg)
        assert is_valid is False
        # Should see type error, but NOT a warn>red consistency error
        assert any("reply_rate_warn" in e and "numeric" in e for e in errors)
        assert not any("reply_rate_warn must be >" in e for e in errors)


# ---------------------------------------------------------------------------
# get_client_kpi() — merge behaviour
# ---------------------------------------------------------------------------


class TestGetClientKpi:
    def test_known_client_returns_factory_kpi_when_no_config_override(self):
        load_config(config_path=Path("/tmp/nonexistent_prospeqt_config_xyz.json"))
        kpi = get_client_kpi("MyPlace")
        assert kpi["sent"] == KPI_TARGETS["MyPlace"]["sent"]
        assert kpi["opps_per_day"] == KPI_TARGETS["MyPlace"]["opps_per_day"]

    def test_unknown_client_returns_empty(self):
        load_config(config_path=Path("/tmp/nonexistent_prospeqt_config_xyz.json"))
        kpi = get_client_kpi("NonExistentClient")
        assert kpi == {}

    def test_config_override_replaces_kpi_value(self, tmp_path):
        cfg = {
            "global_thresholds": {},
            "clients": {
                "MyPlace": {"sent": 9999, "not_contacted": 1000, "opps_per_day": 4.0, "reply_rate": 1.5},
            },
        }
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        load_config(config_path=p)

        kpi = get_client_kpi("MyPlace")
        assert kpi["sent"] == 9999

    def test_thresholds_sub_key_not_in_returned_kpi(self, tmp_path):
        """get_client_kpi strips the 'thresholds' sub-key from the client config."""
        cfg = {
            "global_thresholds": {},
            "clients": {
                "MyPlace": {
                    "sent": 2000, "not_contacted": 1000, "opps_per_day": 4.0, "reply_rate": 1.5,
                    "thresholds": {"reply_rate_warn": 2.0},
                },
            },
        }
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        load_config(config_path=p)

        kpi = get_client_kpi("MyPlace")
        assert "thresholds" not in kpi


# ---------------------------------------------------------------------------
# get_client_thresholds() — merge behaviour
# ---------------------------------------------------------------------------


class TestGetClientThresholds:
    def test_factory_defaults_for_unknown_client(self):
        load_config(config_path=Path("/tmp/nonexistent_prospeqt_config_xyz.json"))
        thresholds = get_client_thresholds("NonExistentClient")
        for key, val in FACTORY_THRESHOLDS.items():
            assert thresholds[key] == val, f"Mismatch for {key}"

    def test_all_factory_keys_always_present(self):
        load_config(config_path=Path("/tmp/nonexistent_prospeqt_config_xyz.json"))
        thresholds = get_client_thresholds("MyPlace")
        for key in FACTORY_THRESHOLDS:
            assert key in thresholds

    def test_global_threshold_overrides_factory(self, tmp_path):
        cfg = {
            "global_thresholds": {"reply_rate_warn": 2.0, "reply_rate_red": 1.0},
            "clients": {},
        }
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        load_config(config_path=p)

        thresholds = get_client_thresholds("MyPlace")
        assert thresholds["reply_rate_warn"] == 2.0
        assert thresholds["reply_rate_red"] == 1.0
        # Keys not in global override still come from factory
        assert thresholds["bounce_rate_warn"] == FACTORY_THRESHOLDS["bounce_rate_warn"]

    def test_per_client_threshold_overrides_global(self, tmp_path):
        cfg = {
            "global_thresholds": {"reply_rate_warn": 2.0, "reply_rate_red": 1.0},
            "clients": {
                "MyPlace": {
                    "sent": 2000,
                    "thresholds": {"reply_rate_warn": 3.0},  # per-client override
                },
            },
        }
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        load_config(config_path=p)

        thresholds = get_client_thresholds("MyPlace")
        assert thresholds["reply_rate_warn"] == 3.0  # per-client wins
        assert thresholds["reply_rate_red"] == 1.0   # global still applies
        assert thresholds["bounce_rate_warn"] == FACTORY_THRESHOLDS["bounce_rate_warn"]  # factory fallback

    def test_merge_order_is_factory_then_global_then_per_client(self, tmp_path):
        """Explicit three-layer merge: factory → global → per-client."""
        cfg = {
            "global_thresholds": {
                "reply_rate_warn": 2.0,     # overrides factory 1.0
                "sent_pct_warn": 0.9,       # overrides factory 0.8
            },
            "clients": {
                "Kayse": {
                    "sent": 2000,
                    "thresholds": {
                        "sent_pct_warn": 0.95,  # overrides global 0.9
                    },
                },
            },
        }
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        load_config(config_path=p)

        thresholds = get_client_thresholds("Kayse")
        assert thresholds["reply_rate_warn"] == 2.0   # global beats factory
        assert thresholds["sent_pct_warn"] == 0.95    # per-client beats global
        assert thresholds["pool_days_warn"] == FACTORY_THRESHOLDS["pool_days_warn"]  # factory for rest
