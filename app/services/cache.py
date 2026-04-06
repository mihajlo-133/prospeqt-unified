"""In-memory QA result cache with async locking.

Two storage namespaces:
- _workspace_results: full QA results per workspace (from manual trigger)
- _workspace_campaigns: campaign lists per workspace (from discovery poll)

Both namespaces are protected by a single asyncio.Lock to prevent
concurrent reads/writes from producing inconsistent views.
"""
import asyncio
from datetime import datetime

from app.models.qa import GlobalQAResult, WorkspaceQAResult


class QACache:
    """Thread-safe in-memory cache for QA results and campaign lists.

    Used by:
    - discovery_poll: stores campaign lists via set_campaigns()
    - _run_workspace_qa_job: stores QA results via set_workspace()
    - Dashboard routes: reads via get_all() and get_workspace()
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._workspace_results: dict[str, WorkspaceQAResult] = {}
        self._workspace_campaigns: dict[str, list[dict]] = {}
        self._workspace_errors: dict[str, str] = {}
        self._last_global_refresh: datetime | None = None

    # ------------------------------------------------------------------
    # Workspace QA result operations
    # ------------------------------------------------------------------

    async def set_workspace(self, name: str, result: WorkspaceQAResult) -> None:
        """Store a workspace QA result and clear any previous error for it."""
        async with self._lock:
            self._workspace_results[name] = result
            self._workspace_errors.pop(name, None)

    async def get_workspace(self, name: str) -> WorkspaceQAResult | None:
        """Return the cached QA result for a workspace, or None if not found."""
        async with self._lock:
            return self._workspace_results.get(name)

    async def set_workspace_error(self, name: str, error: str) -> None:
        """Record an error string for a workspace (e.g. timeout, API failure)."""
        async with self._lock:
            self._workspace_errors[name] = error

    # ------------------------------------------------------------------
    # Campaign list operations (discovery poll namespace)
    # ------------------------------------------------------------------

    async def set_campaigns(self, name: str, campaigns: list[dict]) -> None:
        """Store a list of campaign dicts discovered for a workspace."""
        async with self._lock:
            self._workspace_campaigns[name] = campaigns

    async def get_campaigns(self, name: str) -> list[dict]:
        """Return cached campaign list for a workspace, or empty list."""
        async with self._lock:
            return self._workspace_campaigns.get(name, [])

    # ------------------------------------------------------------------
    # Global refresh timestamp
    # ------------------------------------------------------------------

    async def set_last_refresh(self, ts: datetime) -> None:
        """Record the timestamp of the last completed discovery poll."""
        async with self._lock:
            self._last_global_refresh = ts

    # ------------------------------------------------------------------
    # Aggregated view
    # ------------------------------------------------------------------

    async def get_all(self) -> GlobalQAResult:
        """Return a GlobalQAResult aggregated across all workspace results.

        Aggregates:
        - total_broken: sum of total_broken across all workspaces
        - total_campaigns_checked: sum of len(campaigns) per workspace
        - errors: copy of workspace error dict
        - last_refresh: most recently set timestamp
        """
        async with self._lock:
            workspaces = list(self._workspace_results.values())
            return GlobalQAResult(
                workspaces=workspaces,
                errors=dict(self._workspace_errors),
                total_broken=sum(w.total_broken for w in workspaces),
                total_campaigns_checked=sum(len(w.campaigns) for w in workspaces),
                last_refresh=self._last_global_refresh,
            )


# Module-level singleton — imported by poller and routes
_cache = QACache()


def get_cache() -> QACache:
    """Return the global QACache singleton."""
    return _cache
