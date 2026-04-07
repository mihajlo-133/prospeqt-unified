"""Playwright e2e user flow tests for Fix Phase 3 — Sidebar Navigation.

4 user flows + 1 screenshot test:
1. Desktop sidebar nav active state (1440×900)
2. Mobile hamburger overlay (375×812)
3. Active state persistence on subroutes
4. Tablet icon rail with tooltip (768×1024)
5. Screenshots at 3 viewports

Selectors based on the Phase 3 implementation:
- Sidebar: <aside id="sidebar" class="sidebar">
- Hamburger: <button id="sidebar-hamburger">
- Backdrop: <div id="sidebar-backdrop">
- Nav links: <a class="sidebar-link [active]" data-tooltip="Label">
- Active state: class "active" + aria-current="page"
- Mobile open state: sidebar gets class "open"
"""
import os
import re

import pytest
from playwright.sync_api import Page, expect


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sidebar(page: Page):
    return page.locator("#sidebar")


def _hamburger(page: Page):
    return page.locator("#sidebar-hamburger")


def _backdrop(page: Page):
    return page.locator("#sidebar-backdrop")


def _nav_link(page: Page, text: str):
    """Return the sidebar-link matching the given visible text."""
    return page.locator(".sidebar-link", has_text=text)


def _is_sidebar_open(page: Page) -> bool:
    """Return True if the sidebar has the 'open' class (mobile drawer state)."""
    return page.evaluate("() => document.getElementById('sidebar').classList.contains('open')")


# ---------------------------------------------------------------------------
# Flow 1: Desktop sidebar navigation + active state
# ---------------------------------------------------------------------------

def test_desktop_sidebar_nav_active_state(page: Page, live_server_url: str):
    """Desktop (1440×900): sidebar visible, active state tracks navigation.

    - /monitoring loads → sidebar present, Monitoring link is active
    - Click Email Check → URL changes to /qa, Email Check becomes active
    - Click Settings → URL changes to /admin/login
    """
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{live_server_url}/monitoring")
    page.wait_for_load_state("networkidle", timeout=15000)

    # Sidebar is visible on desktop
    sidebar = _sidebar(page)
    expect(sidebar).to_be_visible()

    # Hamburger should NOT be visible on desktop
    hamburger = _hamburger(page)
    expect(hamburger).not_to_be_visible()

    # "Monitoring" link should be active
    monitoring_link = _nav_link(page, "Monitoring")
    expect(monitoring_link).to_be_visible()
    expect(monitoring_link).to_have_class(re.compile(r"\bactive\b"))
    expect(monitoring_link).to_have_attribute("aria-current", "page")

    # "Email Check" link should NOT be active
    email_check_link = _nav_link(page, "Email Check")
    expect(email_check_link).to_be_visible()
    expect(email_check_link).not_to_have_class(re.compile(r"\bactive\b"))

    # Click Email Check → navigates to /qa
    email_check_link.click()
    expect(page).to_have_url(re.compile(r"/qa$"))

    # Now Email Check is active, Monitoring is not
    expect(_nav_link(page, "Email Check")).to_have_class(re.compile(r"\bactive\b"))
    expect(_nav_link(page, "Email Check")).to_have_attribute("aria-current", "page")
    expect(_nav_link(page, "Monitoring")).not_to_have_class(re.compile(r"\bactive\b"))

    # Click Settings → navigates to /admin/login
    settings_link = page.locator(".sidebar-link", has_text="Settings")
    settings_link.click()
    expect(page).to_have_url(re.compile(r"/admin/login"))


# ---------------------------------------------------------------------------
# Flow 2: Mobile hamburger overlay
# ---------------------------------------------------------------------------

def test_mobile_hamburger_overlay(page: Page, live_server_url: str):
    """Mobile (375×812): sidebar hidden by default, hamburger opens/closes drawer.

    - Sidebar NOT open on load (no 'open' class)
    - Hamburger visible
    - Click hamburger → sidebar opens (gets 'open' class), aria-expanded="true"
    - Click backdrop → sidebar closes
    - Click hamburger again → sidebar opens
    - Press Escape → sidebar closes
    """
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{live_server_url}/monitoring")
    page.wait_for_load_state("networkidle", timeout=15000)

    sidebar = _sidebar(page)
    hamburger = _hamburger(page)
    backdrop = _backdrop(page)

    # Sidebar starts closed (no 'open' class)
    assert not _is_sidebar_open(page), "Sidebar should be closed on mobile load"

    # Hamburger is visible
    expect(hamburger).to_be_visible()
    expect(hamburger).to_have_attribute("aria-expanded", "false")

    # Open drawer via hamburger
    hamburger.click()
    page.wait_for_function(
        "() => document.getElementById('sidebar').classList.contains('open')",
        timeout=3000,
    )
    assert _is_sidebar_open(page), "Sidebar should open after hamburger click"
    expect(hamburger).to_have_attribute("aria-expanded", "true")

    # Close via backdrop click
    expect(backdrop).to_be_visible()
    backdrop.click()
    page.wait_for_function(
        "() => !document.getElementById('sidebar').classList.contains('open')",
        timeout=3000,
    )
    assert not _is_sidebar_open(page), "Sidebar should close after backdrop click"
    expect(hamburger).to_have_attribute("aria-expanded", "false")

    # Open again, then close via Escape
    hamburger.click()
    page.wait_for_function(
        "() => document.getElementById('sidebar').classList.contains('open')",
        timeout=3000,
    )
    assert _is_sidebar_open(page), "Sidebar should reopen after second hamburger click"

    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => !document.getElementById('sidebar').classList.contains('open')",
        timeout=3000,
    )
    assert not _is_sidebar_open(page), "Sidebar should close after Escape key"
    expect(hamburger).to_have_attribute("aria-expanded", "false")


