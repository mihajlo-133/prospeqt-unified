"""Tests for QA engine: variable extraction, bad value detection, QA runners.

Covers QA-01 through QA-06 requirements.
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from app.api.instantly import INSTANTLY_BASE
from app.models.qa import BrokenLeadDetail, CampaignQAResult, GlobalQAResult, WorkspaceQAResult
from app.services.qa_engine import (
    check_lead,
    extract_variables,
    is_broken_value,
    run_campaign_qa,
    run_workspace_qa,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# extract_variables — QA-01
# ---------------------------------------------------------------------------


def test_extract_variables_basic():
    """Extract {{firstName}} and {{cityName}} from subject and body."""
    copy = [{"subject": "Hi {{firstName}}", "body": "Welcome to {{cityName}}"}]
    result = extract_variables(copy)
    assert result == {"firstName", "cityName"}


def test_extract_variables_with_spaces():
    """{{ spacedVar }} with surrounding whitespace is stripped to 'spacedVar'."""
    copy = [{"subject": "{{ spacedVar }}", "body": ""}]
    result = extract_variables(copy)
    assert result == {"spacedVar"}


def test_extract_variables_excludes_random():
    """{{RANDOM | opt1 | opt2}} spin syntax is excluded (contains pipe)."""
    copy = [{"subject": "{{RANDOM |opt1|opt2}}", "body": ""}]
    result = extract_variables(copy)
    assert result == set()


def test_extract_variables_excludes_account_signature():
    """{{accountSignature}} system variable is excluded."""
    copy = [{"subject": "", "body": "{{accountSignature}}"}]
    result = extract_variables(copy)
    assert result == set()


def test_extract_variables_mixed():
    """firstName and cityName extracted; RANDOM spin and accountSignature excluded."""
    copy = [
        {
            "subject": "Hi {{firstName}}, about {{RANDOM | opt1 | opt2}}",
            "body": "From {{cityName}}. {{accountSignature}}",
        }
    ]
    result = extract_variables(copy)
    assert result == {"firstName", "cityName"}


def test_extract_variables_empty_copy():
    """Empty subject and body returns empty set."""
    copy = [{"subject": "", "body": ""}]
    result = extract_variables(copy)
    assert result == set()


def test_extract_variables_no_variants():
    """Empty list of variants returns empty set."""
    result = extract_variables([])
    assert result == set()


def test_extract_variables_snake_case():
    """{{case_study_name}} with underscores is extracted correctly."""
    copy = [{"subject": "{{case_study_name}}", "body": ""}]
    result = extract_variables(copy)
    assert result == {"case_study_name"}


def test_extract_variables_deduplicates():
    """Same variable used in multiple variants/fields is returned once."""
    copy = [
        {"subject": "{{firstName}}", "body": "{{firstName}} again"},
        {"subject": "{{firstName}} step 2", "body": "{{companyName}}"},
    ]
    result = extract_variables(copy)
    assert result == {"firstName", "companyName"}


def test_extract_variables_from_fixture():
    """Variables from QA fixture campaign match expected set."""
    fixture = load_fixture("qa_campaign_fixture.json")
    from app.api.instantly import extract_copy_from_campaign
    copy = extract_copy_from_campaign(fixture["campaign"])
    result = extract_variables(copy)
    # firstName, companyName, cityName, spacedVar should be extracted
    # RANDOM spin and accountSignature should be excluded
    assert "firstName" in result
    assert "companyName" in result
    assert "cityName" in result
    assert "spacedVar" in result
    assert "RANDOM" not in result
    assert "accountSignature" not in result


# ---------------------------------------------------------------------------
# is_broken_value — QA-03, QA-04, QA-05
# ---------------------------------------------------------------------------


def test_is_broken_value_none():
    """None (missing key) is broken — QA-04."""
    assert is_broken_value(None) is True


def test_is_broken_value_empty():
    """Empty string is broken — QA-03."""
    assert is_broken_value("") is True


def test_is_broken_value_NO():
    """Exact 'NO' sentinel is broken — QA-05."""
    assert is_broken_value("NO") is True


def test_is_broken_value_valid():
    """A real value like 'hello' is not broken."""
    assert is_broken_value("hello") is False


def test_is_broken_value_lowercase_no():
    """Lowercase 'no' is not broken — only uppercase 'NO' sentinel."""
    assert is_broken_value("no") is False


def test_is_broken_value_na():
    """'N/A' is not broken — only exact 'NO' is the sentinel."""
    assert is_broken_value("N/A") is False


def test_is_broken_value_zero():
    """String '0' is not broken — it's a valid value."""
    assert is_broken_value("0") is False


