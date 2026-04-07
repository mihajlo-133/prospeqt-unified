"""Background monitoring refresh job — the "when" layer on top of T6 cache.

Runs every `MONITORING_POLL_INTERVAL_SECONDS` (default 60) via APScheduler's
`AsyncIOScheduler`. Each cycle:

  1. Snapshot the monitoring registry.
  2. For each client where `cache.should_refresh(name)` is True:
     a. Look up the API key. Missing keys → `write_error("API key not found")`.
     b. Dispatch to `fetch_instantly_data` or `fetch_emailbison_data`.
     c. On success → `write_client_data(name, result)`; spawn a fire-and-forget
        Phase 2 backfill task (Instantly only; EB doesn't have the endpoint).
     d. On failure → `write_error(name, exc)` with friendly message.
  3. Phase 2 backfill calls `_count_not_contacted_via_api` per campaign and
     passes the results + the generation number captured at Phase 1 time to
     `write_backfill`. If a newer Phase 1 fetch landed meanwhile, the cache
     rejects the stale backfill via the generation guard (T6 test 7).

Mock mode: the scheduler job is registered even in mock mode (so T8 can
verify registration) but early-exits without touching the API. The UI
serves the fixture via `get_all_monitoring_data()` in T6.

Separate from `app/services/poller.py` (QA discovery poll) — the two jobs
have different intervals, different state, different failure modes.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.api.monitoring_emailbison import fetch_emailbison_data
from app.api.monitoring_instantly import fetch_instantly_data
from app.services import monitoring_cache as cache
from app.services.monitoring import _count_not_contacted_via_api
from app.services.registry import list_monitoring_workspaces

logger = logging.getLogger(__name__)

#: APScheduler job id — keep stable so `replace_existing=True` works.
JOB_ID = "monitoring_refresh"

#: Default refresh interval (seconds). Checks all clients for staleness every
#: 60s; only stale ones get refetched because of the CACHE_TTL gate in T6.
DEFAULT_INTERVAL_SECONDS = 60


# ---------------------------------------------------------------------------
# Shared HTTP client — one per application lifetime
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None
#: Tracks in-flight Phase 2 backfill tasks so shutdown can await them. Using
#: a set (not a list) because we add/discard on create/finish.
_backfill_tasks: set[asyncio.Task] = set()


def _get_http_client() -> httpx.AsyncClient:
    """Return the shared client. Raises if `init_monitoring_poller` wasn't called."""
    if _http_client is None:
        raise RuntimeError("monitoring_poller.init_monitoring_poller() was not called")
    return _http_client


# ---------------------------------------------------------------------------
# Per-client fetch
# ---------------------------------------------------------------------------

#: Dispatch table: platform → async fetcher. Keeps `_refresh_one_client`
#: platform-agnostic so adding a third platform is one line.
_FETCHERS: dict[str, Callable[[httpx.AsyncClient, str, str], Awaitable[dict]]] = {
    "instantly":  fetch_instantly_data,
    "emailbison": fetch_emailbison_data,
}


async def _refresh_one_client(entry) -> None:
    """Fetch a single client and update the cache.

    Errors are converted to cache error entries via `write_error`. This
    coroutine never raises — it's designed to run under `asyncio.gather`
    with peers so that one failure doesn't cancel the rest.
    """
    name = entry.name
    api_key = entry._api_key  # dataclass exposes this; readonly-by-convention
    if not api_key:
        cache.write_error(name, "API key not found")
        return

    fetcher = _FETCHERS.get(entry.platform)
    if fetcher is None:
        cache.write_error(name, f"Unknown platform: {entry.platform}")
        return

    client = _get_http_client()
    try:
        result = await fetcher(client, name, api_key)
    except Exception as exc:  # noqa: BLE001 — whatever the fetcher raised
        logger.warning("monitoring fetch failed for %s: %s", name, exc)
        cache.write_error(name, exc)
        return

    generation, backfill_ids, backfill_key = cache.write_client_data(name, result)

    # Phase 2 backfill: Instantly only. EB doesn't expose a ground-truth
    # per-campaign not_contacted endpoint, so the Phase 1 estimate from
    # `_count_not_contacted_from_analytics` is final for EB.
    if entry.platform == "instantly" and backfill_ids and backfill_key:
        task = asyncio.create_task(
            _phase2_backfill(name, backfill_ids, backfill_key, generation),
            name=f"backfill-{name}-{generation}",
        )
        _backfill_tasks.add(task)
        task.add_done_callback(_backfill_tasks.discard)


# ---------------------------------------------------------------------------
# Phase 2 backfill
# ---------------------------------------------------------------------------

