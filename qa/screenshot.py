"""
QA Screenshot Capture for Email QA Dashboard

Takes screenshots of the dashboard at multiple viewports using Playwright CLI.
Polls for server readiness before capturing.

Usage:
  python qa/screenshot.py --port 8000 --prefix qa --wait

Pages captured:
  /              — Dashboard overview (workspace grid)
  /admin/login   — Login page

Phase 3b usage:
  MOCK_MODE=1 python qa/screenshot.py --prefix phase3b --wait
"""

import argparse
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

VIEWPORTS = [
    ("desktop", "1440,900"),
    ("tablet", "768,1024"),
    ("mobile", "375,812"),
]

PAGES = [
    ("monitoring", "/monitoring"),
    ("qa", "/qa"),
    ("dashboard", "/"),
    ("login", "/admin/login"),
]

# Phase 3c pages — full sweep of all 7 page types at 3 viewports
PHASE3C_PAGES = [
    # Monitoring overview
    ("monitoring_overview_desktop", "/monitoring", "1440,900"),
    ("monitoring_overview_tablet", "/monitoring", "768,1024"),
    ("monitoring_overview_mobile", "/monitoring", "375,812"),
    # Monitoring drill-down — clean workspace
    ("monitoring_drilldown_swishfunding_desktop", "/monitoring/swishfunding", "1440,900"),
    ("monitoring_drilldown_swishfunding_tablet", "/monitoring/swishfunding", "768,1024"),
    ("monitoring_drilldown_swishfunding_mobile", "/monitoring/swishfunding", "375,812"),
    # Monitoring drill-down — alerts workspace (Kayse)
    ("monitoring_drilldown_kayse_desktop", "/monitoring/kayse", "1440,900"),
    ("monitoring_drilldown_kayse_tablet", "/monitoring/kayse", "768,1024"),
    ("monitoring_drilldown_kayse_mobile", "/monitoring/kayse", "375,812"),
    # QA overview
    ("qa_overview_desktop", "/qa", "1440,900"),
    ("qa_overview_tablet", "/qa", "768,1024"),
    ("qa_overview_mobile", "/qa", "375,812"),
    # QA workspace detail — SwishFunding
    ("qa_workspace_swishfunding_desktop", "/qa/ws/SwishFunding", "1440,900"),
    ("qa_workspace_swishfunding_tablet", "/qa/ws/SwishFunding", "768,1024"),
    ("qa_workspace_swishfunding_mobile", "/qa/ws/SwishFunding", "375,812"),
    # Admin login
    ("admin_login_desktop", "/admin/login", "1440,900"),
    ("admin_login_mobile", "/admin/login", "375,812"),
    # Root redirect
    ("root_desktop", "/", "1440,900"),
]

# Phase 3b monitoring-specific pages (captured in addition to PAGES when prefix=phase3b)
PHASE3B_PAGES = [
    # Basic monitoring at 3 named viewports (desktop/tablet/mobile)
    ("monitoring_desktop_1440", "/monitoring", "1440,900"),
    ("monitoring_tablet_768", "/monitoring", "768,1024"),
    ("monitoring_mobile_375", "/monitoring", "375,812"),
    # Drill-down pages via direct URL (no click needed — HTMX endpoint renders same HTML)
    # Kayse has bounce_critical (red) — use for the "red alerts" shot
    ("drilldown_alerts_red", "/monitoring/kayse", "1440,900"),
    # SmartMatchApp has active+zero_sent (red) — additional alert variety
    ("drilldown_desktop_expanded", "/monitoring/swishfunding", "1440,900"),
    # Mobile drill-down
    ("drilldown_mobile_panel", "/monitoring/kayse", "375,812"),
]

SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"