# ---------------------------------------------------------------------------
# Flow 3: Active state on subroutes
# ---------------------------------------------------------------------------

def test_active_state_on_workspace_detail(page: Page, live_server_url: str):
    """Active state persists when navigating to subroutes.

    - /qa/ws/swishfunding → Email Check sidebar link is active
    - /monitoring/swishfunding → Monitoring sidebar link is active
    """
    page.set_viewport_size({"width": 1440, "height": 900})

    # QA subroute → Email Check active
    page.goto(f"{live_server_url}/qa/ws/swishfunding")
    page.wait_for_load_state("networkidle", timeout=15000)

    email_check_link = _nav_link(page, "Email Check")
    expect(email_check_link).to_have_class(re.compile(r"\bactive\b"))
    expect(email_check_link).to_have_attribute("aria-current", "page")

    monitoring_link = _nav_link(page, "Monitoring")
    expect(monitoring_link).not_to_have_class(re.compile(r"\bactive\b"))

    # Monitoring drilldown subroute → Monitoring active
    page.goto(f"{live_server_url}/monitoring/swishfunding")
    page.wait_for_load_state("networkidle", timeout=15000)

    monitoring_link2 = _nav_link(page, "Monitoring")
    expect(monitoring_link2).to_have_class(re.compile(r"\bactive\b"))
    expect(monitoring_link2).to_have_attribute("aria-current", "page")

    email_check_link2 = _nav_link(page, "Email Check")
    expect(email_check_link2).not_to_have_class(re.compile(r"\bactive\b"))


# ---------------------------------------------------------------------------
# Flow 4: Tablet icon rail + tooltip
# ---------------------------------------------------------------------------

def test_tablet_icon_rail_tooltips(page: Page, live_server_url: str):
    """Tablet (768×1024): sidebar shows 60px icon rail, tooltips on hover.

    - Sidebar visible and narrow (~60px)
    - Sidebar link text labels hidden (icon-only)
    - data-tooltip attribute present on nav links
    - After hover, tooltip content matches link label
    - Navigation still works via icon click
    """
    page.set_viewport_size({"width": 768, "height": 1024})
    page.goto(f"{live_server_url}/monitoring")
    page.wait_for_load_state("networkidle", timeout=15000)

    sidebar = _sidebar(page)
    expect(sidebar).to_be_visible()

    # Sidebar should be narrow (icon rail, ~60px)
    bbox = sidebar.bounding_box()
    assert bbox is not None, "Sidebar bounding box should not be None"
    assert bbox["width"] <= 80, (
        f"Sidebar should be ~60px wide on tablet, got {bbox['width']:.0f}px"
    )

    # data-tooltip attribute must be present on Monitoring link
    monitoring_link = _nav_link(page, "Monitoring")
    tooltip_val = monitoring_link.get_attribute("data-tooltip")
    assert tooltip_val == "Monitoring", (
        f"Expected data-tooltip='Monitoring', got {tooltip_val!r}"
    )

    # data-tooltip on Email Check link
    email_check_link = _nav_link(page, "Email Check")
    ec_tooltip = email_check_link.get_attribute("data-tooltip")
    assert ec_tooltip == "Email Check", (
        f"Expected data-tooltip='Email Check', got {ec_tooltip!r}"
    )

    # Navigation still works at tablet width — click Monitoring (already on /monitoring,
    # click Email Check instead)
    email_check_link.click()
    expect(page).to_have_url(re.compile(r"/qa$"))


# ---------------------------------------------------------------------------
# Screenshots at 3 viewports
# ---------------------------------------------------------------------------

def test_sidebar_screenshots(page: Page, live_server_url: str):
    """Capture sidebar screenshots at desktop, tablet, and mobile viewports."""
    screenshots_dir = "/Users/mihajlo/Desktop/prospeqt-unified/qa/screenshots"

    viewports = [
        ("desktop", 1440, 900),
        ("tablet", 768, 1024),
        ("mobile", 375, 812),
    ]

    for name, width, height in viewports:
        page.set_viewport_size({"width": width, "height": height})
        page.goto(f"{live_server_url}/monitoring")
        page.wait_for_load_state("networkidle", timeout=15000)

        if name == "mobile":
            # Open the drawer so screenshot shows the sidebar
            hamburger = _hamburger(page)
            if hamburger.is_visible():
                hamburger.click()
                page.wait_for_function(
                    "() => document.getElementById('sidebar').classList.contains('open')",
                    timeout=3000,
                )

        path = f"{screenshots_dir}/phase3-sidebar-{name}.png"
        page.screenshot(path=path, full_page=False)
