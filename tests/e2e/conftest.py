"""e2e test fixtures for Playwright-based user flow tests.

Design notes:
- Uses uvicorn subprocess server on an ephemeral port so lifespan hooks
  (APScheduler, httpx client) run in a real asyncio event loop, matching
  production. The alternative (ASGI test server + Playwright) requires
  running Playwright sync API in a thread while FastAPI runs async —
  fragile across OS/platform.
- MOCK_MODE=1 is set so tests serve fixture data (no live API keys needed).
- The server URL is exposed via the `live_server_url` fixture.
- Tests can opt into TARGET_URL (live Render preview) via env var for
  post-deploy verification: TARGET_URL=https://unified-fixes.onrender.com
  pytest tests/e2e/ -v
"""
import os
import socket
import subprocess
import sys
import time

import pytest


def _find_free_port() -> int:
    """Return an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 30.0) -> None:
    """Poll /health until the server responds or timeout expires."""
    import urllib.request
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:
            last_exc = exc
            time.sleep(0.25)
    raise RuntimeError(f"Server at {url} did not become ready within {timeout}s. Last error: {last_exc}")


@pytest.fixture(scope="session")
def live_server_url():
    """Provide a URL to a running server for e2e tests.

    If TARGET_URL env var is set (e.g. Render preview URL), uses that directly.
    Otherwise spins up a local uvicorn subprocess with MOCK_MODE=1.
    """
    target = os.environ.get("TARGET_URL", "").strip()
    if target:
        # Pre-deployed environment (Render preview) — just return the URL
        yield target
        return

    # Local subprocess server
    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["MOCK_MODE"] = "1"
    env["ADMIN_PASSWORD"] = "testpass"
    env["SECRET_KEY"] = "test-secret-e2e"
    # Prevent the 60s background poll from interfering with test timings
    env["MONITORING_POLL_INTERVAL_SECONDS"] = "3600"
    env["QA_POLL_INTERVAL_SECONDS"] = "3600"

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "app.main:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        _wait_for_server(url, timeout=30.0)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def browser_context_args():
    """Configure browser context — increase default timeout for slow local servers."""
    return {"base_url": ""}  # base_url overridden per-test via live_server_url
