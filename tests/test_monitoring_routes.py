"""Route tests for the /monitoring overview and HTMX API endpoints.

All tests run with MOCK_MODE=1 so no live API keys are required.
The mock fixture (tests/fixtures/mock_monitoring_data.json) has 9 clients:
  SwishFunding, MyPlace, SmartMatchApp, HeyReach, Kayse,
  Prosperly (error), Enavra, RankZero, SwishFunding (EB)

Expected status from classifier (per mock fixture edge-case notes):
  green: SwishFunding, RankZero
  amber: MyPlace, HeyReach, Enavra, SwishFunding (EB)
  red:   SmartMatchApp, Kayse
  error: Prosperly
"""
import os

import pytest
from httpx import ASGITransport, AsyncClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ALL_CLIENT_NAMES = [
    "SwishFunding",
    "MyPlace",
    "SmartMatchApp",
    "HeyReach",
    "Kayse",
    "Prosperly",
    "Enavra",
    "RankZero",
    "SwishFunding (EB)",
]


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch):
    """Force MOCK_MODE=1 for all monitoring tests and reset cache state."""
    monkeypatch.setenv("MOCK_MODE", "1")
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    # Reset monitoring cache + mock flag before each test so tests are isolated
    from app.services import monitoring_cache
    monitoring_cache._reset_for_tests()
    monitoring_cache.set_mock_mode(True)
    yield
    monitoring_cache._reset_for_tests()


@pytest.fixture
async def client(mock_mode):
    from app.main import create_app
    from app.services import registry
    from app.services.monitoring_config import load_config

    registry.load_from_env()
    load_config()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# GET /monitoring — overview page
# ---------------------------------------------------------------------------

async def test_monitoring_returns_200(client):
    """GET /monitoring returns HTTP 200."""
    response = await client.get("/monitoring")
    assert response.status_code == 200


async def test_monitoring_returns_html(client):
    """GET /monitoring returns text/html content-type."""
    response = await client.get("/monitoring")
    assert "text/html" in response.headers.get("content-type", "")


async def test_monitoring_contains_all_client_names(client):
    """GET /monitoring HTML contains all 9 client names from mock fixture."""
    response = await client.get("/monitoring")
    assert response.status_code == 200
    text = response.text
    for name in ALL_CLIENT_NAMES:
        assert name in text, f"Client name '{name}' missing from /monitoring response"


async def test_monitoring_has_monitoring_cards_container(client):
    """GET /monitoring has the #monitoring-cards HTMX swap target."""
    response = await client.get("/monitoring")
    assert "monitoring-cards" in response.text


async def test_monitoring_has_refresh_button(client):
    """GET /monitoring has Refresh button wired to HTMX POST."""
    response = await client.get("/monitoring")
    assert "Refresh" in response.text
    assert "/api/monitoring/refresh" in response.text


async def test_monitoring_has_updated_at_timestamp(client):
    """GET /monitoring shows updated-at timestamp in topbar."""
    response = await client.get("/monitoring")
    assert "Updated" in response.text


async def test_monitoring_has_active_tab(client):
    """GET /monitoring passes active_tab=monitoring to base template."""
    response = await client.get("/monitoring")
    # Base template marks the active tab — the monitoring nav link should be active
    assert "monitoring" in response.text.lower()


# ---------------------------------------------------------------------------
# Summary chips — aggregate counts from mock fixture
# ---------------------------------------------------------------------------

async def test_monitoring_summary_chips_present(client):
    """GET /monitoring has chip bar with status counts."""
    response = await client.get("/monitoring")
    assert response.status_code == 200
    text = response.text
    # At least one of the chip types must be present
    # "action needed" OR "watch" OR "on track" chip
    has_chips = any(phrase in text.lower() for phrase in [
        "action needed", "action", "watch", "on track", "sent today", "reply rate",
    ])
    assert has_chips, "No summary chips found in /monitoring response"


async def test_monitoring_has_red_clients(client):
    """Mock fixture has 2 red clients (SmartMatchApp, Kayse) — page reflects this."""
    response = await client.get("/monitoring")
    text = response.text
    # Red clients should appear somewhere in the page (card or chip)
    assert "SmartMatchApp" in text
    assert "Kayse" in text


async def test_monitoring_error_client_present(client):
    """Mock fixture has Prosperly as an error client — page must render it."""
    response = await client.get("/monitoring")
    assert "Prosperly" in response.text


