"""Monitoring dashboard configuration — KPI targets, thresholds, resolution.

Ported from the stdlib monitoring dashboard (server.py) into the FastAPI app.
Intentionally separate from `app/config.py` (pydantic-settings) so that
per-client, hand-editable runtime config (thresholds, KPI targets) doesn't
pollute the strongly-typed environment settings.

Resolution priority (per GOAL.md 5.3.1-5.3.4):
    1. `dashboard_config.json` next to the repo root (highest priority)
    2. `DASHBOARD_CONFIG` environment variable (JSON string)
    3. Factory defaults (`KPI_TARGETS` + `FACTORY_THRESHOLDS`)

Side-effect free on import: `load_config()` must be called explicitly by the
app lifespan. Tests can call `_reset_for_tests()` to clear any loaded state.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Factory defaults (GOAL.md 5.1 + 5.2)
# ---------------------------------------------------------------------------

#: Per-client daily KPI targets. Keys must match `ClientEntry.name` in
#: `app/services/registry.py` exactly.
KPI_TARGETS: dict[str, dict[str, float]] = {
    "MyPlace":           {"sent": 2000,  "not_contacted": 1000,  "opps_per_day": 4.0, "reply_rate": 1.5},
    "SwishFunding":      {"sent": 10000, "not_contacted": 10000, "opps_per_day": 9.0, "reply_rate": 1.3},
    "SmartMatchApp":     {"sent": 2000,  "not_contacted": 2000,  "opps_per_day": 2.0, "reply_rate": 1.5},
    "HeyReach":          {"sent": 2000,  "not_contacted": 2000,  "opps_per_day": 2.0, "reply_rate": 1.5},
    "Kayse":             {"sent": 2000,  "not_contacted": 2000,  "opps_per_day": 2.0, "reply_rate": 1.5},
    "Prosperly":         {"sent": 2000,  "not_contacted": 2000,  "opps_per_day": 2.0, "reply_rate": 1.5},
    "Enavra":            {"sent": 2000,  "not_contacted": 2000,  "opps_per_day": 2.0, "reply_rate": 1.5},
    "RankZero":          {"sent": 2000,  "not_contacted": 2000,  "opps_per_day": 2.0, "reply_rate": 1.5},
    "SwishFunding (EB)": {"sent": 2000,  "not_contacted": 2000,  "opps_per_day": 2.0, "reply_rate": 1.5},
}

#: Global alert thresholds (GOAL.md 5.2). Per-client overrides merge on top
#: via `get_client_thresholds()`.
FACTORY_THRESHOLDS: dict[str, float] = {
    "reply_rate_warn":  1.0,   # pct — below → amber
    "reply_rate_red":   0.5,   # pct — below → red
    "sent_pct_warn":    0.8,   # fraction of KPI — below → amber
    "sent_pct_red":     0.5,   # fraction of KPI — below → red
    "bounce_rate_warn": 3.0,   # pct — above → amber
    "bounce_rate_red":  5.0,   # pct — above → red
    "opps_pct_warn":    0.5,   # fraction of 7-day avg — below → amber
    "pool_days_warn":   7,     # days of leads — below → amber
    "pool_days_red":    3,     # days of leads — below → red
}

#: Numeric fields that must appear under `global_thresholds`. Used by
#: `validate_config()` to enforce type safety.
_NUMERIC_THRESHOLD_FIELDS: tuple[str, ...] = (
    "reply_rate_warn", "reply_rate_red",
    "sent_pct_warn", "sent_pct_red",
    "bounce_rate_warn", "bounce_rate_red",
    "opps_pct_warn",
    "pool_days_warn", "pool_days_red",
)

#: Numeric KPI fields under `clients.{name}`.
_NUMERIC_KPI_FIELDS: tuple[str, ...] = ("sent", "not_contacted", "opps_per_day", "reply_rate")


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _default_config_path() -> Path:
    """Return the path to `dashboard_config.json` at the repo root.

    `app/services/monitoring_config.py` → repo root is three parents up.
    """
    return Path(__file__).resolve().parents[2] / "dashboard_config.json"


# ---------------------------------------------------------------------------
# State (set by load_config() in app lifespan; NOT at import time)
# ---------------------------------------------------------------------------

_config_lock = threading.Lock()
_config: dict | None = None

#: Callbacks invoked after a successful `save_config()` so that dependent
#: modules (e.g. `monitoring_cache`) can invalidate derived state. The cache
#: module registers itself during its own initialisation.
_save_hooks: list[Callable[[dict], None]] = []


def register_save_hook(hook: Callable[[dict], None]) -> None:
    """Register a callback to run after `save_config()` persists new config.

    Used by the monitoring cache to force re-classification when thresholds
    change (GOAL.md 5.4.5). Idempotent: registering the same hook twice is a
    no-op.
    """
    if hook not in _save_hooks:
        _save_hooks.append(hook)


def _factory_config() -> dict:
    """Build a fresh factory-defaults config dict.

    Returned dict is fully owned by the caller and safe to mutate.
    """
    return {
        "version": 1,
        "updated_at": "",
        "global_thresholds": dict(FACTORY_THRESHOLDS),
        "clients": {name: dict(kpi) for name, kpi in KPI_TARGETS.items()},
    }


# ---------------------------------------------------------------------------
# Load / save / get
# ---------------------------------------------------------------------------

def load_config(config_path: Path | None = None) -> dict:
    """Load config from disk → env var → factory defaults.

    Called explicitly by the FastAPI app lifespan on startup. Safe to call
    multiple times (each call replaces the in-memory config).

    Args:
        config_path: Override the default `dashboard_config.json` location.
            Primarily for tests. Production code should pass `None`.
    """
    global _config

    path = config_path or _default_config_path()
    factory = _factory_config()

    # 1. Try disk first
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            is_valid, errors = validate_config(data)
            if is_valid:
                with _config_lock:
                    _config = data
                if not os.environ.get("DASHBOARD_CONFIG"):
                    logger.warning(
                        "admin config loaded from disk but DASHBOARD_CONFIG env var is not set. "
                        "Config will be lost on next Render deploy. Use the Export button in /admin to persist."
                    )
                return data
            logger.warning("dashboard_config.json failed validation: %s — trying env var", errors)
        except Exception as exc:  # noqa: BLE001 — config corruption is non-fatal
            logger.warning("failed to read dashboard_config.json (%s) — trying env var", exc)

    # 2. Try DASHBOARD_CONFIG env var
    env_json = os.environ.get("DASHBOARD_CONFIG", "")
    if env_json:
        try:
            data = json.loads(env_json)
            is_valid, errors = validate_config(data)
            if is_valid:
                with _config_lock:
                    _config = data
                return data
            logger.warning("DASHBOARD_CONFIG env var failed validation: %s — using factory defaults", errors)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to parse DASHBOARD_CONFIG env var (%s) — using factory defaults", exc)

    # 3. Factory defaults
    with _config_lock:
        _config = factory
    return factory


def save_config(cfg: dict, config_path: Path | None = None) -> None:
    """Atomically persist `cfg` to disk and swap in-memory state.

    Writes to `<path>.tmp` then renames — safe against torn writes and
    crash-consistent on POSIX (GOAL.md 5.4.4). After the swap, invokes every
    hook registered via `register_save_hook()` so that dependent caches can
    invalidate.

    Does NOT validate — caller is responsible for calling `validate_config()`
    first (admin panel route enforces this).
    """
    global _config
    path = config_path or _default_config_path()
    tmp_path = path.with_suffix(".tmp")

    cfg.setdefault("version", 1)
    cfg["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()

    payload = json.dumps(cfg, indent=2)
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(path)  # atomic on POSIX and Windows (Python >= 3.3)

    with _config_lock:
        _config = cfg

    for hook in list(_save_hooks):
        try:
            hook(cfg)
        except Exception:  # noqa: BLE001 — one broken hook must not block others
            logger.exception("monitoring_config save hook failed")


def get_config() -> dict:
    """Return the currently loaded config.

    Raises `RuntimeError` if `load_config()` has not yet been called — this is
    a deliberate failure mode so that lifespan-ordering bugs surface loudly
    instead of silently using factory defaults.
    """
    with _config_lock:
        if _config is None:
            raise RuntimeError(
                "monitoring_config.load_config() must be called before get_config() "
                "(typically from the FastAPI app lifespan)"
            )
        return _config


def get_client_kpi(name: str) -> dict:
    """Return KPI targets for `name`, falling back to factory defaults.

    Strips the `thresholds` sub-key so callers get a flat KPI dict.
    """
    cfg = get_config()
    client_cfg = cfg.get("clients", {}).get(name, {})
    if client_cfg:
        return {k: v for k, v in client_cfg.items() if k != "thresholds"}
    return dict(KPI_TARGETS.get(name, {}))


def get_client_thresholds(name: str) -> dict:
    """Resolve thresholds for `name`: factory → global → per-client overrides.

    Later entries override earlier ones, so per-client overrides always win.
    """
    cfg = get_config()
    global_t = cfg.get("global_thresholds", {})
    client_t = cfg.get("clients", {}).get(name, {}).get("thresholds", {})
    return {**FACTORY_THRESHOLDS, **global_t, **client_t}


# ---------------------------------------------------------------------------
# Validation (GOAL.md 5.4.1-5.4.3)
# ---------------------------------------------------------------------------

def validate_config(data: dict) -> tuple[bool, list[str]]:
    """Validate config shape + type + threshold consistency.

    Returns `(is_valid, errors)`. Consistency checks only run on pairs where
    both sides are numeric — a non-numeric value short-circuits the pair so
    callers see a single "must be numeric" error instead of a cascade.

    Consistency rules (GOAL.md 5.4.3):
        - reply_rate_warn  >  reply_rate_red
        - sent_pct_warn    >  sent_pct_red
        - bounce_rate_warn <  bounce_rate_red
        - pool_days_warn   >  pool_days_red
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return False, ["config must be a JSON object"]

    # --- global_thresholds ---
    gt = data.get("global_thresholds", {})
    if not isinstance(gt, dict):
        errors.append("global_thresholds must be an object")
    else:
        non_numeric: set[str] = set()
        for field in _NUMERIC_THRESHOLD_FIELDS:
            if field in gt and not isinstance(gt[field], (int, float)):
                errors.append(f"global_thresholds.{field} must be numeric")
                non_numeric.add(field)

        def _pair_ok(a: str, b: str) -> bool:
            return a in gt and b in gt and not (non_numeric & {a, b})

        if _pair_ok("reply_rate_warn", "reply_rate_red") and gt["reply_rate_warn"] <= gt["reply_rate_red"]:
            errors.append("reply_rate_warn must be > reply_rate_red")
        if _pair_ok("sent_pct_warn", "sent_pct_red") and gt["sent_pct_warn"] <= gt["sent_pct_red"]:
            errors.append("sent_pct_warn must be > sent_pct_red")
        if _pair_ok("bounce_rate_warn", "bounce_rate_red") and gt["bounce_rate_warn"] >= gt["bounce_rate_red"]:
            errors.append("bounce_rate_warn must be < bounce_rate_red")
        if _pair_ok("pool_days_warn", "pool_days_red") and gt["pool_days_warn"] <= gt["pool_days_red"]:
            errors.append("pool_days_warn must be > pool_days_red")

    # --- clients ---
    clients = data.get("clients", {})
    if not isinstance(clients, dict):
        errors.append("clients must be an object")
    else:
        for cname, ccfg in clients.items():
            if not isinstance(ccfg, dict):
                errors.append(f"clients.{cname} must be an object")
                continue
            for field in _NUMERIC_KPI_FIELDS:
                if field in ccfg and not isinstance(ccfg[field], (int, float)):
                    errors.append(f"clients.{cname}.{field} must be numeric")
            cthresh = ccfg.get("thresholds", {})
            if not isinstance(cthresh, dict):
                errors.append(f"clients.{cname}.thresholds must be an object")
            else:
                for field, value in cthresh.items():
                    if not isinstance(value, (int, float)):
                        errors.append(f"clients.{cname}.thresholds.{field} must be numeric")

    return (len(errors) == 0), errors


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _reset_for_tests() -> None:
    """Clear loaded config and save hooks. For use by test fixtures only."""
    global _config
    with _config_lock:
        _config = None
    _save_hooks.clear()
