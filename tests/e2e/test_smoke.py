"""Smoke test — verifies the monitoring page loads at all.

This is the bootstrapping test that confirms:
1. The e2e server fixture starts correctly
2. The /monitoring route returns an HTML page
3. The page title contains "Prospeqt" or "Monitoring"
"""
import re

from playwright.sync_api import Page, expect


def test_monitoring_loads(page: Page, live_server_url: str):
    """Smoke: /monitoring page loads and title contains 'Prospeqt' or 'Monitoring'."""
    page.goto(f"{live_server_url}/monitoring")
    expect(page).to_have_title(re.compile(r"Prospeqt|Monitoring", re.IGNORECASE))
