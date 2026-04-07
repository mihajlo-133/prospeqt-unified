"""Unit tests for Phase 3b monitoring_view helpers.

Tests cover:
  compute_alerts        — 8 alert conditions (3.5.1-3.5.8)
  group_campaigns_by_status — grouping + sort order
  build_campaign_row_view — per-campaign EmailBison normalization

All tests are pure Python (no HTTP, no cache, no fixtures required).
Ad-hoc client dicts are constructed inline where the mock fixture doesn't
cover the required edge case (e.g. pool_critical, reply_very_low).
"""
from __future__ import annotations

import pytest

from app.services.monitoring_view import (
    build_campaign_row_view,
    compute_alerts,
    group_campaigns_by_status,
)


# ---------------------------------------------------------------------------
# Helpers — minimal valid entry dicts
# ---------------------------------------------------------------------------

def _entry(**kwargs) -> dict:
    """Return a minimal healthy-client entry, overridable via kwargs."""
    base = {
        "platform": "instantly",
        "status": "green",
        "active_campaigns": 3,
        "total_campaigns": 3,
        "sent_today": 2000,
        "first_touch_today": 1200,
        "followup_today": 800,
        "replies_today": 30,
        "opps_today": 3,
        "reply_rate_today": 1.5,
        "reply_rate_7d": 1.4,
        "bounce_rate": 1.0,
        "not_contacted": 50000,
        "in_progress": 8000,
        "avg_sent_7d": 2000.0,
        "avg_replies_7d": 28.0,
        "avg_opps_7d": 2.5,
        "opp_trend": "flat",
        "reply_trend": "flat",
        "sent_trend": "flat",
        "campaigns": [],
        "daily": [],
        "kpi": {"sent": 2000},
        "thresholds": {},
    }
    base.update(kwargs)
    return base