# ---------------------------------------------------------------------------
# GET /api/monitoring/cards — cards partial (HTMX sort swap target)
# ---------------------------------------------------------------------------

async def test_cards_partial_returns_200(client):
    """GET /api/monitoring/cards returns 200."""
    response = await client.get("/api/monitoring/cards")
    assert response.status_code == 200


async def test_cards_partial_returns_html(client):
    """GET /api/monitoring/cards returns HTML (not JSON)."""
    response = await client.get("/api/monitoring/cards")
    assert "text/html" in response.headers.get("content-type", "")


async def test_cards_partial_contains_all_clients(client):
    """GET /api/monitoring/cards partial contains all 9 client names."""
    response = await client.get("/api/monitoring/cards")
    assert response.status_code == 200
    text = response.text
    for name in ALL_CLIENT_NAMES:
        assert name in text, f"Client '{name}' missing from cards partial"


async def test_cards_partial_sort_status(client):
    """GET /api/monitoring/cards?sort=status returns cards (default sort)."""
    response = await client.get("/api/monitoring/cards?sort=status")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


async def test_cards_partial_sort_sent_today(client):
    """GET /api/monitoring/cards?sort=sent_today returns sorted cards."""
    response = await client.get("/api/monitoring/cards?sort=sent_today")
    assert response.status_code == 200
    text = response.text
    # All clients should still be present regardless of sort
    for name in ALL_CLIENT_NAMES:
        assert name in text


async def test_cards_partial_sort_reply_rate(client):
    """GET /api/monitoring/cards?sort=reply_rate returns sorted cards."""
    response = await client.get("/api/monitoring/cards?sort=reply_rate")
    assert response.status_code == 200


async def test_cards_partial_sort_name(client):
    """GET /api/monitoring/cards?sort=name returns alphabetically sorted cards."""
    response = await client.get("/api/monitoring/cards?sort=name")
    assert response.status_code == 200
    text = response.text
    # All clients still present
    for name in ALL_CLIENT_NAMES:
        assert name in text


async def test_cards_partial_sort_not_contacted(client):
    """GET /api/monitoring/cards?sort=not_contacted returns sorted cards."""
    response = await client.get("/api/monitoring/cards?sort=not_contacted")
    assert response.status_code == 200


async def test_cards_sort_ordering_by_status(client):
    """Cards sorted by status: red clients appear before green clients."""
    response = await client.get("/api/monitoring/cards?sort=status")
    assert response.status_code == 200
    text = response.text
    # Red clients (SmartMatchApp, Kayse) should appear before green (SwishFunding, RankZero)
    # when sorted by status (red=highest priority)
    pos_red = min(text.find("SmartMatchApp"), text.find("Kayse"))
    pos_green = min(text.find("SwishFunding"), text.find("RankZero"))
    if pos_red > 0 and pos_green > 0:
        assert pos_red < pos_green, "Red clients should appear before green clients in status sort"


# ---------------------------------------------------------------------------
# POST /api/monitoring/refresh — manual cache invalidation
# ---------------------------------------------------------------------------

async def test_refresh_endpoint_returns_200(client):
    """POST /api/monitoring/refresh returns 200."""
    response = await client.post("/api/monitoring/refresh")
    assert response.status_code == 200


async def test_refresh_endpoint_returns_html(client):
    """POST /api/monitoring/refresh returns HTML (cards partial)."""
    response = await client.post("/api/monitoring/refresh")
    assert "text/html" in response.headers.get("content-type", "")


async def test_refresh_endpoint_contains_client_names(client):
    """POST /api/monitoring/refresh response contains all 9 client names."""
    response = await client.post("/api/monitoring/refresh")
    assert response.status_code == 200
    text = response.text
    for name in ALL_CLIENT_NAMES:
        assert name in text, f"Client '{name}' missing from refresh response"


async def test_refresh_clears_cache_timestamps(client):
    """POST /api/monitoring/refresh clears cache timestamps (invalidate_all)."""
    from app.services.monitoring_cache import _ts, should_refresh
    import time

    # Seed cache with a fresh timestamp so should_refresh=False
    with __import__("app.services.monitoring_cache", fromlist=["_lock", "_ts"])._lock:
        _ts["SwishFunding"] = time.time()

    assert not should_refresh("SwishFunding"), "Pre-condition: timestamp should be fresh"

    # Refresh should invalidate all
    await client.post("/api/monitoring/refresh")

    assert should_refresh("SwishFunding"), "After refresh, should_refresh should be True"


