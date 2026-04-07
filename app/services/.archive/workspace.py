import os
import re

WORKSPACE_ENV_PATTERN = re.compile(r'^WORKSPACE_([A-Z0-9_]+)_API_KEY$')

# Map env var slugs to proper display names (matching campaign monitoring dashboard)
DISPLAY_NAMES = {
    "MYPLACE": "MyPlace",
    "SWISHFUNDING": "SwishFunding",
    "SMARTMATCHAPP": "SmartMatchApp",
    "KAYSE": "Kayse",
    "PROSPERLY": "Prosperly",
    "HEYREACH": "HeyReach",
    "ENAVRA": "Enavra",
}

_registry: dict[str, str] = {}


def load_from_env() -> None:
    """Read all WORKSPACE_*_API_KEY env vars and populate the registry."""
    global _registry
    _registry = {}
    for key, value in os.environ.items():
        match = WORKSPACE_ENV_PATTERN.match(key)
        if match and value:
            raw_name = match.group(1)
            # Use proper display name if known, otherwise title-case the slug
            display_name = DISPLAY_NAMES.get(raw_name, raw_name.replace("_", " ").title())
            _registry[display_name] = value


def list_workspaces() -> list[dict]:
    """Return a list of workspace dicts with name and last-4 key preview."""
    result = []
    for name, key in _registry.items():
        preview = key[-4:] if len(key) >= 4 else key
        result.append({"name": name, "key_preview": f"...{preview}"})
    return result


def _resolve_name(name: str) -> str | None:
    """Find the registry key matching name (case-insensitive)."""
    if name in _registry:
        return name
    name_lower = name.lower()
    for key in _registry:
        if key.lower() == name_lower:
            return key
    return None


def get_api_key(name: str) -> str | None:
    """Return the API key for the given workspace name (case-insensitive), or None."""
    resolved = _resolve_name(name)
    return _registry[resolved] if resolved else None


def add_workspace(name: str, api_key: str) -> None:
    """Add or update a workspace in the registry and set the corresponding env var."""
    _registry[name] = api_key
    env_var_name = f"WORKSPACE_{name.upper().replace('-', '_').replace(' ', '_')}_API_KEY"
    os.environ[env_var_name] = api_key


def remove_workspace(name: str) -> bool:
    """Remove a workspace from the registry. Returns True if it existed, False otherwise."""
    resolved = _resolve_name(name)
    if resolved is None:
        return False
    del _registry[resolved]
    env_var_name = f"WORKSPACE_{resolved.upper().replace('-', '_').replace(' ', '_')}_API_KEY"
    os.environ.pop(env_var_name, None)
    return True
