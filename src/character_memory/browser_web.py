from __future__ import annotations

import logging
import re
import threading
from urllib.parse import urlparse

from character_memory.remote_media import ensure_public_http_url
from character_memory.search import FetchedPage, WebFetcher


logger = logging.getLogger("character_memory.browser_web")


def _normalize_text(value: str, max_chars: int) -> str:
    lines: list[str] = []
    previous = ""
    for raw in str(value or "").replace("\r", "\n").split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw).strip()
        if not line or line == previous:
            continue
        lines.append(line)
        previous = line
    text = "\n".join(lines).strip()
    return text[: max(1, int(max_chars))]


class HeadlessBrowserWebFetcher(WebFetcher):
    """Render public web pages in an isolated headless Chromium session.

    Each fetch batch owns its Playwright/browser lifecycle. Space opportunities
    are infrequent, so startup cost is preferable to sharing thread-affine
    Playwright objects between the scheduler and FastAPI worker threads.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        render_wait_ms: int = 700,
        channel: str = "auto",
        allow_private_network: bool = False,
    ):
        self.timeout_seconds = max(3.0, min(90.0, float(timeout_seconds)))
        self.render_wait_ms = max(0, min(5000, int(render_wait_ms)))
        value = str(channel or "auto").strip().lower()
        self.channel = value if value in {"auto", "chromium", "chrome"} else "auto"
        self.allow_private_network = bool(allow_private_network)
        self._lock = threading.Lock()

    def _guard(self, url: str) -> None:
        if self.allow_private_network:
            parsed = urlparse(str(url or ""))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("web URL must be an absolute http(s) URL")
            return
        ensure_public_http_url(url)

    def _launch(self, playwright):
        launch_kwargs = {"headless": True}
        if self.channel == "chrome":
            return playwright.chromium.launch(channel="chrome", **launch_kwargs)
        if self.channel == "chromium":
            return playwright.chromium.launch(**launch_kwargs)

        first_error: Exception | None = None
        try:
            return playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            first_error = exc
        try:
            return playwright.chromium.launch(channel="chrome", **launch_kwargs)
        except Exception as exc:
            raise RuntimeError(
                "headless browser unavailable; install Playwright Chromium with "
                "uv run playwright install chromium or install desktop Chrome. "
                f"chromium={first_error}; chrome={exc}"
            ) from exc

    def fetch_many(self, urls: list[str], *, max_chars: int = 12000) -> tuple[list[FetchedPage], list[dict[str, str]]]:
        targets: list[str] = []
        errors: list[dict[str, str]] = []
        for raw in urls:
            url = str(raw or "").strip()
            if not url or url in targets:
                continue
            try:
                self._guard(url)
            except (ValueError, RuntimeError) as exc:
                # Skipping an unusable candidate is safer than opening it, and
                # one bad host (a candidate that does not resolve here) must not
                # abort a batch whose other candidates are fine. The rejection
                # stays visible to the caller through errors.
                logger.warning("world.browser target_rejected url=%s error=%s", url, exc)
                errors.append({"url": url, "error": str(exc)[:800]})
                continue
            targets.append(url)
        if not targets:
            return [], errors

        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed; install the browser extra/all runtime first"
            ) from exc

        pages: list[FetchedPage] = []
        timeout_ms = int(self.timeout_seconds * 1000)

        with self._lock:
            with sync_playwright() as playwright:
                browser = self._launch(playwright)
                try:
                    context = browser.new_context(
                        ignore_https_errors=False,
                        java_script_enabled=True,
                        service_workers="block",
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/140 Safari/537.36 CharacterMemoryWorldBrowser/1.0"
                        ),
                    )

                    validated_hosts: set[str] = set()

                    def route_guard(route):
                        request = route.request
                        parsed = urlparse(request.url)
                        if parsed.scheme in {"data", "blob"}:
                            route.continue_()
                            return
                        if parsed.scheme not in {"http", "https"}:
                            route.abort()
                            return
                        hostname = (parsed.hostname or "").lower()
                        try:
                            if not self.allow_private_network and hostname not in validated_hosts:
                                ensure_public_http_url(request.url)
                                validated_hosts.add(hostname)
                        except (ValueError, RuntimeError):
                            route.abort()
                            return
                        if request.resource_type in {"image", "media", "font"}:
                            route.abort()
                            return
                        route.continue_()

                    context.route("**/*", route_guard)

                    for target in targets:
                        page = context.new_page()
                        try:
                            response = page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
                            if response is None:
                                raise RuntimeError("browser navigation returned no HTTP response")
                            if response.status >= 400:
                                raise RuntimeError(f"browser navigation failed with HTTP {response.status}")
                            try:
                                page.wait_for_load_state("networkidle", timeout=min(2500, timeout_ms))
                            except PlaywrightTimeoutError:
                                pass
                            if self.render_wait_ms:
                                page.wait_for_timeout(self.render_wait_ms)

                            final_url = page.url
                            self._guard(final_url)
                            extracted = page.evaluate(
                                """() => {
                                  const selectors = ['article', 'main', '[role="main"]', 'body'];
                                  let best = '';
                                  for (const selector of selectors) {
                                    for (const node of document.querySelectorAll(selector)) {
                                      const clone = node.cloneNode(true);
                                      clone.querySelectorAll('script,style,noscript,svg,canvas,nav,footer')
                                        .forEach((item) => item.remove());
                                      const text = (clone.innerText || clone.textContent || '').trim();
                                      if (text.length > best.length) best = text;
                                    }
                                    if (best.length >= 800 && selector !== 'body') break;
                                  }
                                  const description = document.querySelector(
                                    'meta[name="description"],meta[property="og:description"]'
                                  )?.content || '';
                                  return {
                                    title: document.title || '',
                                    content: best,
                                    description,
                                    lang: document.documentElement.lang || ''
                                  };
                                }"""
                            )
                            content = _normalize_text(extracted.get("content", ""), max_chars)
                            if not content:
                                raise RuntimeError("rendered page contained no readable text")
                            mime = str(response.headers.get("content-type") or "text/html").split(";", 1)[0].strip()
                            pages.append(
                                FetchedPage(
                                    url=final_url,
                                    title=_normalize_text(extracted.get("title", ""), 500),
                                    content=content,
                                    content_type=mime or "text/html",
                                    description=_normalize_text(extracted.get("description", ""), 1000),
                                )
                            )
                        except Exception as exc:
                            logger.warning("world.browser fetch_failed url=%s error=%s", target, exc)
                            errors.append({"url": target, "error": str(exc)[:800]})
                        finally:
                            page.close()
                    context.close()
                finally:
                    browser.close()
        return pages, errors

    def fetch(self, url: str, *, max_chars: int = 12000) -> FetchedPage:
        target = str(url or "").strip()
        if target:
            # Keep the single-target contract: an unusable URL is reported as a
            # ValueError (the dev route maps it to 400) before anything is
            # opened, while the batch path above fails soft per candidate.
            self._guard(target)
        pages, errors = self.fetch_many([target], max_chars=max_chars)
        if pages:
            return pages[0]
        detail = errors[-1]["error"] if errors else "browser returned no page"
        raise RuntimeError(detail)
