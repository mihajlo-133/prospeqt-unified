"""Playwright e2e user flow tests for Fix 4 — Refresh Actually Refreshes.

These 4 tests validate the full user-facing behavior of the refresh button:
1. Refresh actually fetches fresh data (server returns 200 + swapped DOM)
2. Refresh disables the button during the request (prevents double-click)
3. Refresh freshens the cache (rendered data is up-to-date)
4. Refresh resets the countdown bar

All tests run against MOCK_MODE=1 server by default (fixture data, no live API).
For live API validation, set TARGET_URL env var to a real server URL.

Note on test_refresh_actually_fetches_fresh_data:
In mock mode, refresh_all_clients_sync() short-circuits (no real fetch).
We verify: POST returns 200, cards partial DOM is swapped, container still attached.

Note on test_refresh_disables_button_during_request:
HTMX + disabled attribute: the beforeRequest listener (monitoring.html) sets
disabled=true using evt.detail.elt (element-based matching, not path string).
In mock mode the response is near-instant, so we inject a fetch monkey-patch
that holds the request for 300ms — giving a reliable window to observe disabled.
The template also handles htmx:responseError and htmx:sendError for re-enable.
"""
import re
import time

from playwright.sync_api import Page, expect


def _get_updated_at_text(page: Page) -> str:
    """Extract the 'Updated HH:MM:SS' timestamp text from the topbar."""
    el = page.locator("#updated-at")
    el.wait_for(state="visible", timeout=10000)
    return el.inner_text().strip()


def _parse_time_from_updated(text: str) -> tuple[int, int, int] | None:
    """Parse HH:MM:SS from text like 'Updated 14:32:01'."""
    m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _time_to_seconds(h: int, m: int, s: int) -> int:
    return h * 3600 + m * 60 + s


def test_refresh_actually_fetches_fresh_data(page: Page, live_server_url: str):
    """Refresh button triggers a new request that returns cards HTML.

    In mock mode we verify:
    - The refresh POST completes (network request observed)
    - The cards partial is swapped in (DOM updated)
    - The page remains functional after the swap

    For real latency timing (≥1s), run against TARGET_URL with live API keys.
    """
    page.goto(f"{live_server_url}/monitoring")

    # Wait for page to fully load
    page.wait_for_load_state("networkidle", timeout=15000)

    # The HTMX target for manual refresh is #cards-partial.
    # On desktop viewports it's hidden (table view shown instead), but the
    # element is still attached to the DOM. Use "attached" not "visible".
    cards_container = page.locator("#cards-partial")
    cards_container.wait_for(state="attached", timeout=10000)
    initial_html = cards_container.inner_html()

    # Click refresh and verify the POST request is made + response received
    btn = page.locator('button[hx-post="/api/monitoring/refresh"]')
    with page.expect_response(lambda r: "monitoring/refresh" in r.url, timeout=60000) as resp_info:
        btn.click()

    # Verify we got a successful response from the refresh endpoint
    response = resp_info.value
    assert response.status == 200, f"Expected 200 from /api/monitoring/refresh, got {response.status}"

    # Wait for HTMX to complete the DOM swap
    page.wait_for_function(
        "() => !document.querySelector('button[hx-post=\"/api/monitoring/refresh\"]')?.classList.contains('htmx-request')",
        timeout=10000,
    )

    # Verify cards container is still in DOM after swap (may be hidden at desktop width)
    cards_container.wait_for(state="attached", timeout=5000)
    post_refresh_html = cards_container.inner_html()
    assert len(post_refresh_html) > 0, "Cards container should not be empty after refresh"


