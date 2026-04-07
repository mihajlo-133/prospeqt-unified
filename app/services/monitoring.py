"""Monitoring aggregation helpers — pure logic + small I/O helpers.

Everything in this module is:
  * **Pure** (no global state, no side effects on import) OR
  * **Parameterised I/O** (the one async function takes an `httpx.AsyncClient`
    passed in by the caller; the module itself owns no connection pool).

This split keeps business logic (trends, classification, pool runway) fully
unit-testable without mocking HTTP, while letting the Instantly client
(`app/api/monitoring_instantly.py`, T4) reuse the same backfill routine by
passing its own shared client.

Ported from `gtm/prospeqt-outreach-dashboard/server.py`:
  * `_safe_num`              (lines 310-323)
  * `_friendly_error`        (lines 330-349)
  * `_count_not_contacted_from_analytics`  (lines 384-398)
  * `_count_not_contacted_via_api`         (lines 401-430) — async port
  * `_trend`                 (lines 881-890)
  * `_pool_days_remaining`   (lines 893-899)
  * `_classify_client`       (lines 902-947)
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.monitoring_config import get_client_kpi, get_client_thresholds

logger = logging.getLogger(__name__)

INSTANTLY_BASE = "https://api.instantly.ai/api/v2"
FILTER_VAL_NOT_CONTACTED = "FILTER_VAL_NOT_CONTACTED"


# ---------------------------------------------------------------------------
# Safe numeric coercion (GOAL.md 4.2.1)
# ---------------------------------------------------------------------------

def _safe_num(val: Any, default: float | int = 0) -> float | int:
    """Coerce `val` to a number, returning `default` on None / "\\N" / garbage.

    Instantly's analytics endpoints occasionally return `"\\N"` (a PostgreSQL
    NULL export artifact) in fields like `emails_sent_count`. Without this
    helper every call site would need its own `try/except` pair.
    """
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return val
    try:
        return int(val)
    except (ValueError, TypeError):
        try:
            return float(val)
        except (ValueError, TypeError):
            return default


# ---------------------------------------------------------------------------
# Friendly error messages (GOAL.md 4.2.5)
# ---------------------------------------------------------------------------

#: Ordered substring patterns → user-facing messages. First match wins; keep
#: the more specific patterns (e.g. "HTTP 401") ahead of the broader ones
#: (e.g. "HTTP 5"). Matching is case-insensitive.
_ERROR_MESSAGES: tuple[tuple[str, str], ...] = (
    ("invalid literal",     "Data format error — retrying on next refresh"),
    ("http 401",            "Authentication failed — API key may be invalid"),
    ("http 403",            "Access denied — check API permissions"),
    ("http 429",            "Rate limited — will retry shortly"),
    ("http 5",              "Platform is experiencing issues — will retry"),
    ("url error",           "Could not reach platform — will retry"),
    ("connect",             "Could not reach platform — will retry"),
    ("json decode",         "Received unexpected response — will retry"),
    ("timed out",           "Request timed out — will retry"),
    ("timeout",             "Request timed out — will retry"),
    ("api key not found",   "API key not configured"),
)


def _friendly_error(raw_error: str) -> str:
    """Map a raw Python exception string to a human-friendly message."""
    lower = (raw_error or "").lower()
    for pattern, message in _ERROR_MESSAGES:
        if pattern in lower:
            return message
    return "Data temporarily unavailable — retrying"


# ---------------------------------------------------------------------------
# Trend / pool runway (GOAL.md 4.2.2, 4.2.3)
# ---------------------------------------------------------------------------

def _trend(today_val: float, avg_val: float) -> str:
    """Return "up" / "down" / "flat" by comparing today to the 7-day average.

    Ratios:
        ratio >= 1.1 → "up"
        ratio <= 0.9 → "down"
        avg_val == 0 → "flat" (undefined ratio, no trend to show)
    """
    if avg_val == 0:
        return "flat"
    ratio = today_val / avg_val
    if ratio >= 1.1:
        return "up"
    if ratio <= 0.9:
        return "down"
    return "flat"


def _pool_days_remaining(data: dict, client_name: str) -> float:
    """Calculate lead pool runway in days.

    `client_name` is kept in the signature (unused) to match the source
    signature and the `_classify_client` call site exactly — makes future
    per-client overrides (e.g. pool-days calibration) a one-line change
    instead of a signature migration.
    """
    del client_name  # reserved for future per-client overrides
    nc = data.get("not_contacted", 0) or 0
    rate = (data.get("sent_today", 0) or 0) or (data.get("avg_sent_7d", 0) or 0)
    if rate <= 0:
        return float("inf")
    return nc / rate


# ---------------------------------------------------------------------------
# 10-rule classification waterfall (GOAL.md 4.2.4)
# ---------------------------------------------------------------------------

def _classify_client(data: dict, client_name: str) -> str:
    """Classify a client's health as "green" / "amber" / "red".

    Waterfall (first matching rule wins). Rule numbering matches GOAL.md
    4.2.4 for traceability:

        1.  active == 0 AND total > 0 ............. → amber  (paused)
        2.  active > 0 AND sent_today == 0 ......... → red    (stalled)
        3.  sent_ratio < sent_pct_red .............. → red    (way behind KPI)
        4.  sent_ratio < sent_pct_warn ............. → amber  (behind KPI)
        5.  sent > 50 AND rr < reply_rate_red ...... → red    (dead copy)
        6.  sent > 50 AND rr < reply_rate_warn ..... → amber  (soft copy)
        7.  bounce_rate > bounce_rate_red .......... → red    (infra issue)
        8.  bounce_rate > bounce_rate_warn ......... → amber  (watch infra)
        9.  pool_days < pool_days_red .............. → red    (out of leads)
        10. pool_days < pool_days_warn ............. → amber  (low leads)
        else ...................................... → green

    Rule 5/6 gate on `sent > 50` so that a single reply swing on small volume
    can't trip the classifier.
    """
    kpi = get_client_kpi(client_name)
    thresholds = get_client_thresholds(client_name)
    sent_kpi = kpi.get("sent", 0) or 0

    active = data.get("active_campaigns", 0) or 0
    total = data.get("total_campaigns", 0) or 0
    sent_today = data.get("sent_today", 0) or 0

    # Rule 1: paused — has campaigns but none running
    if active == 0 and total > 0:
        return "amber"

    # Rule 2: stalled — campaigns are supposedly running but nothing sent
    if active > 0 and sent_today == 0:
        return "red"

    # Rules 3 & 4: sent vs KPI
    if sent_kpi > 0:
        sent_ratio = sent_today / sent_kpi
        if sent_ratio < thresholds["sent_pct_red"]:
            return "red"
        if sent_ratio < thresholds["sent_pct_warn"]:
            return "amber"

    # Rules 5 & 6: reply rate (only on meaningful sample sizes)
    reply_rate = data.get("reply_rate_today", 0) or 0
    if sent_today > 50:
        if reply_rate < thresholds["reply_rate_red"]:
            return "red"
        if reply_rate < thresholds["reply_rate_warn"]:
            return "amber"

    # Rules 7 & 8: bounce rate
    bounce_rate = data.get("bounce_rate", 0) or 0
    if bounce_rate > thresholds["bounce_rate_red"]:
        return "red"
    if bounce_rate > thresholds["bounce_rate_warn"]:
        return "amber"

    # Rules 9 & 10: lead pool runway
    pool_days = _pool_days_remaining(data, client_name)
    if pool_days < thresholds["pool_days_red"]:
        return "red"
    if pool_days < thresholds["pool_days_warn"]:
        return "amber"

    return "green"


# ---------------------------------------------------------------------------
# not_contacted counters (GOAL.md 4.2.6 and 4.2.7)
# ---------------------------------------------------------------------------

def _count_not_contacted_from_analytics(analytics_entry: dict) -> int:
    """Phase 1 estimate: `leads_count - new_leads_contacted_count`.

    Fast — available from the workspace analytics response, no extra API
    call. Uses `new_leads_contacted_count` (unique first-email recipients)
    NOT `contacted_count` (total contact events) because the latter can
    exceed `leads_count` for campaigns with moved/recycled leads and
    produce false negatives.

    Replaced by `_count_not_contacted_via_api` during Phase 2 backfill
    (see GOAL.md 4.4.3 and T7).
    """
    leads = _safe_num(analytics_entry.get("leads_count"))
    contacted = _safe_num(analytics_entry.get("new_leads_contacted_count"))
    return max(0, leads - contacted)


async def _count_not_contacted_via_api(
    client: httpx.AsyncClient,
    campaign_id: str,
    api_key: str,
    page_limit: int = 100,
) -> int:
    """Phase 2 ground-truth count via paginated POST /leads/list.

    Matches the Instantly UI exactly — pages through every lead with
    `filter=FILTER_VAL_NOT_CONTACTED` and returns the total. Slower than
    the analytics estimate but accurate for campaigns with recycled leads
    where the two fields diverge.

    Args:
        client: A shared `httpx.AsyncClient`. The caller owns its lifetime
            (typically the per-workspace semaphore-gated client in T4).
        campaign_id: Instantly campaign UUID.
        api_key: Bearer token for the workspace.
        page_limit: Items per page. Instantly's max is 100.

    Returns 0 on any HTTP / decode failure — errors here must not break
    the dashboard, they only downgrade this client's count back to the
    Phase 1 estimate.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    url = f"{INSTANTLY_BASE}/leads/list"
    count = 0
    cursor: str | None = None

    while True:
        body: dict[str, Any] = {
            "filter": FILTER_VAL_NOT_CONTACTED,
            "campaign": campaign_id,
            "limit": page_limit,
        }
        if cursor:
            body["starting_after"] = cursor
        try:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — any failure aborts pagination gracefully
            logger.warning(
                "backfill failed for campaign %s: %s — returning partial count %d",
                campaign_id, exc, count,
            )
            return count

        items = payload.get("items", []) if isinstance(payload, dict) else []
        count += len(items)
        cursor = payload.get("next_starting_after") if isinstance(payload, dict) else None
        if not items or not cursor:
            return count
