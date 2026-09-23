from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading

import pytest

from character_memory.browser_web import HeadlessBrowserWebFetcher


pytestmark = pytest.mark.browser


class _RenderedHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = b"""<!doctype html>
<html>
<head><title>World Browser Fixture</title></head>
<body>
  <nav>navigation noise</nav>
  <main id="content">loading</main>
  <script>
    setTimeout(() => {
      document.getElementById('content').textContent =
        'Rendered after JavaScript: world browser works';
    }, 80);
  </script>
</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


@pytest.fixture
def rendered_page_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RenderedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_guard_rejected_candidates_are_skipped_not_fatal():
    """One unresolvable host must not abort the whole candidate batch.

    The public-target guard runs before sync_playwright() is imported, so this
    path is testable without any browser.
    """
    fetcher = HeadlessBrowserWebFetcher()
    missing = "http://character-memory-no-such-host.invalid/one"
    also_missing = "http://character-memory-no-such-host-too.invalid/two"

    pages, errors = fetcher.fetch_many([missing, also_missing])

    assert pages == []
    assert [item["url"] for item in errors] == [missing, also_missing]
    assert all("could not be resolved" in item["error"] for item in errors)


def test_single_fetch_still_rejects_an_unresolvable_target():
    """The single-URL dev route keeps its ValueError contract for bad targets."""
    fetcher = HeadlessBrowserWebFetcher()

    with pytest.raises(ValueError):
        fetcher.fetch("http://character-memory-no-such-host.invalid/one")


def test_headless_world_browser_reads_javascript_rendered_text(rendered_page_url):
    if os.getenv("RUN_PLAYWRIGHT") != "1":
        pytest.skip("real browser smoke runs only in the dedicated Playwright job")

    fetcher = HeadlessBrowserWebFetcher(
        channel="chromium",
        render_wait_ms=250,
        timeout_seconds=10,
        allow_private_network=True,
    )
    # pytest-playwright owns an asyncio loop in the test thread, while the
    # formal Space scheduler/FastAPI sync routes call the World Browser from a
    # normal worker thread. Exercise the same production boundary here.
    with ThreadPoolExecutor(max_workers=1) as pool:
        page = pool.submit(fetcher.fetch, rendered_page_url, max_chars=4000).result(timeout=15)

    assert page.title == "World Browser Fixture"
    assert "Rendered after JavaScript: world browser works" in page.content
    assert "loading" not in page.content
    assert "navigation noise" not in page.content