def _campaign(**kwargs) -> dict:
    base = {
        "name": "Test Campaign",
        "id": "test-001",
        "status": "active",
        "sent_today": 500,
        "first_touch": 300,
        "followups": 200,
        "replies_today": 8,
        "opps_today": 1,
        "reply_rate": 1.6,
        "not_contacted": 5000,
        "in_progress": 1000,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# compute_alerts — pool conditions (3.5.1-3.5.2)
# ---------------------------------------------------------------------------

def test_compute_alerts_pool_critical():
    """pool_days = 1000/1000 = 1.0 < 3 → red 'critically low' alert."""
    entry = _entry(not_contacted=1000, avg_sent_7d=1000.0)
    alerts = compute_alerts(entry)
    levels = [a["level"] for a in alerts]
    messages = " ".join(a["message"] for a in alerts)
    assert "red" in levels
    assert "critically low" in messages.lower()


def test_compute_alerts_pool_low():
    """pool_days = 10000/2000 = 5.0 (3 <= 5 < 7) → amber 'running low' alert."""
    entry = _entry(not_contacted=10000, avg_sent_7d=2000.0)
    alerts = compute_alerts(entry)
    assert any(a["level"] == "amber" and "running low" in a["message"].lower() for a in alerts)


def test_compute_alerts_pool_healthy():
    """pool_days = 50000/2000 = 25.0 (>= 7) → no pool alert."""
    entry = _entry(not_contacted=50000, avg_sent_7d=2000.0)
    alerts = compute_alerts(entry)
    pool_alerts = [a for a in alerts if "pool" in a["message"].lower() or "lead" in a["message"].lower()]
    assert pool_alerts == [], f"Unexpected pool alert for healthy pool: {pool_alerts}"


# ---------------------------------------------------------------------------
# compute_alerts — reply rate conditions (3.5.3-3.5.4)
# ---------------------------------------------------------------------------

def test_compute_alerts_reply_very_low():
    """reply_rate=0.3% and sent=100 > 50 → red reply rate critically low."""
    entry = _entry(reply_rate_today=0.3, sent_today=100)
    alerts = compute_alerts(entry)
    assert any(a["level"] == "red" and "critically low" in a["message"].lower() for a in alerts), (
        f"Expected red reply alert for rate=0.3%, got: {alerts}"
    )


def test_compute_alerts_reply_below_target():
    """reply_rate=0.8% and sent=100 → amber reply below target."""
    entry = _entry(reply_rate_today=0.8, sent_today=100)
    alerts = compute_alerts(entry)
    assert any(a["level"] == "amber" and "below target" in a["message"].lower() for a in alerts), (
        f"Expected amber reply alert for rate=0.8%, got: {alerts}"
    )


def test_compute_alerts_reply_skip_if_sent_low():
    """reply_rate=0.2% but sent=30 (<= 50) → no reply alert fired."""
    entry = _entry(reply_rate_today=0.2, sent_today=30)
    alerts = compute_alerts(entry)
    reply_alerts = [a for a in alerts if "reply" in a["message"].lower()]
    assert reply_alerts == [], f"Reply alert should not fire when sent=30: {reply_alerts}"


# ---------------------------------------------------------------------------
# compute_alerts — campaign active/sent conditions (3.5.5-3.5.6)
# ---------------------------------------------------------------------------

def test_compute_alerts_no_active_campaigns():
    """active=0 with total=3 → red 'No active campaigns' alert."""
    entry = _entry(active_campaigns=0, total_campaigns=3)
    alerts = compute_alerts(entry)
    assert any(
        a["level"] == "red" and "no active campaigns" in a["message"].lower()
        for a in alerts
    ), f"Expected no-active-campaigns alert, got: {alerts}"


def test_compute_alerts_active_zero_sent():
    """active=2, sent=0 → red 'Active campaigns but 0 emails sent' alert."""
    entry = _entry(active_campaigns=2, sent_today=0)
    alerts = compute_alerts(entry)
    assert any(
        a["level"] == "red" and "0 emails sent" in a["message"].lower()
        for a in alerts
    ), f"Expected active+zero-sent alert, got: {alerts}"


# ---------------------------------------------------------------------------
# compute_alerts — bounce rate conditions (3.5.7-3.5.8)
# ---------------------------------------------------------------------------

def test_compute_alerts_bounce_critical():
    """bounce_rate=6.0 > 5.0 → red 'critically high' bounce alert."""
    entry = _entry(bounce_rate=6.0)
    alerts = compute_alerts(entry)
    assert any(
        a["level"] == "red" and "critically high" in a["message"].lower()
        for a in alerts
    ), f"Expected bounce critical alert for rate=6.0, got: {alerts}"


def test_compute_alerts_bounce_elevated():
    """bounce_rate=4.0 (3 < 4 <= 5) → amber 'elevated' bounce alert."""
    entry = _entry(bounce_rate=4.0)
    alerts = compute_alerts(entry)
    assert any(
        a["level"] == "amber" and "elevated" in a["message"].lower()
        for a in alerts
    ), f"Expected bounce elevated alert for rate=4.0, got: {alerts}"


# ---------------------------------------------------------------------------
# compute_alerts — multiple and clean states
# ---------------------------------------------------------------------------

def test_compute_alerts_multiple_fire():
    """Pool critical + bounce critical both fire in the same alert list."""
    entry = _entry(
        not_contacted=500,   # pool = 500/1000 = 0.5d → critical
        avg_sent_7d=1000.0,
        bounce_rate=6.5,     # > 5.0 → critical
        sent_today=200,
    )
    alerts = compute_alerts(entry)
    levels = [a["level"] for a in alerts]
    messages = " ".join(a["message"] for a in alerts).lower()
    assert levels.count("red") >= 2, f"Expected >= 2 red alerts, got: {alerts}"
    assert "critically low" in messages
    assert "critically high" in messages


def test_compute_alerts_clean():
    """Fully healthy client → empty alert list."""
    # SwishFunding-like: pool=9.8d, reply=1.44%, bounce=1.9%, active=4, sent=9850
    entry = _entry(
        not_contacted=95000,
        avg_sent_7d=9720.0,
        reply_rate_today=1.44,
        sent_today=9850,
        bounce_rate=1.9,
        active_campaigns=4,
        total_campaigns=5,
    )
    alerts = compute_alerts(entry)
    assert alerts == [], f"Expected no alerts for healthy client, got: {alerts}"


# ---------------------------------------------------------------------------
# group_campaigns_by_status
# ---------------------------------------------------------------------------

def test_group_campaigns_by_status():
    """4 campaigns (2 active, 1 paused, 1 completed) → correct keys + sent_today DESC order."""
    campaigns = [
        _campaign(status="active",    name="A-low",  sent_today=100),
        _campaign(status="active",    name="A-high", sent_today=800),
        _campaign(status="paused",    name="P1",     sent_today=0),
        _campaign(status="completed", name="C1",     sent_today=0),
    ]
    groups = group_campaigns_by_status(campaigns, "instantly")

    keys = [g["key"] for g in groups]
    assert keys == ["active", "paused", "completed", "other"]

    active_group = next(g for g in groups if g["key"] == "active")
    assert active_group["count"] == 2
    # Sorted by sent_today DESC within group
    assert active_group["rows"][0]["name"] == "A-high"
    assert active_group["rows"][1]["name"] == "A-low"

    paused_group = next(g for g in groups if g["key"] == "paused")
    assert paused_group["count"] == 1

    completed_group = next(g for g in groups if g["key"] == "completed")
    assert completed_group["count"] == 1

    other_group = next(g for g in groups if g["key"] == "other")
    assert other_group["count"] == 0


def test_group_campaigns_open_defaults():
    """Active and Paused groups are open=True by default; Completed and Other are False."""
    campaigns = [
        _campaign(status="active"),
        _campaign(status="paused"),
        _campaign(status="completed"),
    ]
    groups = group_campaigns_by_status(campaigns, "instantly")
    group_map = {g["key"]: g for g in groups}

    assert group_map["active"]["open"] is True
    assert group_map["paused"]["open"] is True
    assert group_map["completed"]["open"] is False
    assert group_map["other"]["open"] is False


# ---------------------------------------------------------------------------
# build_campaign_row_view — EmailBison normalization
# ---------------------------------------------------------------------------

def test_build_campaign_row_view_instantly():
    """Instantly campaign row: all fields pass through, in_progress is numeric string."""
    camp = _campaign(
        status="active",
        sent_today=400,
        first_touch=250,
        followups=150,
        not_contacted=3000,
        in_progress=800,
    )
    row = build_campaign_row_view(camp, "instantly")

    assert row["first_touch"] == 250
    assert row["followups"] == 150
    assert row["not_contacted_fmt"] == "3,000"
    assert row["in_progress_fmt"] != "N/A"
    assert "800" in row["in_progress_fmt"]


def test_build_campaign_row_view_emailbison():
    """EmailBison campaign row: first_touch=0, followups=0, not_contacted=0, in_progress='N/A'."""
    camp = _campaign(
        status="active",
        sent_today=300,
        first_touch=200,    # should be zeroed
        followups=100,      # should be zeroed
        not_contacted=5000, # should be zeroed
        in_progress=600,    # should show "N/A"
    )
    row = build_campaign_row_view(camp, "emailbison")

    assert row["first_touch"] == 0
    assert row["first_touch_fmt"] == "0"
    assert row["followups"] == 0
    assert row["followups_fmt"] == "0"
    assert row["not_contacted_fmt"] == "0"
    assert row["in_progress_fmt"] == "N/A"
