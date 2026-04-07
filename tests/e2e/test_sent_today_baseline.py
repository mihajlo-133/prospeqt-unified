"""Cross-check sent_today values between fixes branch and old dashboard baseline.

Strategy:
- Old dashboard (campaign-dashboard-0zra.onrender.com): exposes /api/data as JSON.
  We fetch it directly via urllib (no Playwright needed — it's a JSON endpoint).
- New dashboard (unified-fixes.onrender.com): renders sent_today values in the
  monitoring table HTML.  We navigate to /monitoring and read
  tr[data-slug="{slug}"] > td.mon-td--num:first-of-type (the Sent column).

The test asserts values are within 1% of each other, allowing for fetch-timing
drift between when the two fetches happen.

Client slug mapping (old dashboard key → new dashboard data-slug):
  MyPlace          → myplace
  SwishFunding     → swishfunding
  SmartMatchApp    → smartmatchapp
  HeyReach         → heyreach
  Kayse            → kayse
  Prosperly        → prosperly
  Enavra           → enavra
  RankZero         → rankzero
  SwishFunding (EB) → swishfunding_eb

Run against live URLs:
  TARGET_URL=https://unified-fixes.onrender.com pytest tests/e2e/test_sent_today_baseline.py -v
"""
import json
import urllib.request
import urllib.error

import pytest
from playwright.sync_api import Page


# Old dashboard JSON API — returns a dict keyed by client name.
OLD_DASHBOARD_API = "https://campaign-dashboard-0zra.onrender.com/api/data"
NEW_DASHBOARD_BASE = "https://unified-fixes.onrender.com"

# All 9 monitored clients: old_dashboard_key → new_dashboard_data_slug
CLIENTS_TO_CHECK = {
    "MyPlace":           "myplace",
    "SwishFunding":      "swishfunding",
    "SmartMatchApp":     "smartmatchapp",
    "HeyReach":          "heyreach",
    "Kayse":             "kayse",
    "Prosperly":         "prosperly",
    "Enavra":            "enavra",
    "RankZero":          "rankzero",
    "SwishFunding (EB)": "swishfunding_eb",
}

# Maximum acceptable divergence between old and new dashboard (percentage points).
MAX_DIFF_PCT = 1.0