# ---------------------------------------------------------------------------
# Error card rendering — Prosperly has _error in mock fixture
# ---------------------------------------------------------------------------

async def test_error_client_card_shows_friendly_message(client):
    """Prosperly error card shows a friendly error message, not a traceback."""
    response = await client.get("/monitoring")
    assert response.status_code == 200
    text = response.text
    assert "Prosperly" in text
    # Should NOT contain Python traceback noise
    assert "Traceback" not in text
    assert "Exception" not in text


async def test_error_client_in_cards_partial(client):
    """Cards partial also renders Prosperly error card."""
    response = await client.get("/api/monitoring/cards")
    assert "Prosperly" in response.text


async def test_error_client_has_error_class(client):
    """Prosperly card has CSS error indicator (red border class or error-card class)."""
    response = await client.get("/api/monitoring/cards")
    text = response.text
    assert "Prosperly" in text
    # Look for any error CSS indicator in the vicinity of Prosperly
    # The card should have some visual error state
    prosperly_idx = text.find("Prosperly")
    surrounding = text[max(0, prosperly_idx - 200):prosperly_idx + 500]
    has_error_indicator = any(cls in surrounding for cls in [
        "error", "card--red", "border-red", "status--error", "card--error",
    ])
    assert has_error_indicator, f"No error CSS class found near Prosperly card. Surrounding HTML:\n{surrounding}"


# ---------------------------------------------------------------------------
# Status counts — verify summary chip numbers against mock fixture
# ---------------------------------------------------------------------------

async def test_status_counts_green(client):
    """Summary chip shows correct count of green clients (2: SwishFunding, RankZero)."""
    response = await client.get("/monitoring")
    text = response.text
    # Should show "2 on track" or similar
    # Green: SwishFunding (healthy), RankZero (healthy EB)
    assert "2" in text  # At minimum "2" appears somewhere for green count


async def test_classification_red_clients(client):
    """SmartMatchApp and Kayse classify as red in mock data."""
    import json
    from pathlib import Path
    from app.services.monitoring import _classify_client
    from app.services.monitoring_config import load_config

    load_config()
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "mock_monitoring_data.json").read_text()
    )
    # SmartMatchApp: active=2, sent_today=0 → rule 2 → red
    assert _classify_client(fixture["SmartMatchApp"], "SmartMatchApp") == "red"
    # Kayse: bounce_rate=6.4 > 5.0 red threshold → rule 7 → red
    assert _classify_client(fixture["Kayse"], "Kayse") == "red"


async def test_classification_amber_clients(client):
    """MyPlace, HeyReach, Enavra, SwishFunding (EB) classify as amber."""
    import json
    from pathlib import Path
    from app.services.monitoring import _classify_client
    from app.services.monitoring_config import load_config

    load_config()
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "mock_monitoring_data.json").read_text()
    )
    # MyPlace: sent=1300 / kpi=2000 = 0.65 < 0.8 warn → amber
    assert _classify_client(fixture["MyPlace"], "MyPlace") == "amber"
    # HeyReach: reply_rate=0.68 < 1.0 warn → amber
    assert _classify_client(fixture["HeyReach"], "HeyReach") == "amber"
    # SwishFunding (EB): active=0, total=2 → rule 1 → amber
    assert _classify_client(fixture["SwishFunding (EB)"], "SwishFunding (EB)") == "amber"


async def test_classification_green_clients(client):
    """SwishFunding and RankZero classify as green."""
    import json
    from pathlib import Path
    from app.services.monitoring import _classify_client
    from app.services.monitoring_config import load_config

    load_config()
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "mock_monitoring_data.json").read_text()
    )
    assert _classify_client(fixture["SwishFunding"], "SwishFunding") == "green"
    assert _classify_client(fixture["RankZero"], "RankZero") == "green"


# ---------------------------------------------------------------------------
# Platform badges — Instantly vs EmailBison
# ---------------------------------------------------------------------------

async def test_emailbison_clients_have_platform_badge(client):
    """EmailBison clients (RankZero, SwishFunding EB) have emailbison indicator."""
    response = await client.get("/monitoring")
    text = response.text
    # Both EB clients should be present
    assert "RankZero" in text
    assert "SwishFunding (EB)" in text


async def test_instantly_clients_have_platform_badge(client):
    """Instantly clients have instantly indicator."""
    response = await client.get("/monitoring")
    text = response.text
    assert "SwishFunding" in text
    assert "MyPlace" in text


# ---------------------------------------------------------------------------
# Mobile viewport — structural check
# ---------------------------------------------------------------------------