def test_is_broken_value_whitespace():
    """Non-empty whitespace string is not broken (not '', not 'NO')."""
    assert is_broken_value("  ") is False


# ---------------------------------------------------------------------------
# Pydantic models — shape verification
# ---------------------------------------------------------------------------


def test_campaign_qa_result_model():
    """CampaignQAResult has the required D-08 fields."""
    now = datetime.now()
    result = CampaignQAResult(
        campaign_id="c-001",
        campaign_name="Test Campaign",
        total_leads=10,
        broken_count=3,
        issues_by_variable={"firstName": 2, "cityName": 1},
        last_checked=now,
    )
    assert result.campaign_id == "c-001"
    assert result.campaign_name == "Test Campaign"
    assert result.total_leads == 10
    assert result.broken_count == 3
    assert result.issues_by_variable == {"firstName": 2, "cityName": 1}
    assert result.last_checked == now


def test_campaign_qa_result_defaults():
    """CampaignQAResult has sensible defaults for optional fields."""
    result = CampaignQAResult(
        campaign_id="c-002",
        campaign_name="Default Test",
        total_leads=5,
        broken_count=0,
    )
    assert result.issues_by_variable == {}
    assert result.last_checked is None


def test_workspace_qa_result_model():
    """WorkspaceQAResult has workspace_name, campaigns, total_broken, error, last_checked."""
    campaign_result = CampaignQAResult(
        campaign_id="c-001",
        campaign_name="Test",
        total_leads=5,
        broken_count=2,
    )
    ws_result = WorkspaceQAResult(
        workspace_name="my-workspace",
        campaigns=[campaign_result],
        total_broken=2,
        error=None,
        last_checked=datetime.now(),
    )
    assert ws_result.workspace_name == "my-workspace"
    assert len(ws_result.campaigns) == 1
    assert ws_result.total_broken == 2
    assert ws_result.error is None


def test_workspace_qa_result_error():
    """WorkspaceQAResult can store an error string."""
    ws_result = WorkspaceQAResult(
        workspace_name="failed-ws",
        error="API key invalid",
    )
    assert ws_result.error == "API key invalid"
    assert ws_result.campaigns == []
    assert ws_result.total_broken == 0


def test_global_qa_result_model():
    """GlobalQAResult has workspaces, errors, total_broken, total_campaigns_checked, last_refresh."""
    global_result = GlobalQAResult(
        workspaces=[],
        errors={"bad-ws": "connection refused"},
        total_broken=0,
        total_campaigns_checked=0,
        last_refresh=datetime.now(),
    )
    assert global_result.workspaces == []
    assert "bad-ws" in global_result.errors
    assert global_result.total_broken == 0
    assert global_result.total_campaigns_checked == 0


# ---------------------------------------------------------------------------
# BrokenLeadDetail model and CampaignQAResult.broken_leads — VIEW-04
# ---------------------------------------------------------------------------


def test_broken_lead_detail_model():
    """BrokenLeadDetail stores email, lead_status, and broken_vars."""
    from app.models.qa import BrokenLeadDetail
    detail = BrokenLeadDetail(
        email="test@example.com",
        lead_status=1,
        broken_vars={"cityName": "", "firstName": None},
    )
    assert detail.email == "test@example.com"
    assert detail.lead_status == 1
    assert detail.broken_vars["cityName"] == ""
    assert detail.broken_vars["firstName"] is None


def test_campaign_qa_result_broken_leads_default():
    """CampaignQAResult.broken_leads defaults to empty list (backward compatible)."""
    result = CampaignQAResult(
        campaign_id="c-test",
        campaign_name="Test",
        total_leads=5,
        broken_count=0,
    )
    assert result.broken_leads == []


