"""Background discovery poller and manual QA trigger infrastructure.

Responsibilities:
- discovery_poll: periodically discovers campaigns across all workspaces (OPS-04)
  - error isolation via asyncio.gather(return_exceptions=True) (OPS-05)
  - updates last_refresh timestamp on completion (OPS-06)
- trigger_qa_all: fire-and-forget full QA across all workspaces (OPS-01)
- trigger_qa_workspace: fire-and-forget QA for one workspace (OPS-02)
- trigger_qa_campaign: fire-and-forget QA for one campaign (OPS-03)
- Deduplication: concurrent scans for the same scope are suppressed (D-18)
"""
import asyncio
import logging
from datetime import datetime, timezone

import httpx

from app.api.instantly import list_campaigns
from app.services.cache import get_cache
from app.services.qa_engine import run_campaign_qa, run_workspace_qa
from app.services.workspace import get_api_key, list_workspaces

logger = logging.getLogger(__name__)

# Maps task_key -> asyncio.Task for in-flight QA jobs.
# task_key format: "workspace:{name}" or "campaign:{id}"
_running_scans: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Discovery poll (background interval job)
# ---------------------------------------------------------------------------


async def _discover_workspace(client: httpx.AsyncClient, ws_name: str) -> None:
    """Discover campaigns for one workspace and store in cache.

    Per D-13: discovery only (no QA). Errors are caught and stored as
    workspace errors so the caller can use return_exceptions=True safely.
    """
    api_key = get_api_key(ws_name)
    if not api_key:
        logger.warning("No API key for workspace %s — skipping discovery", ws_name)
        return
    try:
        campaigns = await list_campaigns(client, api_key, ws_name)
        await get_cache().set_campaigns(ws_name, campaigns)
        logger.debug("Discovered %d campaigns for workspace %s", len(campaigns), ws_name)
    except Exception as exc:
        logger.exception("Discovery failed for workspace %s: %s", ws_name, exc)
        await get_cache().set_workspace_error(ws_name, str(exc))


async def discovery_poll() -> None:
    """Background poll: discover campaigns across all workspaces.

    Per D-12, OPS-04: runs on APScheduler interval (QA_POLL_INTERVAL_SECONDS).
    Per D-15, OPS-05: error isolation via asyncio.gather(return_exceptions=True).
    Per D-16, OPS-06: updates last_refresh timestamp after all workspaces complete.

    Individual workspace failures are logged but do not prevent other workspaces
    from completing. The gather result is discarded — errors are already recorded
    in the cache by _discover_workspace.
    """
    workspaces = list_workspaces()
    if not workspaces:
        logger.debug("No workspaces configured — skipping discovery poll")
        return

    async with httpx.AsyncClient(timeout=60.0) as client:
        await asyncio.gather(
            *[_discover_workspace(client, ws["name"]) for ws in workspaces],
            return_exceptions=True,
        )

    await get_cache().set_last_refresh(datetime.now(timezone.utc))
    logger.info("Discovery poll complete: %d workspaces scanned", len(workspaces))


# ---------------------------------------------------------------------------
# Running scan deduplication helpers
# ---------------------------------------------------------------------------


def _prune_done_scans() -> None:
    """Remove completed tasks from _running_scans.

    Called at the start of every trigger function to prevent unbounded growth
    and to allow re-triggering a scan after its task has finished (Pitfall 6).
    """
    done_keys = [k for k, v in _running_scans.items() if v.done()]
    for k in done_keys:
        del _running_scans[k]


def get_scanning_workspace_names() -> set[str]:
    """Return set of workspace names that have an in-flight scan."""
    _prune_done_scans()
    names = set()
    for key, task in _running_scans.items():
        if key.startswith("workspace:") and not task.done():
            names.add(key.removeprefix("workspace:"))
    return names


# ---------------------------------------------------------------------------
# Background QA jobs (called by asyncio.create_task)
# ---------------------------------------------------------------------------


async def _run_workspace_qa_job(ws_name: str) -> None:
    """Background task: run full QA for a workspace and store result in cache."""
    api_key = get_api_key(ws_name)
    if not api_key:
        logger.warning("No API key for workspace %s — skipping QA job", ws_name)
        return
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            result = await run_workspace_qa(client, api_key, ws_name)
            await get_cache().set_workspace(ws_name, result)
        logger.info("QA complete for workspace %s: %d broken leads", ws_name, result.total_broken)
    except Exception as exc:
        logger.exception("QA job failed for workspace %s: %s", ws_name, exc)
        await get_cache().set_workspace_error(ws_name, str(exc))