async def _phase2_backfill(
    client_name: str,
    campaign_ids: list[str],
    api_key: str,
    generation: int,
) -> None:
    """Ground-truth not_contacted counts via paginated POST /leads/list.

    Runs as a fire-and-forget task after a successful Phase 1 fetch. If a
    newer Phase 1 fetch lands before this finishes, `write_backfill` will
    detect the stale generation and drop these results (T6 test 7).

    Per-campaign failures inside `_count_not_contacted_via_api` return a
    partial count (logged), so one bad campaign never kills the backfill.
    """
    client = _get_http_client()
    nc_by_campaign: dict[str, int] = {}
    for cid in campaign_ids:
        try:
            count = await _count_not_contacted_via_api(client, cid, api_key)
        except Exception as exc:  # noqa: BLE001 — tolerate any transport oddity
            logger.warning("backfill helper raised for %s/%s: %s", client_name, cid, exc)
            continue
        nc_by_campaign[cid] = count

    if not nc_by_campaign:
        return

    applied = cache.write_backfill(client_name, generation, nc_by_campaign)
    if not applied:
        logger.debug(
            "backfill for %s generation %d discarded (newer fetch already landed)",
            client_name, generation,
        )


# ---------------------------------------------------------------------------
# The scheduler job itself
# ---------------------------------------------------------------------------

async def refresh_stale_monitoring() -> None:
    """Top-level APScheduler job. Refreshes every stale client in parallel.

    Never raises — wraps the whole fan-out in an exception handler so a bug
    in one client's fetcher can't kill the APScheduler job runner.
    """
    if cache.is_mock_mode():
        # In mock mode there's nothing to fetch; the UI serves fixture data
        # directly via get_all_monitoring_data(). Register the job anyway
        # (T8 verifies registration) but return immediately each tick.
        return

    try:
        entries = list_monitoring_workspaces()
        stale = [e for e in entries if cache.should_refresh(e.name)]
        if not stale:
            return

        logger.debug("monitoring refresh: %d stale clients", len(stale))
        # Run fetches concurrently. Individual clients own their own
        # per-workspace semaphores (separate for Instantly vs EB), so we
        # don't need to throttle at this level — the semaphores already
        # limit to 5 in-flight requests per workspace.
        await asyncio.gather(
            *(_refresh_one_client(e) for e in stale),
            return_exceptions=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("monitoring refresh cycle crashed")


# ---------------------------------------------------------------------------
# Lifespan hooks
# ---------------------------------------------------------------------------

def init_monitoring_poller(scheduler: AsyncIOScheduler) -> None:
    """Register the monitoring refresh job on the existing app scheduler.

    Called from `app/main.py` lifespan. Idempotent via `replace_existing=True`.
    Also creates the shared `httpx.AsyncClient` and calls `cache.init_cache()`
    to wire the config-save hook.
    """
    global _http_client
    if _http_client is None:
        # Shared client for all monitoring HTTP. 30s total timeout per call,
        # 10s connect. HTTP/2 keeps per-workspace connection pools warm.
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )

    cache.init_cache()  # register config-save → invalidate hook (idempotent)

    # Remove any existing job with our ID before re-adding. APScheduler's
    # `replace_existing=True` is only honored by the *job store* after
    # `scheduler.start()` — for pending-queue adds (our case, we register
    # during lifespan before start), re-adding the same id actually produces
    # a duplicate. Explicit remove guarantees idempotency in all phases.
    try:
        scheduler.remove_job(JOB_ID)
    except Exception:
        pass  # job didn't exist; that's fine

    interval = int(os.getenv("MONITORING_POLL_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS)))
    scheduler.add_job(
        refresh_stale_monitoring,
        "interval",
        seconds=interval,
        id=JOB_ID,
        replace_existing=True,
        coalesce=True,      # if the previous tick is still running, skip the next one instead of piling up
        max_instances=1,    # never run two copies of this job concurrently
    )


async def shutdown_monitoring_poller() -> None:
    """Close the shared httpx client and cancel any in-flight backfills.

    Called from `app/main.py` lifespan on shutdown. Awaits all backfill
    tasks so half-written cache state doesn't linger across restarts.
    """
    global _http_client

    # Cancel backfills gracefully. They'll log on cancellation but the
    # cache writes are idempotent and generation-guarded, so partial
    # results are safe.
    for task in list(_backfill_tasks):
        if not task.done():
            task.cancel()
    if _backfill_tasks:
        await asyncio.gather(*_backfill_tasks, return_exceptions=True)
    _backfill_tasks.clear()

    if _http_client is not None:
        try:
            await _http_client.aclose()
        finally:
            _http_client = None


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _set_http_client_for_tests(client) -> None:
    """Inject a mock httpx client. Test-only."""
    global _http_client
    _http_client = client


def _reset_for_tests() -> None:
    """Clear shared client and in-flight task set. Test-only."""
    global _http_client
    _http_client = None
    _backfill_tasks.clear()


__all__ = [
    "JOB_ID",
    "DEFAULT_INTERVAL_SECONDS",
    "refresh_stale_monitoring",
    "init_monitoring_poller",
    "shutdown_monitoring_poller",
    "_refresh_one_client",
    "_phase2_backfill",
    "_set_http_client_for_tests",
    "_reset_for_tests",
]