def test_campaign_qa_result_broken_leads_populated():
    """CampaignQAResult.broken_leads stores BrokenLeadDetail objects."""
    from app.models.qa import BrokenLeadDetail
    detail = BrokenLeadDetail(email="x@y.com", lead_status=1, broken_vars={"a": ""})
    result = CampaignQAResult(
        campaign_id="c-test",
        campaign_name="Test",
        total_leads=5,
        broken_count=1,
        broken_leads=[detail],
    )
    assert len(result.broken_leads) == 1
    assert result.broken_leads[0].email == "x@y.com"


# ---------------------------------------------------------------------------
# check_lead — QA-02, QA-04
# ---------------------------------------------------------------------------


def test_check_lead_returns_broken_vars():
    """Empty cityName is flagged as broken."""
    result = check_lead(
        payload={"firstName": "John", "cityName": ""},
        copy_vars={"firstName", "cityName"},
    )
    assert result == {"cityName"}


def test_check_lead_all_clean():
    """All variables populated — returns empty set."""
    result = check_lead(
        payload={"firstName": "John", "cityName": "NYC"},
        copy_vars={"firstName", "cityName"},
    )
    assert result == set()


def test_check_lead_missing_key():
    """Missing key from payload is treated as broken (None) — QA-04."""
    result = check_lead(
        payload={"firstName": "John"},
        copy_vars={"firstName", "cityName"},
    )
    assert result == {"cityName"}


def test_check_lead_NO_sentinel():
    """Exact 'NO' sentinel is flagged — QA-05."""
    result = check_lead(
        payload={"firstName": "NO"},
        copy_vars={"firstName"},
    )
    assert result == {"firstName"}


def test_check_lead_empty_copy_vars():
    """No copy variables — nothing is broken."""
    result = check_lead(
        payload={"firstName": "John", "cityName": "NYC"},
        copy_vars=set(),
    )
    assert result == set()


# ---------------------------------------------------------------------------
# run_campaign_qa — QA-06
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_run_campaign_qa_result_shape():
    """run_campaign_qa returns CampaignQAResult with correct fields."""
    fixture = load_fixture("qa_campaign_fixture.json")
    campaign = fixture["campaign"]

    respx.post(f"{INSTANTLY_BASE}/leads/list").mock(
        return_value=httpx.Response(
            200,
            json={"items": fixture["leads"], "next_starting_after": None},
        )
    )

    async with httpx.AsyncClient() as client:
        result = await run_campaign_qa(client, "test-api-key", campaign, "test-ws")

    assert isinstance(result, CampaignQAResult)
    assert result.campaign_id == "qa-camp-001"
    assert result.campaign_name == "QA Test Campaign"
    assert isinstance(result.total_leads, int)
    assert isinstance(result.broken_count, int)
    assert isinstance(result.issues_by_variable, dict)
    assert result.last_checked is not None


@respx.mock
@pytest.mark.asyncio
async def test_run_campaign_qa_broken_count_distinct():
    """3 leads, 2 broken (empty cityName, NO firstName) -> broken_count=2."""
    fixture = load_fixture("qa_campaign_fixture.json")
    campaign = fixture["campaign"]

    respx.post(f"{INSTANTLY_BASE}/leads/list").mock(
        return_value=httpx.Response(
            200,
            json={"items": fixture["leads"], "next_starting_after": None},
        )
    )

    async with httpx.AsyncClient() as client:
        result = await run_campaign_qa(client, "test-api-key", campaign, "test-ws")

    # lead-001 (Alice) is clean; lead-002 has empty cityName; lead-003 has NO firstName
    assert result.broken_count == 2
    assert result.total_leads == 3


@respx.mock
@pytest.mark.asyncio
async def test_run_campaign_qa_issues_by_variable():
    """issues_by_variable counts distinct broken leads per variable."""
    fixture = load_fixture("qa_campaign_fixture.json")
    campaign = fixture["campaign"]

    respx.post(f"{INSTANTLY_BASE}/leads/list").mock(
        return_value=httpx.Response(
            200,
            json={"items": fixture["leads"], "next_starting_after": None},
        )
    )

    async with httpx.AsyncClient() as client:
        result = await run_campaign_qa(client, "test-api-key", campaign, "test-ws")

    # lead-002 has empty cityName, lead-003 has NO firstName
    assert result.issues_by_variable.get("cityName") == 1
    assert result.issues_by_variable.get("firstName") == 1