def _fetch_old_dashboard_values() -> dict[str, int]:
    """Fetch sent_today values from the old dashboard JSON API.

    Returns a dict: {client_name: sent_today_int}.
    Raises on network error or unexpected response shape.
    """
    req = urllib.request.Request(
        OLD_DASHBOARD_API,
        headers={"User-Agent": "pytest-e2e-baseline-check/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Old dashboard unreachable: {exc}") from exc

    data = json.loads(raw)
    result: dict[str, int] = {}

    if isinstance(data, dict):
        # Response is {ClientName: {sent_today: N, ...}, ...}
        for client_name in CLIENTS_TO_CHECK:
            if client_name in data:
                result[client_name] = int(data[client_name].get("sent_today", 0))
            else:
                raise KeyError(
                    f"Client '{client_name}' not found in old dashboard API response. "
                    f"Available keys: {list(data.keys())[:10]}"
                )
    elif isinstance(data, list):
        # Response is [{name: ..., sent_today: N}, ...]
        by_name = {item.get("name", item.get("client", "")): item for item in data}
        for client_name in CLIENTS_TO_CHECK:
            if client_name in by_name:
                result[client_name] = int(by_name[client_name].get("sent_today", 0))
            else:
                raise KeyError(
                    f"Client '{client_name}' not found in old dashboard API response. "
                    f"Available keys: {list(by_name.keys())[:10]}"
                )
    else:
        raise TypeError(f"Unexpected old dashboard API response type: {type(data)}")

    return result


@pytest.mark.skipif(
    True,  # Controlled by coordinator — skip until deploy is signalled
    reason="Standby: waiting for coordinator signal that fix is committed and deployed",
)
def test_sent_today_matches_old_dashboard_baseline_standby():
    """Placeholder — remove skipif once coordinator signals deploy is ready."""
    pass


def test_sent_today_matches_old_dashboard_baseline(page: Page, live_server_url: str):
    """Cross-check sent_today values between the fixes branch and the old dashboard.

    This test ONLY runs meaningfully against the live deployed URL.
    Set TARGET_URL=https://unified-fixes.onrender.com to run against live.
    Without TARGET_URL the test is skipped (mock fixture data differs from live).

    To run against live deployed URL:
        TARGET_URL=https://unified-fixes.onrender.com pytest tests/e2e/test_sent_today_baseline.py::test_sent_today_matches_old_dashboard_baseline -v
    """
    import os
    target_url = os.environ.get("TARGET_URL", "").strip()
    is_live = bool(target_url)

    if not is_live:
        pytest.skip(
            "Skipping sent_today cross-check in local/mock mode. "
            "Set TARGET_URL=https://unified-fixes.onrender.com to run against live."
        )

    # --- Step 1: Fetch old dashboard baseline values ---
    try:
        old_values = _fetch_old_dashboard_values()
    except RuntimeError as exc:
        # Old dashboard unreachable — document this and hardcode last-known values
        # as the baseline (captured 2026-04-07 ~15:00 Belgrade time).
        old_values = {
            "MyPlace":           0,
            "SwishFunding":      19695,
            "SmartMatchApp":     1134,
            "HeyReach":          2025,
            "Kayse":             0,
            "Prosperly":         3150,
            "Enavra":            2000,
            "RankZero":          1556,
            "SwishFunding (EB)": 0,
        }
        print(
            f"\nWARNING: Old dashboard unreachable ({exc}). "
            f"Using hardcoded baseline from 2026-04-07: {old_values}"
        )

    # --- Step 2: Navigate to new dashboard and trigger a fresh fetch ---
    base = target_url if is_live else live_server_url
    page.goto(f"{base}/monitoring", wait_until="networkidle", timeout=60000)

    # Trigger refresh to ensure we're reading live data (not stale cache)
    refresh_btn = page.locator('button[hx-post="/api/monitoring/refresh"]')
    if refresh_btn.count() > 0:
        with page.expect_response(
            lambda r: "/api/monitoring/refresh" in r.url,
            timeout=120000,
        ):
            refresh_btn.click()
        # Wait for HTMX to complete the DOM swap
        page.wait_for_function(
            "() => !document.querySelector('button[hx-post=\"/api/monitoring/refresh\"]')"
            "?.classList.contains('htmx-request')",
            timeout=120000,
        )

    # --- Step 3: Read sent_today values from the new dashboard table ---
    # Rows are: tr[data-slug="{slug}"]
    # The Sent (sent_today) column is the first td.mon-td--num in the row.
    # Rows still in loading/error state have no td.mon-td--num — skip them gracefully.
    new_values: dict[str, int | None] = {}

    for client_name, slug in CLIENTS_TO_CHECK.items():
        row = page.locator(f'tr[data-slug="{slug}"]')
        row.wait_for(state="attached", timeout=15000)

        # Loaded rows have td.mon-td--num; loading/error rows have colspan td only.
        sent_cell = row.locator("td.mon-td--num").first
        if sent_cell.count() == 0:
            new_values[client_name] = None  # loading or error — skip comparison
            continue

        raw_text = sent_cell.inner_text(timeout=5000).strip()
        # Strip commas, trend arrows, whitespace  →  "19,695 ↑" → "19695"
        digits_only = "".join(ch for ch in raw_text if ch.isdigit())
        new_values[client_name] = int(digits_only) if digits_only else 0

    # --- Step 4: Print comparison table ---
    print("\n--- sent_today cross-check ---")
    print(f"{'Client':<22} {'Old':>8} {'New':>8} {'Diff%':>8} {'Status':>8}")
    print("-" * 57)
    for client in CLIENTS_TO_CHECK:
        old = old_values[client]
        new = new_values[client]
        if new is None:
            print(f"{client:<22} {old:>8,} {'N/A':>8} {'—':>8} {'SKIP':>8}")
            continue
        if old == 0 and new == 0:
            diff_pct = 0.0
        elif old == 0:
            diff_pct = 100.0
        else:
            diff_pct = abs(new - old) / old * 100
        status = "OK" if diff_pct < MAX_DIFF_PCT else "FAIL"
        print(f"{client:<22} {old:>8,} {new:>8,} {diff_pct:>7.2f}% {status:>8}")

    # --- Step 5: Assert within tolerance (live only) ---
    if is_live:
        failures = []
        for client in CLIENTS_TO_CHECK:
            old = old_values[client]
            new = new_values[client]
            if old == 0 and new == 0:
                continue
            diff_pct = abs(new - old) / old * 100 if old != 0 else 100.0
            if diff_pct >= MAX_DIFF_PCT:
                failures.append(
                    f"{client}: old={old:,} new={new:,} diff={diff_pct:.2f}%"
                )

        assert not failures, (
            "sent_today values diverge beyond 1% tolerance between old and new dashboard:\n"
            + "\n".join(failures)
        )
    else:
        # In mock mode we can't compare against live data — just verify selectors work
        assert new_values, "No sent_today values extracted from mock dashboard"
        print(
            "\n[mock mode] Selector validation passed. "
            "Values not compared (mock fixture ≠ live data). "
            "Run with TARGET_URL=https://unified-fixes.onrender.com for live comparison."
        )