async def _run_campaign_qa_job(campaign_id: str, campaign: dict, ws_name: str) -> None:
    """Background task: run QA for one campaign and update workspace result in cache."""
    api_key = get_api_key(ws_name)
    if not api_key:
        logger.warning("No API key for workspace %s — skipping campaign QA job", ws_name)
        return
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            result = await run_campaign_qa(client, api_key, campaign, ws_name)

        # Merge updated campaign result into existing workspace result (if any)
        cache = get_cache()
        ws_result = await cache.get_workspace(ws_name)
        if ws_result:
            # Replace the old campaign entry (or append if not found)
            updated_campaigns = [
                c for c in ws_result.campaigns if c.campaign_id != campaign_id
            ]
            updated_campaigns.append(result)
            updated_ws = ws_result.model_copy(update={
                "campaigns": updated_campaigns,
                "total_broken": sum(c.broken_count for c in updated_campaigns),
                "last_checked": datetime.now(timezone.utc),
            })
            await cache.set_workspace(ws_name, updated_ws)
        else:
            # No workspace result yet — create a minimal one for this campaign
            from app.models.qa import WorkspaceQAResult
            minimal = WorkspaceQAResult(
                workspace_name=ws_name,
                campaigns=[result],
                total_broken=result.broken_count,
                last_checked=datetime.now(timezone.utc),
            )
            await cache.set_workspace(ws_name, minimal)

        logger.info("Campaign QA complete for %s in %s: %d broken", campaign_id, ws_name, result.broken_count)
    except Exception as exc:
        logger.exception("Campaign QA job failed for %s in %s: %s", campaign_id, ws_name, exc)


# ---------------------------------------------------------------------------
# Manual QA triggers (fire-and-forget, return immediately)
# ---------------------------------------------------------------------------


async def trigger_qa_all() -> dict:
    """Trigger full QA across all configured workspaces.

    Returns immediately. QA runs as background asyncio tasks.
    Skips workspaces that already have an in-flight scan.
    Per OPS-01, D-17.

    Returns:
        {"status": "started", "workspaces_triggered": N}
    """
    _prune_done_scans()
    workspaces = list_workspaces()
    tasks_started = 0

    for ws in workspaces:
        task_key = f"workspace:{ws['name']}"
        existing = _running_scans.get(task_key)
        if existing and not existing.done():
            logger.debug("Skipping workspace %s — scan already in flight", ws["name"])
            continue
        task = asyncio.create_task(
            _run_workspace_qa_job(ws["name"]),
            name=f"qa-{task_key}",
        )
        _running_scans[task_key] = task
        tasks_started += 1

    return {"status": "started", "workspaces_triggered": tasks_started}


async def trigger_qa_workspace(ws_name: str) -> dict:
    """Trigger full QA for one workspace. Returns immediately.

    Deduplicates: if a scan is already running for this workspace,
    returns {"status": "already_running"} immediately.
    Per OPS-02, D-18.

    Returns:
        {"status": "started" | "already_running", "scope": task_key}
    """
    _prune_done_scans()
    task_key = f"workspace:{ws_name}"
    existing = _running_scans.get(task_key)
    if existing and not existing.done():
        return {"status": "already_running", "scope": task_key}

    task = asyncio.create_task(
        _run_workspace_qa_job(ws_name),
        name=f"qa-{task_key}",
    )
    _running_scans[task_key] = task
    return {"status": "started", "scope": task_key}


async def trigger_qa_campaign(campaign_id: str, campaign: dict, ws_name: str) -> dict:
    """Trigger QA for one campaign. Returns immediately.

    Deduplicates: if a scan is already running for this campaign,
    returns {"status": "already_running"} immediately.
    Per OPS-03, D-18.

    Returns:
        {"status": "started" | "already_running", "scope": task_key}
    """
    _prune_done_scans()
    task_key = f"campaign:{campaign_id}"
    existing = _running_scans.get(task_key)
    if existing and not existing.done():
        return {"status": "already_running", "scope": task_key}

    task = asyncio.create_task(
        _run_campaign_qa_job(campaign_id, campaign, ws_name),
        name=f"qa-{task_key}",
    )
    _running_scans[task_key] = task
    return {"status": "started", "scope": task_key}