def wait_for_server(port: int, timeout: float = 15.0) -> bool:
    """Poll GET /health until the server responds or timeout."""
    url = f"http://localhost:{port}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _capture_single(url: str, out_path: Path, vp_size: str, wait_ms: int = 2000) -> bool:
    """Run npx playwright screenshot for a single URL. Returns True on success."""
    result = subprocess.run(
        [
            "npx", "playwright", "screenshot",
            "--viewport-size", vp_size,
            "--full-page",
            "--wait-for-timeout", str(wait_ms),
            url,
            str(out_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode == 0 and out_path.exists()


def capture_screenshots(port: int, prefix: str) -> list[str]:
    """Capture screenshots at all viewports for all pages using Playwright CLI."""
    # Use a subdirectory named after the prefix so phase-specific screenshots stay organised
    out_dir = SCREENSHOTS_DIR / prefix if prefix != "qa" else SCREENSHOTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved = []

    for vp_name, vp_size in VIEWPORTS:
        for page_name, path in PAGES:
            out_path = out_dir / f"{prefix}_{page_name}_{vp_name}_{ts}.png"
            url = f"http://localhost:{port}{path}"

            result = subprocess.run(
                [
                    "npx", "playwright", "screenshot",
                    "--viewport-size", vp_size,
                    "--full-page",
                    "--wait-for-timeout", "2000",
                    url,
                    str(out_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0 and out_path.exists():
                saved.append(str(out_path))
                print(f"  Saved: {out_path.name}")
            else:
                print(f"  WARN: Failed {page_name} at {vp_name}: {result.stderr[:200]}")

    return saved


def capture_phase3b_screenshots(port: int, prefix: str) -> list[str]:
    """Capture Phase 3b monitoring-specific named screenshots."""
    out_dir = SCREENSHOTS_DIR / prefix
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []

    for shot_name, path, vp_size in PHASE3B_PAGES:
        out_path = out_dir / f"{shot_name}.png"
        url = f"http://localhost:{port}{path}"

        # Drill-down pages need extra time for HTMX swap rendering
        wait_ms = 3000 if "drilldown" in shot_name else 2000

        if _capture_single(url, out_path, vp_size, wait_ms=wait_ms):
            saved.append(str(out_path))
            print(f"  Saved: {out_path.name}")
        else:
            result = subprocess.run(
                ["npx", "playwright", "screenshot",
                 "--viewport-size", vp_size,
                 "--full-page",
                 "--wait-for-timeout", str(wait_ms),
                 url, str(out_path)],
                capture_output=True, text=True, timeout=30,
            )
            print(f"  WARN: Failed {shot_name}: {result.stderr[:200]}")

    return saved


def capture_phase3c_screenshots(port: int, prefix: str) -> list[str]:
    """Capture Phase 3c full sweep — all 7 page types at 3 viewports."""
    out_dir = SCREENSHOTS_DIR / prefix
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []

    for shot_name, path, vp_size in PHASE3C_PAGES:
        out_path = out_dir / f"{shot_name}.png"
        url = f"http://localhost:{port}{path}"

        wait_ms = 3000 if "drilldown" in shot_name or "workspace" in shot_name else 2000

        if _capture_single(url, out_path, vp_size, wait_ms=wait_ms):
            saved.append(str(out_path))
            print(f"  Saved: {out_path.name}")
        else:
            print(f"  WARN: Failed {shot_name} ({vp_size}) at {path}")

    return saved


def main():
    parser = argparse.ArgumentParser(description="QA Screenshot Capture")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--prefix", default="qa", help="Filename prefix (e.g., qa, before, after, phase3b, phase3c)")
    parser.add_argument("--wait", action="store_true", help="Wait for server to be ready")
    parser.add_argument("--phase3b-only", action="store_true", help="Capture only Phase 3b monitoring shots")
    args = parser.parse_args()

    if args.wait:
        print(f"Waiting for server on port {args.port}...")
        if not wait_for_server(args.port):
            print("ERROR: Server did not become ready within 15s")
            sys.exit(1)
        print("Server is ready.")

    saved = []

    if args.phase3b_only or args.prefix == "phase3b":
        print(f"Capturing Phase 3b monitoring screenshots (prefix={args.prefix})...")
        saved.extend(capture_phase3b_screenshots(args.port, args.prefix))
    elif args.prefix.startswith("phase3c"):
        print(f"Capturing Phase 3c full sweep (prefix={args.prefix})...")
        saved.extend(capture_phase3c_screenshots(args.port, args.prefix))
    else:
        print(f"Capturing screenshots (prefix={args.prefix})...")
        saved.extend(capture_screenshots(args.port, args.prefix))

    if saved:
        out_dir = SCREENSHOTS_DIR / args.prefix if args.prefix != "qa" else SCREENSHOTS_DIR
        print(f"\n{len(saved)} screenshots saved to {out_dir}/")
        for path in saved:
            print(f"  {Path(path).name}")
    else:
        print("\nNo screenshots captured. Check Playwright installation.")
        sys.exit(1)


if __name__ == "__main__":
    main()
