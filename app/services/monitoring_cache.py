"""In-memory monitoring cache — the thing the scheduler writes and the UI reads.

Ported from `gtm/prospeqt-outreach-dashboard/server.py` (lines 950-1135) with
three structural changes:

  1. **Sync API, async-friendly.** The cache uses a `threading.Lock` so the
     APScheduler async jobs (T7) AND any sync code paths (admin panel HTTP
     handlers) can share it. Acquires are tiny (O(1) dict lookups), no
     blocking I/O under the lock.

  2. **Pure state layer, no scheduling.** T7 owns the "when". T6 owns the
     "what". Scheduler calls `should_refresh(name)` → fetches via T4/T5 →
     writes via `write_client_data(name, result)` → receives the new
     generation number back → schedules Phase-2 backfill → calls
     `write_backfill(name, generation, {cid: count})`.

  3. **Mock mode decoupled from HTTP.** `MOCK_MODE=1` env var (or explicit
     `set_mock_mode(True)`) causes `get_all_monitoring_data()` to serve
     the fixture instead of live data. The scheduler skips fetching
     entirely when mock mode is on (T7's responsibility to check).

Internal `_nc_backfill` / `_nc_api_key` keys returned by
`fetch_instantly_data` are stripped before storing, so the UI contract
stays clean. `write_client_data` returns them to the caller for Phase 2.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.monitoring import _classify_client, _friendly_error
from app.services.monitoring_config import (
    get_client_kpi,
    get_client_thresholds,
    register_save_hook,
)
from app.services.registry import get_client, list_monitoring_workspaces

logger = logging.getLogger(__name__)

#: Cache TTL in seconds (GOAL.md 4.3.6).
CACHE_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Internal state — all reads/writes guarded by `_lock`
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_data: dict[str, dict] = {}           # client_name → ClientData dict (stripped of internal keys)
_ts: dict[str, float] = {}            # client_name → epoch seconds of last successful fetch OR error
_errors: dict[str, str] = {}          # client_name → friendly error message (present iff last fetch failed)
_generation: dict[str, int] = {}      # client_name → monotonic counter for stale-backfill detection

#: Mock mode flag. Set from `MOCK_MODE=1` env var at module import OR
#: explicitly via `set_mock_mode(True)`. Respecting the env var on import
#: makes `pytest` + `python -m app.main --mock` Just Work without extra
#: plumbing.
_mock_mode: bool = os.environ.get("MOCK_MODE", "") in ("1", "true", "yes")

#: Cached mock fixture payload. Lazy-loaded on first `get_all_monitoring_data()`
#: call in mock mode so that file-system work doesn't happen at import time.
_mock_fixture: dict | None = None

#: Path to the mock fixture. Resolves to `<repo>/tests/fixtures/mock_monitoring_data.json`.
_MOCK_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "mock_monitoring_data.json"


# ---------------------------------------------------------------------------
# Mock mode controls
# ---------------------------------------------------------------------------

def set_mock_mode(enabled: bool) -> None:
    """Enable or disable mock mode at runtime. Called by CLI `--mock` flag."""
    global _mock_mode, _mock_fixture
    _mock_mode = enabled
    if not enabled:
        _mock_fixture = None  # free the cached copy


def is_mock_mode() -> bool:
    """Return True if the cache is serving mock data."""
    return _mock_mode


def _load_mock_fixture() -> dict:
    """Load `mock_monitoring_data.json`, caching the result."""
    global _mock_fixture
    if _mock_fixture is None:
        _mock_fixture = json.loads(_MOCK_FIXTURE_PATH.read_text(encoding="utf-8"))
    return _mock_fixture


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def should_refresh(client_name: str) -> bool:
    """Return True if this client's cached data is older than CACHE_TTL.

    Clients never fetched before (no timestamp) always need a refresh.
    """
    with _lock:
        ts = _ts.get(client_name, 0)
    return (time.time() - ts) > CACHE_TTL


def get_generation(client_name: str) -> int:
    """Return the current generation counter for `client_name` (0 if unseen)."""
    with _lock:
        return _generation.get(client_name, 0)


# ---------------------------------------------------------------------------
# Writes (called by T7 scheduler job)
# ---------------------------------------------------------------------------

def write_client_data(client_name: str, result: dict) -> tuple[int, list[str], str | None]:
    """Write Phase 1 fetch result to the cache.

    Strips internal `_nc_backfill` / `_nc_api_key` keys from `result` so the
    cached dict matches the public ClientData contract. Increments the
    generation counter and clears any prior error state.

    Returns `(new_generation, backfill_campaign_ids, backfill_api_key)`. The
    scheduler uses these to kick off Phase 2 backfill with the generation
    guard — if a newer write lands before backfill completes, `write_backfill`
    will discard the stale results.
    """
    # Deep-copy so backfill mutations don't leak back into the caller's
    # local reference, AND so tests that reuse the same fixture dict don't
    # see cross-test contamination through the shared `campaigns` list.
    data = copy.deepcopy(result)
    nc_backfill: list[str] = list(data.pop("_nc_backfill", []) or [])
    nc_api_key: str | None = data.pop("_nc_api_key", None)

    with _lock:
        _data[client_name] = data
        _ts[client_name] = time.time()
        _errors.pop(client_name, None)
        _generation[client_name] = _generation.get(client_name, 0) + 1
        gen = _generation[client_name]
    return gen, nc_backfill, nc_api_key


def write_error(client_name: str, raw_error: str | Exception) -> None:
    """Record a fetch failure as a friendly error string on the cache entry.

    The error is stored alongside (not instead of) any previously-cached
    data — the UI can decide whether to show the stale data with a warning
    or swap in the error state. The timestamp is still bumped so that
    `should_refresh` won't hammer a failing client every 60 seconds.
    """
    msg = str(raw_error)
    friendly = _friendly_error(msg)
    with _lock:
        _errors[client_name] = friendly
        _ts[client_name] = time.time()


def write_backfill(
    client_name: str,
    generation: int,
    nc_by_campaign: dict[str, int],
) -> bool:
    """Apply Phase 2 ground-truth not_contacted counts with generation guard.

    Rejects the write (returns `False`) if a newer Phase 1 fetch has
    incremented the generation counter since this backfill was dispatched —
    that's the race protection from GOAL.md 4.4.4.

    On success, updates per-campaign `not_contacted`, recomputes client-level
    `not_contacted` (active campaigns only) and `in_progress` (leads − completed
    − bounced − nc, clamped ≥0), and re-classifies the client because those
    two fields feed into the 10-rule waterfall.
    """
    with _lock:
        current_gen = _generation.get(client_name, 0)
        if current_gen != generation:
            logger.debug(
                "backfill for %s dropped: generation %d stale (current %d)",
                client_name, generation, current_gen,
            )
            return False

        data = _data.get(client_name)
        if not data:
            return False

        # Update per-campaign not_contacted counts in place
        for camp in data.get("campaigns", []):
            cid = camp.get("id")
            if cid in nc_by_campaign:
                camp["not_contacted"] = nc_by_campaign[cid]

        active_camps = [c for c in data.get("campaigns", []) if c.get("status") == "active"]

        # Client-level not_contacted = sum over active campaigns only
        data["not_contacted"] = sum(c.get("not_contacted", 0) for c in active_camps)

        # in_progress = leads − completed − bounced − not_contacted (active, clamped)
        total_leads     = sum(c.get("total_leads", 0) for c in active_camps)
        total_completed = sum(c.get("total_completed", 0) for c in active_camps)
        total_bounced   = sum(c.get("total_bounced", 0) for c in active_camps)
        data["in_progress"] = max(0, total_leads - total_completed - total_bounced - data["not_contacted"])

        # Re-classify because not_contacted → pool_days changed
        data["status"] = _classify_client(data, client_name)

    return True


# ---------------------------------------------------------------------------
# Read (called by routes)
# ---------------------------------------------------------------------------

def get_all_monitoring_data() -> dict[str, dict]:
    """Return enriched cached data for all monitoring clients. Never blocks.

    For each client in the registry's `list_monitoring_workspaces()`:
      * If mock mode → return the fixture entry (enriched).
      * Else if an error is cached → return `{error, status: "error", ...}`.
      * Else if fresh data is cached → return it (enriched).
      * Else → return `{error: "Loading...", status: "loading", ...}` so the
        UI can show a skeleton while the first fetch completes.

    Enrichment adds (per GOAL.md 3.x and 5.x):
      * `kpi`         — resolved via `get_client_kpi(name)`
      * `thresholds`  — resolved via `get_client_thresholds(name)` (always
                        current, so admin panel changes take effect
                        immediately without waiting for the next refresh)
      * `status`      — re-classified on every read so it reflects current
                        thresholds
      * `fetched_at`  — ISO 8601 UTC timestamp of last successful fetch
      * `platform`    — from the registry entry
      * `error`       — `None` on success
    """
    if _mock_mode:
        return _get_all_mock()

    result: dict[str, dict] = {}
    with _lock:
        # Take a snapshot of what we have so the read below is lock-free
        # for the enrichment pass (enrichment calls into monitoring_config
        # which has its own lock — never nest locks).
        snapshot_data = {k: dict(v) for k, v in _data.items()}
        snapshot_err = dict(_errors)
        snapshot_ts = dict(_ts)

    for entry in list_monitoring_workspaces():
        name = entry.name
        if name in snapshot_data:
            data = snapshot_data[name]
            data["error"] = None
            data["fetched_at"] = datetime.fromtimestamp(
                snapshot_ts.get(name, 0), tz=timezone.utc
            ).isoformat() if snapshot_ts.get(name) else None
            # Always reclassify on read so threshold changes take effect immediately
            data["status"] = _classify_client(data, name)
        elif name in snapshot_err:
            data = {
                "error":      snapshot_err[name],
                "status":     "error",
                "fetched_at": datetime.fromtimestamp(
                    snapshot_ts.get(name, 0), tz=timezone.utc
                ).isoformat() if snapshot_ts.get(name) else None,
            }
        else:
            data = {"error": "Loading...", "status": "loading", "fetched_at": None}

        data["platform"]   = entry.platform
        data["kpi"]        = get_client_kpi(name)
        data["thresholds"] = get_client_thresholds(name)
        result[name] = data

    return result


def _get_all_mock() -> dict[str, dict]:
    """Serve the mock fixture, enriched the same way as live data.

    Runs the fixture through `_classify_client` so tests exercise the real
    classifier — the UI should be rendering the same status strings in mock
    mode as in production.
    """
    fixture = _load_mock_fixture()
    result: dict[str, dict] = {}
    for entry in list_monitoring_workspaces():
        name = entry.name
        raw = fixture.get(name)
        if raw is None:
            result[name] = {
                "error":      "Missing from mock fixture",
                "status":     "error",
                "platform":   entry.platform,
                "kpi":        get_client_kpi(name),
                "thresholds": get_client_thresholds(name),
                "fetched_at": None,
            }
            continue

        # Entries with `_error` in the fixture become error cards
        if raw.get("_error"):
            result[name] = {
                "error":      raw["_error"],
                "status":     "error",
                "platform":   entry.platform,
                "kpi":        get_client_kpi(name),
                "thresholds": get_client_thresholds(name),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            continue

        # Copy to avoid mutating the shared fixture dict
        data: dict[str, Any] = {k: v for k, v in raw.items() if not k.startswith("_")}
        data["error"]      = None
        data["platform"]   = entry.platform
        data["kpi"]        = get_client_kpi(name)
        data["thresholds"] = get_client_thresholds(name)
        data["fetched_at"] = datetime.now(timezone.utc).isoformat()
        data["status"]     = _classify_client(data, name)
        result[name] = data
    return result


# ---------------------------------------------------------------------------
# Manual invalidation + config-save hook
# ---------------------------------------------------------------------------

def invalidate_all() -> None:
    """Force the next `should_refresh()` check to return True for all clients.

    Used when the admin panel saves new thresholds/KPIs — we want the next
    scheduler cycle to re-fetch fresh data even if the cache is not yet
    stale (GOAL.md 5.4.5). Note that even without a re-fetch, the next
    `get_all_monitoring_data()` call will reclassify with the new thresholds
    because the read path always calls `_classify_client` against the
    current config.
    """
    with _lock:
        _ts.clear()


def invalidate_client(client_name: str) -> None:
    """Force a single client to be refetched on the next scheduler cycle."""
    with _lock:
        _ts.pop(client_name, None)


def _on_config_saved(_cfg: dict) -> None:
    """Hook called by `monitoring_config.save_config()` — clears TS so the
    scheduler re-fetches on the next cycle. See `init_cache()`."""
    invalidate_all()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def init_cache() -> None:
    """Register with `monitoring_config` so config-save triggers invalidation.

    Called explicitly from the FastAPI app lifespan. Idempotent.
    """
    register_save_hook(_on_config_saved)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _reset_for_tests() -> None:
    """Clear all cache state + mock flag. Test-only."""
    global _mock_fixture
    with _lock:
        _data.clear()
        _ts.clear()
        _errors.clear()
        _generation.clear()
    _mock_fixture = None
    set_mock_mode(False)


__all__ = [
    "CACHE_TTL",
    "should_refresh",
    "get_generation",
    "write_client_data",
    "write_error",
    "write_backfill",
    "get_all_monitoring_data",
    "invalidate_all",
    "invalidate_client",
    "set_mock_mode",
    "is_mock_mode",
    "init_cache",
    "_reset_for_tests",
]