@respx.mock
@pytest.mark.asyncio
async def test_run_campaign_qa_no_issues():
    """All leads clean -> broken_count=0, issues_by_variable={}."""
    fixture = load_fixture("qa_campaign_fixture.json")
    campaign = fixture["campaign"]

    clean_leads = [
        {
            "id": "qa-lead-001",
            "email": "clean@example.com",
            "status": 1,
            "payload": {
                "firstName": "Alice",
                "companyName": "Acme Corp",
                "cityName": "New York",
                "spacedVar": "value",
            },
        }
    ]

    respx.post(f"{INSTANTLY_BASE}/leads/list").mock(
        return_value=httpx.Response(
            200,
            json={"items": clean_leads, "next_starting_after": None},
        )
    )

    async with httpx.AsyncClient() as client:
        result = await run_campaign_qa(client, "test-api-key", campaign, "test-ws")

    assert result.broken_count == 0
    assert result.issues_by_variable == {}


@respx.mock
@pytest.mark.asyncio
async def test_run_campaign_qa_broken_leads_captured():
    """run_campaign_qa populates broken_leads with per-lead detail."""
    fixture = load_fixture("qa_campaign_fixture.json")
    campaign = fixture["campaign"]

    respx.post(f"{INSTANTLY_BASE}/leads/list").mock(
        return_value=httpx.Response(
            200,
            json={"items": fixture["leads"], "next_starting_after": None},
        )
    )

    async with httpx.AsyncClient() as client:
        result = await run_campaign_qa(client, "test-api-key", campaign, "test-ws")

    # 2 broken leads: lead-002 (empty cityName), lead-003 (NO firstName)
    assert len(result.broken_leads) == 2
    emails = {bl.email for bl in result.broken_leads}
    assert "broken-city@example.com" in emails
    assert "broken-name@example.com" in emails


@respx.mock
@pytest.mark.asyncio
async def test_run_campaign_qa_broken_leads_detail_values():
    """broken_leads entries contain correct broken_vars with actual values."""
    fixture = load_fixture("qa_campaign_fixture.json")
    campaign = fixture["campaign"]

    respx.post(f"{INSTANTLY_BASE}/leads/list").mock(
        return_value=httpx.Response(
            200,
            json={"items": fixture["leads"], "next_starting_after": None},
        )
    )

    async with httpx.AsyncClient() as client:
        result = await run_campaign_qa(client, "test-api-key", campaign, "test-ws")

    # Find the lead with empty cityName
    city_lead = next(bl for bl in result.broken_leads if bl.email == "broken-city@example.com")
    assert city_lead.lead_status == 1
    assert "cityName" in city_lead.broken_vars
    assert city_lead.broken_vars["cityName"] == ""

    # Find the lead with NO firstName
    name_lead = next(bl for bl in result.broken_leads if bl.email == "broken-name@example.com")
    assert name_lead.lead_status == 1
    assert "firstName" in name_lead.broken_vars
    assert name_lead.broken_vars["firstName"] == "NO"


@respx.mock
@pytest.mark.asyncio
async def test_run_campaign_qa_broken_leads_empty_when_clean():
    """All leads clean -> broken_leads is empty list."""
    fixture = load_fixture("qa_campaign_fixture.json")
    campaign = fixture["campaign"]
    clean_leads = [
        {
            "id": "clean-001",
            "email": "clean@example.com",
            "status": 1,
            "payload": {
                "firstName": "Alice",
                "companyName": "Acme Corp",
                "cityName": "New York",
                "spacedVar": "value",
            },
        }
    ]

    respx.post(f"{INSTANTLY_BASE}/leads/list").mock(
        return_value=httpx.Response(
            200,
            json={"items": clean_leads, "next_starting_after": None},
        )
    )

    async with httpx.AsyncClient() as client:
        result = await run_campaign_qa(client, "test-api-key", campaign, "test-ws")

    assert result.broken_leads == []


def test_case_sensitive_match():
    """Variable name matching is case-sensitive — 'companyname' != 'companyName'."""
    # payload has lowercase 'companyname', copy uses 'companyName' — they don't match
    result = check_lead(
        payload={"companyname": "Acme"},
        copy_vars={"companyName"},
    )
    # 'companyName' is missing from payload — it's broken
    assert "companyName" in result