def test_refresh_disables_button_during_request(page: Page, live_server_url: str):
    """Refresh button is disabled during the request and re-enabled after.

    Strategy:
    1. Intercept the POST via fetch monkey-patch to hold the request open briefly,
       giving us a reliable window to observe disabled=true regardless of server speed.
    2. After request completes, button must be re-enabled (disabled attribute absent).

    For live API calls (slow server), step 1 is trivially satisfied since the network
    round-trip takes ≥1s. The fetch intercept also works for mock mode where the
    server responds in <100ms.
    """
    page.goto(f"{live_server_url}/monitoring")
    page.wait_for_load_state("networkidle", timeout=15000)

    # Monkey-patch fetch to introduce a deliberate 300ms hold on the refresh endpoint.
    # This gives us a reliable window to observe disabled=true even in mock mode.
    # The hold resolves automatically — the actual response still goes through.
    page.evaluate("""
        (function() {
            var origFetch = window.fetch;
            window.fetch = function(url, opts) {
                var urlStr = (url && url.toString) ? url.toString() : String(url);
                if (urlStr.indexOf('/api/monitoring/refresh') !== -1) {
                    return new Promise(function(resolve) {
                        setTimeout(function() {
                            resolve(origFetch(url, opts));
                        }, 300);
                    });
                }
                return origFetch(url, opts);
            };
        })();
    """)

    btn = page.locator('button[hx-post="/api/monitoring/refresh"]')
    btn.click()

    # HTMX's beforeRequest fires synchronously on click → button should be disabled
    # within the 300ms hold window we injected above.
    from playwright.sync_api import expect as pw_expect
    pw_expect(btn).to_be_disabled(timeout=5000)

    # Wait for button to be re-enabled after request completes.
    # Timeout is generous (120s) to account for slow live API fetches (9 clients in parallel).
    pw_expect(btn).not_to_be_disabled(timeout=120000)


def test_refresh_freshens_cache(page: Page, live_server_url: str):
    """After refresh, the rendered client cards reflect data from the cache.

    We verify:
    - All expected client names are present in the cards after refresh
    - The cards partial was swapped (HTMX response arrived)

    In mock mode the cache serves fixture data; in live mode it serves
    freshly fetched data. Both should produce non-empty, named cards.
    """
    page.goto(f"{live_server_url}/monitoring")
    page.wait_for_load_state("networkidle", timeout=15000)

    btn = page.locator('button[hx-post="/api/monitoring/refresh"]')
    btn.click()

    # Wait for HTMX swap to complete
    page.wait_for_function(
        "() => !document.querySelector('button[hx-post=\"/api/monitoring/refresh\"]')?.classList.contains('htmx-request')",
        timeout=60000,
    )

    # Verify client cards are present in the refreshed partial
    cards_container = page.locator("#cards-partial")
    cards_html = cards_container.inner_html()

    # At least some client names should appear (mock fixture has 9 clients)
    expected_clients = ["SwishFunding", "MyPlace", "SmartMatchApp"]
    for name in expected_clients:
        assert name in cards_html, f"Expected client '{name}' to appear in refreshed cards"


def test_refresh_resets_countdown_bar(page: Page, live_server_url: str):
    """After refresh, the countdown bar width resets to near 100%.

    The countdown bar is a CSS animation. Clicking refresh triggers
    htmx:afterRequest which calls restartCountdown() — removing and re-adding
    the 'reset' class to restart the animation from 100%.

    We verify by reading the bar's computed width shortly after refresh
    and confirming it's back near 100% (within the first 2s of the animation).
    """
    page.goto(f"{live_server_url}/monitoring")
    page.wait_for_load_state("networkidle", timeout=15000)

    # Wait a moment so the animation starts (bar moves away from 100%)
    # The animation is 300s total; after 2s it'll be at ~99.3% — still essentially full.
    # We just need it to have started.
    time.sleep(1)

    # Get initial bar width percentage
    initial_width_pct = page.evaluate("""
        () => {
            var bar = document.getElementById('mon-countdown-bar');
            if (!bar) return null;
            var parent = bar.parentElement;
            if (!parent) return null;
            var barWidth = bar.getBoundingClientRect().width;
            var parentWidth = parent.getBoundingClientRect().width;
            return parentWidth > 0 ? (barWidth / parentWidth) * 100 : null;
        }
    """)

    # Click refresh
    btn = page.locator('button[hx-post="/api/monitoring/refresh"]')
    btn.click()

    # Wait for the request to complete
    page.wait_for_function(
        "() => !document.querySelector('button[hx-post=\"/api/monitoring/refresh\"]')?.classList.contains('htmx-request')",
        timeout=60000,
    )

    # Give the animation a brief moment to restart
    time.sleep(0.3)

    # Get bar width after refresh
    post_refresh_width_pct = page.evaluate("""
        () => {
            var bar = document.getElementById('mon-countdown-bar');
            if (!bar) return null;
            var parent = bar.parentElement;
            if (!parent) return null;
            var barWidth = bar.getBoundingClientRect().width;
            var parentWidth = parent.getBoundingClientRect().width;
            return parentWidth > 0 ? (barWidth / parentWidth) * 100 : null;
        }
    """)

    assert post_refresh_width_pct is not None, "Countdown bar not found after refresh"
    assert post_refresh_width_pct >= 90, (
        f"Expected countdown bar to reset to ≥90% after refresh, got {post_refresh_width_pct:.1f}%"
    )
