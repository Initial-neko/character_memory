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