# ---------------------------------------------------------------------------
# run_workspace_qa — aggregation and error isolation
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_run_workspace_qa_aggregates():
    """Two campaigns -> total_broken = sum of both campaign broken_counts."""
    campaigns_data = {
        "items": [
            {
                "id": "ws-camp-001",
                "name": "Campaign A",
                "status": 1,
                "sequences": [
                    {
                        "steps": [
                            {
                                "variants": [
                                    {
                                        "subject": "Hi {{firstName}}",
                                        "body": "Welcome to {{cityName}}",
                                    }
                                ]
                            }
                        ]
                    }
                ],
            },
            {
                "id": "ws-camp-002",
                "name": "Campaign B",
                "status": 1,
                "sequences": [
                    {
                        "steps": [
                            {
                                "variants": [
                                    {
                                        "subject": "Hello {{firstName}}",
                                        "body": "From {{companyName}}",
                                    }
                                ]
                            }
                        ]
                    }
                ],
            },
        ],
        "next_starting_after": None,
    }

    # Campaign A: one broken lead (empty cityName)
    camp_a_leads = {
        "items": [
            {
                "id": "lead-a1",
                "email": "a1@test.com",
                "status": 1,
                "payload": {"firstName": "John", "cityName": ""},
            },
            {
                "id": "lead-a2",
                "email": "a2@test.com",
                "status": 1,
                "payload": {"firstName": "Jane", "cityName": "NYC"},
            },
        ],
        "next_starting_after": None,
    }

    # Campaign B: one broken lead (missing companyName)
    camp_b_leads = {
        "items": [
            {
                "id": "lead-b1",
                "email": "b1@test.com",
                "status": 1,
                "payload": {"firstName": "Bob"},
            },
        ],
        "next_starting_after": None,
    }

    call_count = 0

    def leads_side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json=camp_a_leads)
        return httpx.Response(200, json=camp_b_leads)

    respx.get(f"{INSTANTLY_BASE}/campaigns").mock(
        return_value=httpx.Response(200, json=campaigns_data)
    )
    respx.post(f"{INSTANTLY_BASE}/leads/list").mock(side_effect=leads_side_effect)

    async with httpx.AsyncClient() as client:
        result = await run_workspace_qa(client, "test-api-key", "test-workspace")

    assert isinstance(result, WorkspaceQAResult)
    assert result.workspace_name == "test-workspace"
    assert len(result.campaigns) == 2
    # Campaign A: 1 broken (lead-a1 has empty cityName)
    # Campaign B: 1 broken (lead-b1 missing companyName)
    assert result.total_broken == 2


@respx.mock
@pytest.mark.asyncio
async def test_run_workspace_qa_error_isolation():
    """One campaign lead fetch fails -> other campaigns still succeed."""
    campaigns_data = {
        "items": [
            {
                "id": "err-camp-001",
                "name": "Failing Campaign",
                "status": 1,
                "sequences": [
                    {
                        "steps": [
                            {
                                "variants": [
                                    {"subject": "Hi {{firstName}}", "body": ""}
                                ]
                            }
                        ]
                    }
                ],
            },
            {
                "id": "ok-camp-002",
                "name": "Succeeding Campaign",
                "status": 1,
                "sequences": [
                    {
                        "steps": [
                            {
                                "variants": [
                                    {"subject": "Hello {{firstName}}", "body": ""}
                                ]
                            }
                        ]
                    }
                ],
            },
        ],
        "next_starting_after": None,
    }

    call_count = 0

    def leads_side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First campaign fails
            return httpx.Response(500, json={"error": "internal error"})
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "ok-lead-001",
                        "email": "ok@test.com",
                        "status": 1,
                        "payload": {"firstName": "Alice"},
                    }
                ],
                "next_starting_after": None,
            },
        )

    respx.get(f"{INSTANTLY_BASE}/campaigns").mock(
        return_value=httpx.Response(200, json=campaigns_data)
    )
    respx.post(f"{INSTANTLY_BASE}/leads/list").mock(side_effect=leads_side_effect)

    async with httpx.AsyncClient() as client:
        result = await run_workspace_qa(client, "test-api-key", "test-workspace")

    # Should have 1 successful campaign result (the second one)
    assert len(result.campaigns) == 1
    assert result.campaigns[0].campaign_id == "ok-camp-002"
    # No broken leads in the successful campaign
    assert result.total_broken == 0
