"""Unified client registry for the Prospeqt Unified Dashboard.

Single source of truth for all client entries across QA and Monitoring.
Each entry knows its platform, env var, workspace ID, and which features
are enabled. Replaces the old workspace.py (QA-only) registry.
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Base directory for resolving relative key_path values (local dev fallback)
BASE_DIR = Path(__file__).resolve().parents[3]  # prospeqt-unified repo root's parent (claude-code)


@dataclass
class ClientEntry:
    name: str
    slug: str
    platform: str  # "instantly" | "emailbison"
    env_var: str
    workspace_id: str | None = None
    monitoring_enabled: bool = True
    qa_enabled: bool = True
    _api_key: str | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Client definitions (9 entries per GOAL.md 1.3)
# ---------------------------------------------------------------------------

_CLIENT_DEFS: list[dict] = [
    # Instantly v2
    {"name": "MyPlace",       "slug": "myplace",       "platform": "instantly",  "env_var": "INSTANTLY_MYPLACE",       "workspace_id": "3c4b6833-22ba-4cee-8215-021ab01da35e", "monitoring_enabled": True,  "qa_enabled": True},
    {"name": "SwishFunding",  "slug": "swishfunding",  "platform": "instantly",  "env_var": "INSTANTLY_SWISHFUNDING",  "workspace_id": "b6f49b96-a950-4c7c-be1c-9370b185922f", "monitoring_enabled": True,  "qa_enabled": True},
    {"name": "SmartMatchApp", "slug": "smartmatchapp", "platform": "instantly",  "env_var": "INSTANTLY_SMARTMATCHAPP", "workspace_id": "cfedb87e-bd53-4e57-bb9f-6429930cf928", "monitoring_enabled": True,  "qa_enabled": True},
    {"name": "HeyReach",      "slug": "heyreach",      "platform": "instantly",  "env_var": "INSTANTLY_HEYREACH",      "workspace_id": "ca623f10-3378-4bd0-aebe-1557bdcfc9a0", "monitoring_enabled": True,  "qa_enabled": True},
    {"name": "Kayse",         "slug": "kayse",         "platform": "instantly",  "env_var": "INSTANTLY_KAYSE",         "workspace_id": "796f9bc3-2b29-4df8-a4da-0d9ff7fe6da3", "monitoring_enabled": True,  "qa_enabled": True},
    {"name": "Prosperly",     "slug": "prosperly",     "platform": "instantly",  "env_var": "INSTANTLY_PROSPERLY",     "workspace_id": "10b3e975-f70b-483e-bb5a-613a60060147", "monitoring_enabled": True,  "qa_enabled": True},
    {"name": "Enavra",        "slug": "enavra",        "platform": "instantly",  "env_var": "INSTANTLY_ENAVRA",        "workspace_id": "038fbeb2-d826-415e-be68-b77654f78a88", "monitoring_enabled": True,  "qa_enabled": True},
    # EmailBison
    {"name": "RankZero",          "slug": "rankzero",          "platform": "emailbison", "env_var": "EMAILBISON_RANKZERO",     "workspace_id": None, "monitoring_enabled": True,  "qa_enabled": False},
    {"name": "SwishFunding (EB)", "slug": "swishfunding_eb",   "platform": "emailbison", "env_var": "EMAILBISON_SWISHFUNDING", "workspace_id": None, "monitoring_enabled": True,  "qa_enabled": False},
]

# ---------------------------------------------------------------------------
# Registry state
# ---------------------------------------------------------------------------

_registry: list[ClientEntry] = []


def read_api_key(key_ref: str) -> str | None:
    """Read API key from env var (Render) or markdown file (local dev).

    Ported from the monitoring dashboard's server.py. On Render, keys are
    set as env vars directly. For local dev, keys live in markdown files
    inside fenced code blocks.
    """
    # Try env var first
    value = os.environ.get(key_ref)
    if value:
        return value.strip()
    # Fall back to file-based reading for local dev
    path = BASE_DIR / key_ref
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
        match = re.search(r"```\n(.+?)\n```", content, re.DOTALL)
        return match.group(1).strip() if match else None
    except Exception:
        return None


def load_from_env() -> None:
    """Populate the registry from environment variables.

    Checks both naming conventions:
    - Monitoring-style: INSTANTLY_*, EMAILBISON_* (from env_var field)
    - QA legacy-style: WORKSPACE_{SLUG}_API_KEY (backward compat per GOAL.md 1.5)
    """
    global _registry
    _registry = []
    for defn in _CLIENT_DEFS:
        entry = ClientEntry(
            name=defn["name"],
            slug=defn["slug"],
            platform=defn["platform"],
            env_var=defn["env_var"],
            workspace_id=defn.get("workspace_id"),
            monitoring_enabled=defn.get("monitoring_enabled", True),
            qa_enabled=defn.get("qa_enabled", True),
        )
        # Try monitoring-style env var first
        key = os.environ.get(defn["env_var"])
        if not key:
            # Try QA legacy-style env var
            legacy_var = f"WORKSPACE_{defn['slug'].upper()}_API_KEY"
            key = os.environ.get(legacy_var)
        if key:
            entry._api_key = key.strip()
        _registry.append(entry)


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------


def list_workspaces() -> list[dict]:
    """Return QA-enabled workspaces with name and key preview.

    Backward-compatible with the old workspace.py signature used by
    dashboard.py, admin.py, and poller.py.
    """
    result = []
    for entry in _registry:
        if not entry.qa_enabled:
            continue
        if entry._api_key:
            preview = f"...{entry._api_key[-4:]}"
        else:
            preview = "not set"
        result.append({"name": entry.name, "key_preview": preview})
    return result


def list_monitoring_workspaces() -> list[ClientEntry]:
    """Return all entries where monitoring_enabled=True."""
    return [e for e in _registry if e.monitoring_enabled]


def list_qa_workspaces() -> list[ClientEntry]:
    """Return all entries where qa_enabled=True."""
    return [e for e in _registry if e.qa_enabled]


def get_api_key(name: str) -> str | None:
    """Return the API key for a client by display name (case-insensitive).

    Backward-compatible with old workspace.py signature.
    """
    name_lower = name.lower()
    for entry in _registry:
        if entry.name.lower() == name_lower:
            return entry._api_key
    return None


def get_client(name: str) -> ClientEntry | None:
    """Return the full ClientEntry for a client by display name (case-insensitive)."""
    name_lower = name.lower()
    for entry in _registry:
        if entry.name.lower() == name_lower:
            return entry
    return None


def add_workspace(name: str, api_key: str) -> None:
    """Add or update a workspace in the registry (runtime only).

    Backward-compatible with old workspace.py for admin panel usage.
    Sets the corresponding env var so the key persists for this process.
    """
    name_lower = name.lower()
    for entry in _registry:
        if entry.name.lower() == name_lower:
            entry._api_key = api_key
            os.environ[entry.env_var] = api_key
            return
    # Not in static defs — add as a dynamic instantly workspace
    slug = name.upper().replace("-", "_").replace(" ", "_")
    env_var_name = f"WORKSPACE_{slug}_API_KEY"
    os.environ[env_var_name] = api_key
    _registry.append(ClientEntry(
        name=name,
        slug=slug.lower(),
        platform="instantly",
        env_var=env_var_name,
        qa_enabled=True,
        monitoring_enabled=False,
        _api_key=api_key,
    ))


def remove_workspace(name: str) -> bool:
    """Remove a workspace from the registry. Returns True if it existed.

    Backward-compatible with old workspace.py for admin panel usage.
    """
    name_lower = name.lower()
    for i, entry in enumerate(_registry):
        if entry.name.lower() == name_lower:
            os.environ.pop(entry.env_var, None)
            legacy_var = f"WORKSPACE_{entry.slug.upper()}_API_KEY"
            os.environ.pop(legacy_var, None)
            _registry.pop(i)
            return True
    return False


def add_workspace_full(
    name: str,
    api_key: str,
    platform: str = "instantly",
    monitoring_enabled: bool = False,
    qa_enabled: bool = True,
) -> None:
    """Add or update a workspace with full registry fields.

    Extends `add_workspace()` with platform, monitoring_enabled, and
    qa_enabled. If the workspace already exists in the registry its fields
    are updated in-place; otherwise a new dynamic entry is appended.

    The original 2-arg `add_workspace()` signature is preserved for
    backward compatibility.
    """
    name_lower = name.lower()
    for entry in _registry:
        if entry.name.lower() == name_lower:
            entry._api_key = api_key
            entry.platform = platform
            entry.monitoring_enabled = monitoring_enabled
            entry.qa_enabled = qa_enabled
            os.environ[entry.env_var] = api_key
            return
    # New dynamic entry
    slug = name.upper().replace("-", "_").replace(" ", "_")
    env_var_name = f"WORKSPACE_{slug}_API_KEY"
    os.environ[env_var_name] = api_key
    _registry.append(ClientEntry(
        name=name,
        slug=slug.lower(),
        platform=platform,
        env_var=env_var_name,
        qa_enabled=qa_enabled,
        monitoring_enabled=monitoring_enabled,
        _api_key=api_key,
    ))


def list_all_workspaces_detailed() -> list[dict]:
    """Return all registry entries with full fields for the admin panel.

    Includes every entry regardless of qa_enabled / monitoring_enabled,
    so the admin workspace table shows the complete picture.

    Returns:
        List of dicts with keys: name, platform, key_preview,
        monitoring_enabled, qa_enabled.
    """
    result = []
    for entry in _registry:
        if entry._api_key:
            preview = f"...{entry._api_key[-4:]}"
        else:
            preview = "not set"
        result.append({
            "name": entry.name,
            "platform": entry.platform,
            "key_preview": preview,
            "monitoring_enabled": entry.monitoring_enabled,
            "qa_enabled": entry.qa_enabled,
        })
    return result
