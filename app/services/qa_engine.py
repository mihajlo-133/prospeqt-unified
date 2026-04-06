"""QA engine: variable extraction, bad value detection, per-campaign and per-workspace QA.

This is the core logic module for the Email QA Dashboard. It:
  1. Extracts {{variableName}} placeholders from campaign copy (QA-01)
  2. Detects broken variable values in lead payloads (QA-03, QA-04, QA-05)
  3. Computes per-campaign QA results with distinct broken lead counts (QA-06)
  4. Aggregates across campaigns into workspace-level results
"""
import logging
import re
from datetime import datetime, timezone
from typing import FrozenSet

import httpx

from app.models.qa import BrokenLeadDetail, CampaignQAResult, WorkspaceQAResult

logger = logging.getLogger(__name__)

# Matches {{variableName}} and {{ spacedVar }} patterns
_RAW_PATTERN = re.compile(r"\{\{([^}]+)\}\}")

# System variables that appear in campaign copy but are NOT lead payload fields.
# RANDOM covers the spin syntax {{RANDOM | opt1 | opt2}} — filtered by pipe check first.
# sendingAccountName is injected by Instantly (the sender's display name), not a lead field.
_SYSTEM_VARS: FrozenSet[str] = frozenset(["RANDOM", "accountSignature", "sendingAccountName"])


def extract_variables(copy_variants: list[dict]) -> set[str]:
    """Extract {{variableName}} placeholders from campaign copy variants.

    Rules (per D-01 through D-04):
    - Strips surrounding whitespace: {{ spacedVar }} -> 'spacedVar'
    - Excludes {{RANDOM | opt1 | opt2}} spin syntax (contains pipe character)
    - Excludes system variables: accountSignature
    - Returns empty set for empty or missing copy

    Args:
        copy_variants: List of {"subject": str, "body": str} dicts from
                       extract_copy_from_campaign().

    Returns:
        Set of variable name strings (case-sensitive, deduplicated).
    """
    vars_found: set[str] = set()
    for variant in copy_variants:
        for field_text in (variant.get("subject", ""), variant.get("body", "")):
            for raw in _RAW_PATTERN.findall(field_text):
                stripped = raw.strip()
                # Exclude RANDOM spin syntax: {{RANDOM | option1 | option2}}
                if "|" in stripped:
                    continue
                # Take first token (handles edge cases like "varName extra")
                ident = stripped.split()[0] if stripped else ""
                if ident and ident not in _SYSTEM_VARS:
                    vars_found.add(ident)
    return vars_found


def is_broken_value(value: str | None) -> bool:
    """Return True if value indicates broken or missing personalization data.

    Rules (per D-05, D-06, QA-03, QA-04, QA-05):
    - None: key absent from lead payload — broken (QA-04)
    - "": empty string — broken (QA-03)
    - "NO": Instantly sentinel for missing data — broken (QA-05)
    - All other values are considered valid

    Note: matching is exact and case-sensitive.
    "no" and "N/A" are NOT broken — only exact uppercase "NO".
    """
    return value is None or value == "" or value == "NO"


def check_lead(payload: dict, copy_vars: set[str]) -> set[str]:
    """Return the set of variable names that have broken values for this lead.

    Performs case-sensitive key lookup in payload (QA-02).
    Missing keys are treated as None (broken) per QA-04.

    Args:
        payload: The lead.payload dict from the Instantly API.
        copy_vars: Set of variable names extracted from campaign copy.

    Returns:
        Set of variable names that are broken for this lead.
    """
    broken: set[str] = set()
    for var_name in copy_vars:
        value = payload.get(var_name)  # None if key absent (QA-04)
        if is_broken_value(value):
            broken.add(var_name)
    return broken


async def run_campaign_qa(
    client: httpx.AsyncClient,
    api_key: str,
    campaign: dict,
    workspace_name: str,
) -> CampaignQAResult:
    """Run QA for a single campaign and return the result.

    Fetches all active leads, extracts copy variables, and checks each lead
    for broken values. Returns a CampaignQAResult with:
    - broken_count: distinct count of leads with at least one broken variable
    - issues_by_variable: per-variable count of affected leads
    - total_leads: total active lead count (before QA filtering)

    Per QA-06, D-08.

    Args:
        client: Shared httpx.AsyncClient instance.
        api_key: Instantly workspace API key.
        campaign: Campaign dict (must have 'id', 'name', 'sequences').
        workspace_name: Name used for semaphore selection and logging.

    Returns:
        CampaignQAResult with populated broken_count and issues_by_variable.
    """
    from app.api.instantly import extract_copy_from_campaign, fetch_all_leads

    copy_variants = extract_copy_from_campaign(campaign)
    copy_vars = extract_variables(copy_variants)
    leads = await fetch_all_leads(client, api_key, campaign["id"], workspace_name)

    issues_by_variable: dict[str, int] = {}
    broken_lead_ids: set[str] = set()
    broken_lead_details: list[BrokenLeadDetail] = []

    for lead in leads:
        payload = lead.get("payload") or {}
        broken_vars = check_lead(payload, copy_vars)
        if broken_vars:
            # Use lead id for deduplication; fall back to email if id absent
            broken_lead_ids.add(lead.get("id", lead.get("email", "")))
            for var_name in broken_vars:
                issues_by_variable[var_name] = issues_by_variable.get(var_name, 0) + 1
            # Capture per-lead detail for VIEW-04 drill-down
            broken_lead_details.append(BrokenLeadDetail(
                email=lead.get("email", ""),
                lead_status=lead.get("status", 0),
                broken_vars={v: payload.get(v) for v in broken_vars},
            ))

    return CampaignQAResult(
        campaign_id=campaign["id"],
        campaign_name=campaign["name"],
        total_leads=len(leads),
        broken_count=len(broken_lead_ids),
        issues_by_variable=issues_by_variable,
        broken_leads=broken_lead_details,
        last_checked=datetime.now(timezone.utc),
    )


async def run_workspace_qa(
    client: httpx.AsyncClient,
    api_key: str,
    workspace_name: str,
) -> WorkspaceQAResult:
    """Run QA for all draft/active campaigns in a workspace.

    Processes campaigns sequentially. If a single campaign fails (API error,
    unexpected data), the error is logged and execution continues with the
    remaining campaigns (error isolation per D-09).

    Args:
        client: Shared httpx.AsyncClient instance.
        api_key: Instantly workspace API key.
        workspace_name: Human-readable workspace identifier.

    Returns:
        WorkspaceQAResult with per-campaign results and aggregated totals.
        Failed campaigns are skipped (not included in results list).
    """
    from app.api.instantly import list_campaigns

    campaigns = await list_campaigns(client, api_key, workspace_name)
    results: list[CampaignQAResult] = []
    total_broken = 0

    for campaign in campaigns:
        try:
            result = await run_campaign_qa(client, api_key, campaign, workspace_name)
            results.append(result)
            total_broken += result.broken_count
        except Exception as exc:
            logger.exception(
                "QA failed for campaign '%s' (id=%s) in workspace '%s': %s",
                campaign.get("name"),
                campaign.get("id"),
                workspace_name,
                exc,
            )
            # Continue with remaining campaigns — error isolation

    return WorkspaceQAResult(
        workspace_name=workspace_name,
        campaigns=results,
        total_broken=total_broken,
        last_checked=datetime.now(timezone.utc),
    )