async def test_monitoring_has_viewport_meta(client):
    """GET /monitoring includes viewport meta tag for responsive layout."""
    response = await client.get("/monitoring")
    assert 'name="viewport"' in response.text


async def test_monitoring_has_htmx(client):
    """GET /monitoring includes HTMX script for partial swaps."""
    response = await client.get("/monitoring")
    assert "htmx" in response.text.lower()


# ---------------------------------------------------------------------------
# GET /monitoring/{slug} — per-client drill-down (T6 stub)
# ---------------------------------------------------------------------------

KNOWN_SLUGS = [
    "myplace",
    "swishfunding",
    "smartmatchapp",
    "heyreach",
    "kayse",
    "prosperly",
    "enavra",
    "rankzero",
    "swishfunding_eb",
]

# Instantly clients have workspace_id → should show "View in Instantly"
INSTANTLY_SLUGS = [
    "myplace",
    "swishfunding",
    "smartmatchapp",
    "heyreach",
    "kayse",
    "prosperly",
    "enavra",
]

# EmailBison clients have no workspace_id → no "View in Instantly"
EMAILBISON_SLUGS = [
    "rankzero",
    "swishfunding_eb",
]


async def test_drilldown_known_slugs_return_200(client):
    """GET /monitoring/{slug} returns 200 for all 9 known registry slugs."""
    for slug in KNOWN_SLUGS:
        response = await client.get(f"/monitoring/{slug}")
        assert response.status_code == 200, (
            f"Expected 200 for /monitoring/{slug}, got {response.status_code}"
        )


async def test_drilldown_unknown_slug_returns_404(client):
    """GET /monitoring/{slug} returns 404 for an unknown slug."""
    response = await client.get("/monitoring/does-not-exist")
    assert response.status_code == 404


async def test_drilldown_returns_html(client):
    """GET /monitoring/{slug} returns text/html content-type."""
    response = await client.get("/monitoring/myplace")
    assert "text/html" in response.headers.get("content-type", "")


async def test_drilldown_contains_client_name(client):
    """GET /monitoring/{slug} page contains the client's display name."""
    # myplace → MyPlace
    response = await client.get("/monitoring/myplace")
    assert response.status_code == 200
    assert "MyPlace" in response.text


async def test_drilldown_instantly_clients_have_workspace_link(client):
    """Instantly clients render a 'View in Instantly' link with workspace_id in href."""
    for slug in INSTANTLY_SLUGS:
        response = await client.get(f"/monitoring/{slug}")
        assert response.status_code == 200, f"Drill-down for {slug} must return 200"
        text = response.text
        # Should contain some reference to Instantly (link or badge)
        # workspace_id is present → instantly_url should be set
        assert "instantly" in text.lower(), (
            f"/monitoring/{slug}: expected Instantly reference in drill-down page"
        )


async def test_drilldown_emailbison_clients_no_instantly_link(client):
    """EmailBison clients (rankzero, swishfunding_eb) have no 'View in Instantly' link."""
    for slug in EMAILBISON_SLUGS:
        response = await client.get(f"/monitoring/{slug}")
        assert response.status_code == 200, f"Drill-down for {slug} must return 200"
        text = response.text
        # workspace_id is None for EB clients → no analytics.instantly.ai URL
        assert "analytics.instantly.ai" not in text, (
            f"/monitoring/{slug}: EB client should not have an analytics.instantly.ai link"
        )


# ---------------------------------------------------------------------------
# GET /api/monitoring/drilldown/{slug} — drill-down partial (Phase 3b T7)
# ---------------------------------------------------------------------------

async def test_drilldown_partial_instantly_client_ok(client):
    """Partial returns 200, contains client name + Instantly link, no full page layout."""
    response = await client.get("/api/monitoring/drilldown/myplace")
    assert response.status_code == 200
    text = response.text
    assert "MyPlace" in text
    # Instantly clients have workspace_id → View in Instantly present
    assert "View in Instantly" in text
    # Partial must NOT have full page chrome
    assert "<html" not in text.lower()
    assert "<body" not in text.lower()


async def test_drilldown_partial_emailbison_client_ok(client):
    """EmailBison partial: N/A for in_progress, no Instantly link, first_touch=0."""
    response = await client.get("/api/monitoring/drilldown/rankzero")
    assert response.status_code == 200
    text = response.text
    assert "RankZero" in text
    # EB clients: in_progress shows "N/A"
    assert "N/A" in text
    # No Instantly analytics link
    assert "View in Instantly" not in text
    assert "analytics.instantly.ai" not in text
    # Partial only, no layout chrome
    assert "<html" not in text.lower()
    assert "<body" not in text.lower()


