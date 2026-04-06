"""
QA Screenshot Capture for Email QA Dashboard

Takes screenshots of the dashboard at multiple viewports using Playwright CLI.
Polls for server readiness before capturing.

Usage:
  python qa/screenshot.py --port 8000 --prefix qa --wait

Pages captured:
  /              — Dashboard overview (workspace grid)
  /admin/login   — Login page
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
    ("dashboard", "/"),
    ("login", "/admin/login"),
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


def capture_screenshots(port: int, prefix: str) -> list[str]:
    """Capture screenshots at all viewports for all pages using Playwright CLI."""
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved = []

    for vp_name, vp_size in VIEWPORTS:
        for page_name, path in PAGES:
            out_path = SCREENSHOTS_DIR / f"{prefix}_{page_name}_{vp_name}_{ts}.png"
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


def main():
    parser = argparse.ArgumentParser(description="QA Screenshot Capture")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--prefix", default="qa", help="Filename prefix (e.g., qa, before, after)")
    parser.add_argument("--wait", action="store_true", help="Wait for server to be ready")
    args = parser.parse_args()

    if args.wait:
        print(f"Waiting for server on port {args.port}...")
        if not wait_for_server(args.port):
            print("ERROR: Server did not become ready within 15s")
            sys.exit(1)
        print("Server is ready.")

    print(f"Capturing screenshots (prefix={args.prefix})...")
    saved = capture_screenshots(args.port, args.prefix)

    if saved:
        print(f"\n{len(saved)} screenshots saved to {SCREENSHOTS_DIR}/")
    else:
        print("\nNo screenshots captured. Check Playwright installation.")
        sys.exit(1)


if __name__ == "__main__":
    main()
