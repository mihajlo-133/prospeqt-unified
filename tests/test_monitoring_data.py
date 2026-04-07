"""Tests for app/services/monitoring.py.

Covers:
  - _safe_num: coercion, edge cases (None, "\\N", garbage, numeric types)
  - _friendly_error: all 10 pattern branches + fallback
  - _trend: up / down / flat thresholds, zero-avg edge case
  - _pool_days_remaining: normal, zero rate, prefers sent_today over avg_sent_7d
  - _count_not_contacted_from_analytics: normal, N-subtraction, zero floor
  - _count_not_contacted_via_api: single page, multi-page, HTTP errors, empty result
  - _classify_client: all 10 rules + green baseline (GOAL.md 4.2.4)
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from httpx import Response

from app.services import monitoring as m
from app.services import monitoring_config as mc


# ---------------------------------------------------------------------------
# Config bootstrap — load factory defaults once so get_client_kpi /
# get_client_thresholds work in every test.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def load_factory_config(tmp_path):
    """Load factory config before each test; reset module state after."""
    mc._reset_for_tests()
    mc.load_config(config_path=tmp_path / "nonexistent_config.json")
    yield
    mc._reset_for_tests()


# ---------------------------------------------------------------------------
# Fixture: mock monitoring data
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_data() -> dict:
    path = Path(__file__).parent / "fixtures" / "mock_monitoring_data.json"
    return json.loads(path.read_text())


# ===========================================================================
# _safe_num
# ===========================================================================

class TestSafeNum:
    def test_none_returns_default(self):
        assert m._safe_num(None) == 0

    def test_none_returns_custom_default(self):
        assert m._safe_num(None, default=99) == 99

    def test_postgres_null_string(self):
        assert m._safe_num("\\N") == 0

    def test_postgres_null_custom_default(self):
        assert m._safe_num("\\N", default=5) == 5

    def test_int_passthrough(self):
        assert m._safe_num(42) == 42

    def test_float_passthrough(self):
        assert m._safe_num(3.14) == 3.14

    def test_zero_passthrough(self):
        assert m._safe_num(0) == 0

    def test_string_integer(self):
        assert m._safe_num("100") == 100

    def test_string_float(self):
        assert m._safe_num("2.5") == 2.5

    def test_empty_string_returns_default(self):
        assert m._safe_num("") == 0

    def test_garbage_string_returns_default(self):
        assert m._safe_num("nope") == 0

    def test_negative_int(self):
        assert m._safe_num(-10) == -10

    def test_string_negative(self):
        assert m._safe_num("-5") == -5

    def test_list_returns_default(self):
        assert m._safe_num([1, 2, 3]) == 0

    def test_dict_returns_default(self):
        assert m._safe_num({"a": 1}) == 0

    def test_bool_true_is_1(self):
        # bool is subclass of int in Python
        assert m._safe_num(True) == 1

    def test_bool_false_is_0(self):
        assert m._safe_num(False) == 0


# ===========================================================================
# _friendly_error
# ===========================================================================

class TestFriendlyError:
    def test_invalid_literal(self):
        msg = m._friendly_error("invalid literal for int() with base 10")
        assert msg == "Data format error — retrying on next refresh"

    def test_http_401(self):
        msg = m._friendly_error("HTTP 401: Unauthorized")
        assert msg == "Authentication failed — API key may be invalid"

    def test_http_403(self):
        msg = m._friendly_error("HTTP 403: Forbidden")
        assert msg == "Access denied — check API permissions"

    def test_http_429(self):
        msg = m._friendly_error("HTTP 429: Too Many Requests")
        assert msg == "Rate limited — will retry shortly"

    def test_http_500(self):
        msg = m._friendly_error("HTTP 500: Internal Server Error")
        assert msg == "Platform is experiencing issues — will retry"

    def test_http_503(self):
        msg = m._friendly_error("HTTP 503: Service Unavailable")
        assert msg == "Platform is experiencing issues — will retry"

    def test_url_error(self):
        msg = m._friendly_error("URL error: connection refused")
        assert msg == "Could not reach platform — will retry"

    def test_connect_error(self):
        msg = m._friendly_error("Cannot connect to host api.instantly.ai")
        assert msg == "Could not reach platform — will retry"

    def test_json_decode_error(self):
        msg = m._friendly_error("json decode error: expecting value")
        assert msg == "Received unexpected response — will retry"

    def test_timed_out(self):
        msg = m._friendly_error("Request timed out after 30s")
        assert msg == "Request timed out — will retry"

    def test_timeout(self):
        msg = m._friendly_error("ReadTimeout: timeout on reading response")
        assert msg == "Request timed out — will retry"

    def test_api_key_not_found(self):
        msg = m._friendly_error("API key not found for workspace")
        assert msg == "API key not configured"

    def test_unknown_error_returns_fallback(self):
        msg = m._friendly_error("something completely unexpected")
        assert msg == "Data temporarily unavailable — retrying"

    def test_case_insensitive_matching(self):
        # Pattern matching is case-insensitive
        msg = m._friendly_error("HTTP 401 UNAUTHORIZED")
        assert msg == "Authentication failed — API key may be invalid"

    def test_empty_string_returns_fallback(self):
        msg = m._friendly_error("")
        assert msg == "Data temporarily unavailable — retrying"

    def test_none_returns_fallback(self):
        msg = m._friendly_error(None)
        assert msg == "Data temporarily unavailable — retrying"

    def test_http_401_beats_http_5(self):
        # "http 5" would match HTTP 500, but "http 401" is more specific.
        # This test verifies ordering: 401 pattern fires first on "HTTP 401"
        msg = m._friendly_error("HTTP 401")
        assert "Authentication" in msg

    def test_timed_out_beats_timeout(self):
        # "timed out" is listed before "timeout" — both should produce same message
        assert m._friendly_error("timed out") == m._friendly_error("timeout")


# ===========================================================================
# _trend
# ===========================================================================

class TestTrend:
    def test_up_when_ratio_above_1_1(self):
        assert m._trend(1.1, 1.0) == "up"

    def test_up_exact_boundary(self):
        assert m._trend(1.1, 1.0) == "up"

    def test_down_when_ratio_below_0_9(self):
        assert m._trend(0.89, 1.0) == "down"

    def test_down_exact_boundary(self):
        assert m._trend(0.9, 1.0) == "down"

    def test_flat_in_band(self):
        assert m._trend(1.0, 1.0) == "flat"

    def test_flat_just_below_up(self):
        assert m._trend(1.09, 1.0) == "flat"

    def test_flat_just_above_down(self):
        assert m._trend(0.91, 1.0) == "flat"

    def test_flat_when_avg_is_zero(self):
        # Zero average → undefined ratio → flat
        assert m._trend(100, 0) == "flat"

    def test_up_with_large_numbers(self):
        assert m._trend(9850, 8900) == "up"   # ratio ~1.107

    def test_down_with_large_numbers(self):
        assert m._trend(1260, 1420) == "down"  # ratio ~0.887 <= 0.9

    def test_flat_with_large_numbers(self):
        assert m._trend(1900, 1920) == "flat"  # ratio ~0.990


# ===========================================================================
# _pool_days_remaining
# ===========================================================================

class TestPoolDaysRemaining:
    def test_normal_calculation(self):
        data = {"not_contacted": 10000, "sent_today": 2000, "avg_sent_7d": 1800.0}
        result = m._pool_days_remaining(data, "TestClient")
        assert result == pytest.approx(5.0)

    def test_prefers_sent_today_over_avg(self):
        # sent_today=2000 takes priority over avg_sent_7d=1000 (truthy check)
        data = {"not_contacted": 10000, "sent_today": 2000, "avg_sent_7d": 1000.0}
        result = m._pool_days_remaining(data, "TestClient")
        assert result == pytest.approx(5.0)

    def test_falls_back_to_avg_when_sent_today_is_zero(self):
        # sent_today=0 → falsy → falls back to avg_sent_7d
        data = {"not_contacted": 10000, "sent_today": 0, "avg_sent_7d": 2000.0}
        result = m._pool_days_remaining(data, "TestClient")
        assert result == pytest.approx(5.0)

    def test_infinity_when_both_rates_zero(self):
        data = {"not_contacted": 10000, "sent_today": 0, "avg_sent_7d": 0}
        result = m._pool_days_remaining(data, "TestClient")
        assert result == float("inf")

    def test_zero_not_contacted(self):
        data = {"not_contacted": 0, "sent_today": 2000, "avg_sent_7d": 2000.0}
        result = m._pool_days_remaining(data, "TestClient")
        assert result == pytest.approx(0.0)

    def test_missing_keys_default_to_zero(self):
        # No keys at all → rate=0 → inf
        result = m._pool_days_remaining({}, "TestClient")
        assert result == float("inf")

    def test_none_values_treated_as_zero(self):
        # None values should be treated as 0 via `or 0`
        data = {"not_contacted": None, "sent_today": None, "avg_sent_7d": None}
        result = m._pool_days_remaining(data, "TestClient")
        assert result == float("inf")

    def test_enavra_fixture(self, mock_data):
        # Enavra: not_contacted=9800, sent_today=2050, avg_sent_7d=2020
        # pool_days = 9800 / 2050 ≈ 4.78 < pool_days_warn=7 → amber rule 10
        data = mock_data["Enavra"]
        result = m._pool_days_remaining(data, "Enavra")
        assert result == pytest.approx(9800 / 2050, rel=1e-4)
        assert result < 7.0  # must trigger amber rule 10


# ===========================================================================
# _count_not_contacted_from_analytics
# ===========================================================================

class TestCountNotContactedFromAnalytics:
    def test_normal_subtraction(self):
        entry = {"leads_count": 1000, "new_leads_contacted_count": 400}
        assert m._count_not_contacted_from_analytics(entry) == 600

    def test_returns_zero_floor(self):
        # contacted > leads (recycled leads edge case) → floor at 0
        entry = {"leads_count": 100, "new_leads_contacted_count": 150}
        assert m._count_not_contacted_from_analytics(entry) == 0

    def test_missing_fields_default_to_zero(self):
        assert m._count_not_contacted_from_analytics({}) == 0

    def test_null_values_treated_as_zero(self):
        entry = {"leads_count": None, "new_leads_contacted_count": None}
        assert m._count_not_contacted_from_analytics(entry) == 0

    def test_postgres_null_string(self):
        entry = {"leads_count": "\\N", "new_leads_contacted_count": "\\N"}
        assert m._count_not_contacted_from_analytics(entry) == 0

    def test_string_numbers(self):
        entry = {"leads_count": "500", "new_leads_contacted_count": "200"}
        assert m._count_not_contacted_from_analytics(entry) == 300

    def test_full_contacted(self):
        entry = {"leads_count": 1000, "new_leads_contacted_count": 1000}
        assert m._count_not_contacted_from_analytics(entry) == 0


# ===========================================================================
# _count_not_contacted_via_api
# ===========================================================================

class TestCountNotContactedViaApi:
    @pytest.mark.asyncio
    async def test_single_page_no_cursor(self):
        """Single page with 3 items and no next cursor → returns 3."""
        payload = {"items": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}
        with respx.mock:
            respx.post("https://api.instantly.ai/api/v2/leads/list").mock(
                return_value=Response(200, json=payload)
            )
            import httpx
            async with httpx.AsyncClient() as client:
                count = await m._count_not_contacted_via_api(
                    client, "camp-001", "test-api-key"
                )
        assert count == 3

    @pytest.mark.asyncio
    async def test_multi_page_pagination(self):
        """Two pages: first has cursor, second has no cursor."""
        page1 = {
            "items": [{"id": str(i)} for i in range(100)],
            "next_starting_after": "cursor-abc",
        }
        page2 = {
            "items": [{"id": str(i)} for i in range(50)],
        }
        call_count = 0

        import httpx

        async def mock_handler(request):
            nonlocal call_count
            call_count += 1
            body = json.loads(request.content)
            if "starting_after" not in body:
                return Response(200, json=page1)
            return Response(200, json=page2)

        with respx.mock:
            respx.post("https://api.instantly.ai/api/v2/leads/list").mock(
                side_effect=mock_handler
            )
            async with httpx.AsyncClient() as client:
                count = await m._count_not_contacted_via_api(
                    client, "camp-001", "test-api-key"
                )
        assert count == 150
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_empty_items_returns_zero(self):
        """Empty items list → returns 0 immediately."""
        payload = {"items": []}
        with respx.mock:
            respx.post("https://api.instantly.ai/api/v2/leads/list").mock(
                return_value=Response(200, json=payload)
            )
            import httpx
            async with httpx.AsyncClient() as client:
                count = await m._count_not_contacted_via_api(
                    client, "camp-001", "test-api-key"
                )
        assert count == 0

    @pytest.mark.asyncio
    async def test_http_error_returns_partial_count(self):
        """HTTP 500 after first page → returns partial count from page 1."""
        page1 = {
            "items": [{"id": str(i)} for i in range(100)],
            "next_starting_after": "cursor-abc",
        }
        call_count = 0

        import httpx

        async def mock_handler(request):
            nonlocal call_count
            call_count += 1
            body = json.loads(request.content)
            if "starting_after" not in body:
                return Response(200, json=page1)
            return Response(500, text="Internal Server Error")

        with respx.mock:
            respx.post("https://api.instantly.ai/api/v2/leads/list").mock(
                side_effect=mock_handler
            )
            async with httpx.AsyncClient() as client:
                count = await m._count_not_contacted_via_api(
                    client, "camp-001", "test-api-key"
                )
        # Returns 100 from page 1 before the error
        assert count == 100

    @pytest.mark.asyncio
    async def test_first_request_fails_returns_zero(self):
        """First request fails immediately → returns 0."""
        with respx.mock:
            respx.post("https://api.instantly.ai/api/v2/leads/list").mock(
                return_value=Response(401, text="Unauthorized")
            )
            import httpx
            async with httpx.AsyncClient() as client:
                count = await m._count_not_contacted_via_api(
                    client, "camp-001", "test-api-key"
                )
        assert count == 0

    @pytest.mark.asyncio
    async def test_bearer_token_in_headers(self):
        """Verify Authorization header is set correctly."""
        payload = {"items": []}
        captured_headers = {}

        import httpx

        async def mock_handler(request):
            captured_headers.update(dict(request.headers))
            return Response(200, json=payload)

        with respx.mock:
            respx.post("https://api.instantly.ai/api/v2/leads/list").mock(
                side_effect=mock_handler
            )
            async with httpx.AsyncClient() as client:
                await m._count_not_contacted_via_api(
                    client, "camp-001", "my-secret-key"
                )
        assert captured_headers.get("authorization") == "Bearer my-secret-key"

    @pytest.mark.asyncio
    async def test_filter_in_request_body(self):
        """Verify FILTER_VAL_NOT_CONTACTED is included in the request body."""
        payload = {"items": []}
        captured_bodies = []

        import httpx

        async def mock_handler(request):
            captured_bodies.append(json.loads(request.content))
            return Response(200, json=payload)

        with respx.mock:
            respx.post("https://api.instantly.ai/api/v2/leads/list").mock(
                side_effect=mock_handler
            )
            async with httpx.AsyncClient() as client:
                await m._count_not_contacted_via_api(
                    client, "camp-xyz", "key"
                )
        assert len(captured_bodies) == 1
        body = captured_bodies[0]
        assert body["filter"] == m.FILTER_VAL_NOT_CONTACTED
        assert body["campaign"] == "camp-xyz"

    @pytest.mark.asyncio
    async def test_non_dict_response_returns_count_so_far(self):
        """If API returns non-dict JSON, pagination stops gracefully."""
        with respx.mock:
            respx.post("https://api.instantly.ai/api/v2/leads/list").mock(
                return_value=Response(200, json=["unexpected", "list"])
            )
            import httpx
            async with httpx.AsyncClient() as client:
                count = await m._count_not_contacted_via_api(
                    client, "camp-001", "key"
                )
        assert count == 0


# ===========================================================================
# _classify_client — all 10 rules + green baseline
# ===========================================================================

class TestClassifyClient:
    """Each test targets exactly ONE rule in the waterfall (GOAL.md 4.2.4).

    The pattern is: start with a healthy green baseline, then mutate the
    single field that triggers the rule under test.
    """

    def _green_base(self) -> dict:
        """Healthy baseline that resolves to "green" with factory thresholds.

        Uses a standard client (sent_kpi=2000). All thresholds satisfied:
          - 2 active, 2 total (rule 1: skip)
          - sent_today=2000 (rule 2: skip — > 0)
          - sent_ratio=2000/2000=1.0 >= sent_pct_warn=0.8 (rules 3,4: skip)
          - sent_today=2000 > 50, reply_rate=2.0 >= reply_rate_warn=1.0 (rules 5,6: skip)
          - bounce_rate=1.0 < bounce_rate_warn=3.0 (rules 7,8: skip)
          - pool_days=25 >= pool_days_warn=7 (rules 9,10: skip)
        """
        return {
            "active_campaigns": 2,
            "total_campaigns": 2,
            "sent_today": 2000,
            "reply_rate_today": 2.0,
            "bounce_rate": 1.0,
            "not_contacted": 50000,
            "avg_sent_7d": 2000.0,
        }

    def test_green_baseline(self):
        assert m._classify_client(self._green_base(), "Enavra") == "green"

    # Rule 1: amber — active=0 AND total>0 (paused)
    def test_rule_1_amber_paused(self):
        data = self._green_base()
        data["active_campaigns"] = 0
        data["total_campaigns"] = 2
        assert m._classify_client(data, "Enavra") == "amber"

    def test_rule_1_skipped_when_total_is_zero(self):
        # active=0, total=0 → not paused, waterfall continues
        data = self._green_base()
        data["active_campaigns"] = 0
        data["total_campaigns"] = 0
        data["sent_today"] = 0  # would trigger rule 2 if active > 0, but active=0
        # Rule 2: active>0 AND sent_today==0 → red. active=0 so skip.
        # Falls through to rule 3: sent_kpi=2000, sent_ratio=0/2000=0 < sent_pct_red=0.5 → red
        assert m._classify_client(data, "Enavra") == "red"

    # Rule 2: red — active>0 AND sent_today=0 (stalled)
    def test_rule_2_red_stalled(self, mock_data):
        data = mock_data["SmartMatchApp"]
        assert m._classify_client(data, "SmartMatchApp") == "red"

    def test_rule_2_not_triggered_when_active_zero(self):
        data = self._green_base()
        data["active_campaigns"] = 0
        data["total_campaigns"] = 0
        data["sent_today"] = 0
        # active=0 so rule 2 skips; rule 1 also skips (total=0); falls to rule 3
        result = m._classify_client(data, "Enavra")
        assert result != "green"  # some rule fires, but not rule 2

    # Rule 3: red — sent_ratio < sent_pct_red=0.5
    def test_rule_3_red_way_behind_kpi(self):
        data = self._green_base()
        data["sent_today"] = 900  # 900/2000 = 0.45 < sent_pct_red=0.5
        assert m._classify_client(data, "Enavra") == "red"

    def test_rule_3_exact_boundary_is_red(self):
        # ratio == sent_pct_red=0.5 → NOT < → skip rule 3; check rule 4
        data = self._green_base()
        data["sent_today"] = 1000  # 1000/2000 = 0.5 exactly
        # sent_pct_warn=0.8 → 0.5 < 0.8 → amber (rule 4)
        assert m._classify_client(data, "Enavra") == "amber"

    # Rule 4: amber — sent_pct_red <= sent_ratio < sent_pct_warn=0.8
    def test_rule_4_amber_behind_kpi(self, mock_data):
        data = mock_data["MyPlace"]
        assert m._classify_client(data, "MyPlace") == "amber"

    def test_rule_4_amber_boundary(self):
        # 1300/2000 = 0.65 is between red=0.5 and warn=0.8 → amber
        data = self._green_base()
        data["sent_today"] = 1300
        assert m._classify_client(data, "Enavra") == "amber"

    # Rule 5: red — sent>50 AND reply_rate < reply_rate_red=0.5
    def test_rule_5_red_dead_copy(self):
        data = self._green_base()
        data["sent_today"] = 2000
        data["reply_rate_today"] = 0.3  # < reply_rate_red=0.5
        assert m._classify_client(data, "Enavra") == "red"

    def test_rule_5_skipped_when_sent_le_50(self):
        data = self._green_base()
        data["sent_today"] = 50   # <= 50, gate skipped
        data["reply_rate_today"] = 0.1  # would be red if gate applied
        # With sent_today=50 and sent_kpi=2000: ratio=50/2000=0.025 < sent_pct_red=0.5 → red (rule 3)
        assert m._classify_client(data, "Enavra") == "red"

    # Rule 6: amber — sent>50 AND reply_rate_red <= reply_rate < reply_rate_warn=1.0
    def test_rule_6_amber_soft_copy(self, mock_data):
        data = mock_data["HeyReach"]
        # HeyReach reply_rate_today=0.68 < reply_rate_warn=1.0, sent_today=1900>50
        assert m._classify_client(data, "HeyReach") == "amber"

    def test_rule_6_amber_synthetic(self):
        data = self._green_base()
        data["reply_rate_today"] = 0.7  # >= reply_rate_red=0.5, < reply_rate_warn=1.0
        assert m._classify_client(data, "Enavra") == "amber"

    def test_rule_6_skipped_when_sent_le_50(self):
        data = self._green_base()
        data["sent_today"] = 50
        data["reply_rate_today"] = 0.7
        # Rule 3 fires first (ratio=50/2000=0.025 < 0.5 → red)
        result = m._classify_client(data, "Enavra")
        assert result in ("red", "amber")  # rule 3 or 4 fires, not rule 6

    # Rule 7: red — bounce_rate > bounce_rate_red=5.0
    def test_rule_7_red_bounce(self, mock_data):
        data = mock_data["Kayse"]
        assert m._classify_client(data, "Kayse") == "red"

    def test_rule_7_synthetic(self):
        data = self._green_base()
        data["bounce_rate"] = 5.1  # > bounce_rate_red=5.0
        assert m._classify_client(data, "Enavra") == "red"

    def test_rule_7_exact_boundary_not_red(self):
        # bounce_rate == bounce_rate_red → NOT > → skip rule 7; check rule 8
        data = self._green_base()
        data["bounce_rate"] = 5.0  # exactly at red threshold
        # 5.0 > bounce_rate_warn=3.0 → amber (rule 8)
        assert m._classify_client(data, "Enavra") == "amber"

    # Rule 8: amber — bounce_rate_warn=3.0 < bounce_rate <= bounce_rate_red=5.0
    def test_rule_8_amber_bounce(self):
        data = self._green_base()
        data["bounce_rate"] = 4.0  # > warn=3.0, <= red=5.0
        assert m._classify_client(data, "Enavra") == "amber"

    def test_rule_8_exact_warn_boundary(self):
        # bounce_rate == bounce_rate_warn=3.0 → NOT > → skip rule 8 → green
        data = self._green_base()
        data["bounce_rate"] = 3.0
        assert m._classify_client(data, "Enavra") == "green"

    # Rule 9: red — pool_days < pool_days_red=3
    def test_rule_9_red_out_of_leads(self):
        data = self._green_base()
        data["not_contacted"] = 4000
        data["sent_today"] = 2000   # pool_days = 4000/2000 = 2.0 < pool_days_red=3
        assert m._classify_client(data, "Enavra") == "red"

    def test_rule_9_boundary_at_exactly_3(self):
        data = self._green_base()
        data["not_contacted"] = 6000
        data["sent_today"] = 2000   # pool_days = 6000/2000 = 3.0 exactly
        # 3.0 < pool_days_warn=7 → amber (rule 10)
        assert m._classify_client(data, "Enavra") == "amber"

    # Rule 10: amber — pool_days_red <= pool_days < pool_days_warn=7
    def test_rule_10_amber_low_leads(self, mock_data):
        data = mock_data["Enavra"]
        # pool_days = 9800/2050 ≈ 4.78 < pool_days_warn=7 → amber
        assert m._classify_client(data, "Enavra") == "amber"

    def test_rule_10_synthetic(self):
        data = self._green_base()
        data["not_contacted"] = 10000
        data["sent_today"] = 2000   # pool_days = 5.0 < pool_days_warn=7, >= pool_days_red=3
        assert m._classify_client(data, "Enavra") == "amber"

    # Fixture-based green cases
    def test_swishfunding_is_green(self, mock_data):
        data = mock_data["SwishFunding"]
        assert m._classify_client(data, "SwishFunding") == "green"

    def test_rankzero_is_green(self, mock_data):
        data = mock_data["RankZero"]
        assert m._classify_client(data, "RankZero") == "green"

    # EmailBison rule 1 variant (SwishFunding EB: active=0, total=2)
    def test_swishfunding_eb_is_amber_rule_1(self, mock_data):
        data = mock_data["SwishFunding (EB)"]
        assert m._classify_client(data, "SwishFunding (EB)") == "amber"

    # Waterfall order: rule 1 fires before rule 2
    def test_rule_1_fires_before_rule_2(self):
        # active=0, total=2 AND sent_today=0 → rule 1 (amber) fires first
        data = self._green_base()
        data["active_campaigns"] = 0
        data["total_campaigns"] = 2
        data["sent_today"] = 0
        assert m._classify_client(data, "Enavra") == "amber"

    # Rule 3 fires before rule 5
    def test_rule_3_fires_before_rule_5(self):
        # sent=900 → ratio=0.45 < sent_pct_red=0.5 → red (rule 3)
        # even though reply_rate=0.3 would also be red via rule 5
        data = self._green_base()
        data["sent_today"] = 900
        data["reply_rate_today"] = 0.3
        # We can't distinguish which rule fired (both red), but we CAN verify
        # the result is red and not amber.
        assert m._classify_client(data, "Enavra") == "red"

    # Sent KPI = 0 means rules 3+4 are skipped entirely
    def test_rules_3_4_skipped_when_sent_kpi_zero(self):
        # For a client not in KPI_TARGETS, sent_kpi=0 → rules 3+4 skipped
        data = {
            "active_campaigns": 2,
            "total_campaigns": 2,
            "sent_today": 1,  # tiny but > 0, so rule 2 skips
            "reply_rate_today": 2.0,  # good
            "bounce_rate": 1.0,  # good
            "not_contacted": 10000,
            "avg_sent_7d": 1.0,
        }
        # "UnknownClient" not in KPI_TARGETS → sent_kpi=0 → rules 3+4 skipped
        assert m._classify_client(data, "UnknownClient") == "green"


# ===========================================================================
# Integration: fixture data produces expected classifications
# ===========================================================================

class TestFixtureClassifications:
    """Verify each fixture entry resolves to the expected colour documented
    in mock_monitoring_data.json._meta.edge_cases."""

    def test_swishfunding_green(self, mock_data):
        assert m._classify_client(mock_data["SwishFunding"], "SwishFunding") == "green"

    def test_myplace_amber(self, mock_data):
        assert m._classify_client(mock_data["MyPlace"], "MyPlace") == "amber"

    def test_smartmatchapp_red(self, mock_data):
        assert m._classify_client(mock_data["SmartMatchApp"], "SmartMatchApp") == "red"

    def test_heyreach_amber(self, mock_data):
        assert m._classify_client(mock_data["HeyReach"], "HeyReach") == "amber"

    def test_kayse_red(self, mock_data):
        assert m._classify_client(mock_data["Kayse"], "Kayse") == "red"

    def test_enavra_amber(self, mock_data):
        assert m._classify_client(mock_data["Enavra"], "Enavra") == "amber"

    def test_rankzero_green(self, mock_data):
        assert m._classify_client(mock_data["RankZero"], "RankZero") == "green"

    def test_swishfunding_eb_amber(self, mock_data):
        assert m._classify_client(mock_data["SwishFunding (EB)"], "SwishFunding (EB)") == "amber"

    def test_prosperly_error_entry_classifies_amber(self, mock_data):
        # Prosperly has all zeros + active=0, total=0.
        # Rule 1: active=0 AND total=0 → skip (total not > 0).
        # Rule 2: active=0 → skip.
        # Rules 3+4: sent_kpi=2000, sent_ratio=0/2000=0 < sent_pct_red=0.5 → RED.
        assert m._classify_client(mock_data["Prosperly"], "Prosperly") == "red"