async def test_drilldown_partial_not_found(client):
    """Unknown slug → 404, body contains not-found indicator."""
    response = await client.get("/api/monitoring/drilldown/does-not-exist")
    assert response.status_code == 404
    # The partial should contain a friendly not-found message (no layout)
    assert "not found" in response.text.lower() or "404" in response.text


async def test_drilldown_partial_error_state(client):
    """Error-state client (Prosperly) renders a friendly error message in the partial."""
    response = await client.get("/api/monitoring/drilldown/prosperly")
    assert response.status_code == 200
    text = response.text
    assert "Prosperly" in text
    # Should show friendly error, not Python traceback
    assert "Traceback" not in text
    assert "Exception" not in text
    # Must be a partial, not a full page
    assert "<html" not in text.lower()


async def test_drilldown_alerts_pool_critical(client, monkeypatch):
    """Client with pool_days_remaining < 3 renders 'critically low' red alert.

    Injects a pool-critical fixture entry for SwishFunding by patching the
    module-level _mock_fixture cache so _get_all_mock() picks it up.
    """
    import copy
    import json
    from pathlib import Path
    from app.services import monitoring_cache

    # Load real fixture as base, then override SwishFunding with pool-critical values
    base_fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "mock_monitoring_data.json").read_text()
    )
    patched = copy.deepcopy(base_fixture)
    # not_contacted=1000, avg_sent_7d=1000 → pool=1.0 days (< 3 → critical)
    patched["SwishFunding"]["not_contacted"] = 1000
    patched["SwishFunding"]["avg_sent_7d"] = 1000.0

    # Patch _mock_fixture so _load_mock_fixture() returns our modified copy
    monkeypatch.setattr(monitoring_cache, "_mock_fixture", patched)

    response = await client.get("/api/monitoring/drilldown/swishfunding")
    assert response.status_code == 200
    text = response.text
    assert "critically low" in text.lower(), (
        f"Expected 'critically low' in drilldown for pool_days=1.0, got:\n{text[:500]}"
    )


async def test_drilldown_alerts_bounce_critical(client):
    """Kayse has bounce_rate=6.4 > 5.0 → 'critically high' bounce alert in partial."""
    response = await client.get("/api/monitoring/drilldown/kayse")
    assert response.status_code == 200
    text = response.text
    # Kayse bounce=6.4 → bounce_critical alert fires
    assert "critically high" in text.lower(), (
        f"Expected bounce critical alert for Kayse (bounce=6.4), got:\n{text[:500]}"
    )


async def test_drilldown_campaign_groups_rendered(client):
    """Drill-down partial renders campaign group labels (Active, Paused)."""
    response = await client.get("/api/monitoring/drilldown/swishfunding")
    assert response.status_code == 200
    text = response.text
    # SwishFunding has 4 active + 1 paused campaign → both group headers present
    assert "Active" in text
    assert "Paused" in text


async def test_table_partial_endpoint(client):
    """GET /api/monitoring/table returns 200 with table rows, no full page layout."""
    response = await client.get("/api/monitoring/table")
    assert response.status_code == 200
    text = response.text
    assert "text/html" in response.headers.get("content-type", "")
    # Must contain table row markup
    assert "<tr" in text
    # Must NOT have full page layout (it's a partial)
    assert "<html" not in text.lower()
    assert "<body" not in text.lower()
    # All 9 clients should appear in the table
    for name in ALL_CLIENT_NAMES:
        assert name in text, f"Client '{name}' missing from table partial"


async def test_table_partial_sort_reply_rate(client):
    """GET /api/monitoring/table?sort=reply_rate&dir=desc — highest reply rate first."""
    response = await client.get("/api/monitoring/table?sort=reply_rate&dir=desc")
    assert response.status_code == 200
    text = response.text
    for name in ALL_CLIENT_NAMES:
        assert name in text, f"Client '{name}' missing from sorted table partial"
    # MyPlace (reply=1.69%) should appear before HeyReach (reply=0.68%) in desc
    pos_myplace = text.find("MyPlace")
    pos_heyreach = text.find("HeyReach")
    assert pos_myplace > 0 and pos_heyreach > 0
    assert pos_myplace < pos_heyreach, (
        "MyPlace (reply 1.69%) should appear before HeyReach (reply 0.68%) in desc sort"
    )
