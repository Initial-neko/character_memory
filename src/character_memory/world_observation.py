from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, Field

from character_memory.search import (
    BraveSearchProvider,
    FetchedPage,
    ImageSearchResult,
    SearchApiProvider,
    SearchProvider,
    WebSearchResult,
)


logger = logging.getLogger("character_memory.world_observation")


class WorldObservation(BaseModel):
    source_type: str = Field(pattern=r"^(WEB_SEARCH|WEB_PAGE)$")
    query: str = ""
    title: str = ""
    url: str = ""
    source_domain: str = ""
    snippet: str = ""
    content: str = ""
    thumbnail_url: str = ""
    published_at: str | None = None


class WorldObservationBundle(BaseModel):
    query: str
    observations: list[WorldObservation] = Field(default_factory=list, max_length=8)


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.thumbnail_url = ""
        self._inside_title = False
        self._skip_depth = 0
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        name = tag.lower()
        attrs_map = {str(key).lower(): str(value or "") for key, value in attrs}
        if name in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if name == "title":
            self._inside_title = True
        if name == "meta":
            key = (attrs_map.get("property") or attrs_map.get("name") or "").lower()
            value = attrs_map.get("content", "").strip()
            if key in {"description", "og:description", "twitter:description"} and value and not self.description:
                self.description = value
            if key in {"og:image", "twitter:image"} and value and not self.thumbnail_url:
                self.thumbnail_url = value

    def handle_endtag(self, tag):
        name = tag.lower()
        if name in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if name == "title":
            self._inside_title = False

    def handle_data(self, data):
        text = " ".join(str(data or "").split()).strip()
        if not text:
            return
        if self._inside_title and not self.title:
            self.title = text
        if not self._skip_depth:
            self._text.append(text)

    def text(self, max_chars: int) -> str:
        joined = "\n".join(self._text)
        return joined[:max_chars]


class SafeWebFetcher:
    """Small SSRF-resistant reader for observation pages.

    External pages are untrusted data. The caller receives plain extracted text
    and metadata only; nothing here is interpreted as instructions or tool policy.
    """

    def __init__(
        self,
        *,
        timeout: float = 12.0,
        max_bytes: int = 1_000_000,
        client: httpx.Client | None = None,
    ):
        self.max_bytes = max(32_768, int(max_bytes))
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    @staticmethod
    def _validate_url(url: str) -> str:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("web fetch only allows http/https URLs")
        host = parsed.hostname
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"web host cannot be resolved: {host}") from exc
        if not infos:
            raise ValueError(f"web host cannot be resolved: {host}")
        for info in infos:
            address = info[4][0]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                raise ValueError("web fetch rejected a non-public address")
        return parsed.geturl()

    def fetch(self, url: str, *, max_chars: int = 12000) -> FetchedPage:
        current = self._validate_url(url)
        response = None
        for _ in range(4):
            response = self.client.get(
                current,
                headers={
                    "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
                    "User-Agent": "character-memory/0.5 world-observation",
                },
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = str(response.headers.get("location") or "").strip()
                if not location:
                    raise RuntimeError("web redirect is missing Location")
                current = self._validate_url(urljoin(current, location))
                continue
            break
        if response is None:
            raise RuntimeError("web fetch failed")
        if response.status_code in {301, 302, 303, 307, 308}:
            raise RuntimeError("web fetch exceeded redirect limit")
        if response.is_error:
            raise RuntimeError(f"web fetch failed with HTTP {response.status_code}")

        content_type = str(response.headers.get("content-type") or "text/plain").split(";", 1)[0].strip().lower()
        raw = bytes(response.content or b"")
        if len(raw) > self.max_bytes:
            raw = raw[: self.max_bytes]
        text = raw.decode(response.encoding or "utf-8", errors="replace")

        if content_type == "text/html":
            parser = _PageParser()
            parser.feed(text)
            title = parser.title.strip()
            content = parser.text(max_chars)
        elif content_type.startswith("text/"):
            title = ""
            content = text[:max_chars]
        else:
            raise ValueError(f"unsupported web content type: {content_type}")

        logger.info(
            "world.fetch url=%s content_type=%s chars=%d",
            current,
            content_type,
            len(content),
        )
        return FetchedPage(
            url=current,
            title=title,
            content=content,
            content_type=content_type,
            description=getattr(parser, "description", "") if content_type == "text/html" else "",
            thumbnail_url=(
                urljoin(current, getattr(parser, "thumbnail_url", ""))
                if content_type == "text/html" and getattr(parser, "thumbnail_url", "")
                else ""
            ),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


def build_search_provider(settings, *, client: httpx.Client | None = None) -> SearchProvider | None:
    provider_name = str(getattr(settings, "search_provider", "searchapi") or "searchapi").strip().lower()
    kwargs = {
        "country": getattr(settings, "search_country", "jp"),
        "language": getattr(settings, "search_language", "zh-cn"),
        "safe_search": getattr(settings, "search_safe_search", "strict"),
    }
    key = getattr(settings, "search_api_key", "")
    if provider_name in {"searchapi", "searchapi.io", "search_api"}:
        return SearchApiProvider(key, client=client, **kwargs)
    if provider_name == "brave":
        return BraveSearchProvider(key, client=client, **kwargs)
    return None


class WorldObservationService:
    def __init__(
        self,
        settings,
        *,
        provider: SearchProvider | None = None,
        fetcher: SafeWebFetcher | None = None,
    ):
        self.settings = settings
        self.provider = provider if provider is not None else build_search_provider(settings)
        self.fetcher = fetcher or SafeWebFetcher()

    def available(self) -> bool:
        return bool(self.provider is not None and str(getattr(self.settings, "search_api_key", "") or "").strip())

    def observe(self, query: str, *, limit: int = 4, fetch_first: bool = True) -> WorldObservationBundle:
        query = str(query or "").strip()
        if not query:
            raise ValueError("observation query must not be empty")
        if self.provider is None:
            raise RuntimeError("no search provider is configured")
        results = self.provider.search_web(query, limit=max(1, min(limit, 6)))
        observations: list[WorldObservation] = [
            WorldObservation(
                source_type="WEB_SEARCH",
                query=query,
                title=item.title,
                url=item.url,
                source_domain=item.source_domain,
                snippet=item.snippet,
                published_at=item.published_at,
            )
            for item in results
        ]
        if fetch_first and results:
            try:
                page = self.fetcher.fetch(results[0].url, max_chars=5000)
                observations.insert(
                    0,
                    WorldObservation(
                        source_type="WEB_PAGE",
                        query=query,
                        title=page.title or results[0].title,
                        url=page.url,
                        source_domain=urlparse(page.url).hostname or results[0].source_domain,
                        snippet=page.description or results[0].snippet,
                        content=page.content,
                        thumbnail_url=page.thumbnail_url,
                        published_at=results[0].published_at,
                    ),
                )
            except Exception as exc:
                logger.info("world.observe fetch_skipped query=%r error=%s", query[:100], exc)
        return WorldObservationBundle(query=query, observations=observations[:8])

    def search_images(self, query: str, *, limit: int = 9) -> list[ImageSearchResult]:
        if self.provider is None:
            raise RuntimeError("no search provider is configured")
        return self.provider.search_images(query, limit=max(1, min(limit, 9)))

    def close(self) -> None:
        close_provider = getattr(self.provider, "close", None)
        if callable(close_provider):
            close_provider()
        self.fetcher.close()
