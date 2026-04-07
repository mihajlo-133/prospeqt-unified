"""
Phase 5 Final Playwright Screenshot Sweep

Comprehensive visual QA covering all 9 page types at 3 viewports.
Outputs to qa/screenshots/final/

Usage:
  MOCK_MODE=1 python qa/screenshot_final.py --port 8099
"""

import argparse
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

SCREENSHOTS_DIR = Path(__file__).parent / "screenshots" / "final"

# (shot_name, path, viewport_size, wait_ms)
FINAL_PAGES = [
    # 1. Monitoring overview — cards view (mobile/tablet)
    ("final_monitoring_overview_mobile", "/monitoring", "375,812", 2000),
    ("final_monitoring_overview_tablet", "/monitoring", "768,1024", 2000),
    # 2. Monitoring overview — table view (desktop)
    ("final_monitoring_overview_desktop", "/monitoring", "1440,900", 2000),
    # 3. Monitoring drill-down — clean workspace (SwishFunding)
    ("final_monitoring_drilldown_clean_desktop", "/monitoring/swishfunding", "1440,900", 3000),
    ("final_monitoring_drilldown_clean_tablet", "/monitoring/swishfunding", "768,1024", 3000),
    ("final_monitoring_drilldown_clean_mobile", "/monitoring/swishfunding", "375,812", 3000),
    # 4. Monitoring drill-down — alerts workspace (Kayse with red alerts)
    ("final_monitoring_drilldown_alerts_desktop", "/monitoring/kayse", "1440,900", 3000),
    ("final_monitoring_drilldown_alerts_tablet", "/monitoring/kayse", "768,1024", 3000),
    ("final_monitoring_drilldown_alerts_mobile", "/monitoring/kayse", "375,812", 3000),
    # 5. QA overview
    ("final_qa_overview_desktop", "/qa", "1440,900", 2000),
    ("final_qa_overview_tablet", "/qa", "768,1024", 2000),
    ("final_qa_overview_mobile", "/qa", "375,812", 2000),
    # 6. QA workspace detail
    ("final_qa_workspace_desktop", "/qa/ws/SwishFunding", "1440,900", 3000),
    ("final_qa_workspace_tablet", "/qa/ws/SwishFunding", "768,1024", 3000),
    ("final_qa_workspace_mobile", "/qa/ws/SwishFunding", "375,812", 3000),
    # 7. QA campaign detail (one example — navigate via monitoring)
    ("final_qa_campaign_desktop", "/qa/ws/SwishFunding", "1440,900", 3000),
    # 8. Admin login — unauthenticated
    ("final_admin_login_desktop", "/admin/login", "1440,900", 1500),
    ("final_admin_login_tablet", "/admin/login", "768,1024", 1500),
    ("final_admin_login_mobile", "/admin/login", "375,812", 1500),
    # 9. Root redirect
    ("final_root_desktop", "/", "1440,900", 2000),
]


def wait_for_server(port: int, timeout: float = 20.0) -> bool:
    url = f"http://localhost:{port}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def capture(url: str, out_path: Path, vp_size: str, wait_ms: int) -> bool:
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
        timeout=45,
    )
    return result.returncode == 0 and out_path.exists()


def main():
    parser = argparse.ArgumentParser(description="Phase 5 Final Screenshot Sweep")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--wait", action="store_true", help="Wait for server to be ready")
    args = parser.parse_args()

    if args.wait:
        print(f"Waiting for server on port {args.port}...")
        if not wait_for_server(args.port):
            print("ERROR: Server not ready within 20s")
            sys.exit(1)
        print("Server ready.")

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    saved = []
    failed = []

    print(f"Capturing {len(FINAL_PAGES)} screenshots -> {SCREENSHOTS_DIR}/\n")

    for shot_name, path, vp_size, wait_ms in FINAL_PAGES:
        out_path = SCREENSHOTS_DIR / f"{shot_name}.png"
        url = f"http://localhost:{args.port}{path}"

        if capture(url, out_path, vp_size, wait_ms):
            saved.append(out_path.name)
            print(f"  OK  {out_path.name}")
        else:
            failed.append(out_path.name)
            print(f"  FAIL {out_path.name}")

    print(f"\n--- Summary ---")
    print(f"  Saved: {len(saved)}")
    print(f"  Failed: {len(failed)}")
    if failed:
        for f in failed:
            print(f"    FAIL: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
