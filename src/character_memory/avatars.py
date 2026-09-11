from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import json
import logging
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlparse
import uuid

import httpx
from pydantic import BaseModel

from character_memory.search import ImageSearchResult, SearchProvider


logger = logging.getLogger("character_memory.avatars")

_CONTENT_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


class AvatarCandidate(BaseModel):
    id: str
    title: str
    thumbnail_url: str
    source_page_url: str
    source_domain: str
    width: int | None = None
    height: int | None = None


class AvatarMetadata(BaseModel):
    character_id: str
    local_file: str
    source: str = "web_search"
    source_page_url: str
    source_image_url: str
    source_domain: str
    title: str
    search_query: str
    content_type: str
    selected_at: datetime


@dataclass(frozen=True)
class _CachedCandidate:
    public: AvatarCandidate
    image_url: str
    query: str


@dataclass
class _SearchSession:
    character_id: str
    query: str
    created_at: float
    candidates: dict[str, _CachedCandidate]


def _safe_public_http_url(url: str) -> None:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("avatar URL must be an absolute http(s) URL")
    hostname = parsed.hostname.strip().lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise ValueError("avatar URL points to a local host")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise ValueError(f"avatar host could not be resolved: {hostname}") from exc
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError("avatar URL resolved to a non-public address")


class AvatarStore:
    def __init__(
        self,
        root: str | Path,
        *,
        max_bytes: int = 8 * 1024 * 1024,
        client: httpx.Client | None = None,
    ):
        self.root = Path(root)
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.root.mkdir(parents=True, exist_ok=True)
        self._owns_client = client is None
        # Avatar downloads intentionally do not follow redirects automatically.
        # A redirect could turn a public search result into a private-network URL
        # after our SSRF validation. Search-provider thumbnails remain a useful
        # fallback when a source rejects direct server downloads.
        self.client = client or httpx.Client(timeout=30.0, follow_redirects=False)

    def _character_dir(self, character_id: str) -> Path:
        value = str(character_id or "").strip()
        if not value or any(part in value for part in ("/", "\\", "..")):
            raise ValueError("invalid character_id")
        path = self.root / value
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _metadata_path(self, character_id: str) -> Path:
        return self._character_dir(character_id) / "avatar.json"

    def load(self, character_id: str) -> AvatarMetadata | None:
        path = self._metadata_path(character_id)
        if not path.exists():
            return None
        try:
            return AvatarMetadata.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception("avatar.metadata invalid character=%s path=%s", character_id, path)
            return None

    def asset_path(self, character_id: str) -> Path | None:
        metadata = self.load(character_id)
        if metadata is None:
            return None
        path = self._character_dir(character_id) / metadata.local_file
        return path if path.is_file() else None

    def version(self, character_id: str) -> str:
        path = self.asset_path(character_id)
        if path is None:
            return ""
        return str(path.stat().st_mtime_ns)

    def _download_image(self, url: str) -> tuple[bytes, str]:
        _safe_public_http_url(url)
        response = self.client.get(
            url,
            headers={"Accept": "image/*", "User-Agent": "character-memory/0.4 avatar-fetch"},
            follow_redirects=False,
        )
        if 300 <= response.status_code < 400:
            raise RuntimeError("avatar download redirect was rejected for safety")
        if response.is_error:
            raise RuntimeError(f"avatar download failed with HTTP {response.status_code}")
        payload = response.content
        if not payload:
            raise RuntimeError("avatar download returned an empty body")
        if len(payload) > self.max_bytes:
            raise ValueError(f"avatar exceeds the {self.max_bytes} byte limit")
        content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type not in _CONTENT_EXTENSIONS:
            raise ValueError(f"unsupported avatar content type: {content_type or '<missing>'}")
        return payload, content_type

    def save_from_candidate(
        self,
        character_id: str,
        candidate: ImageSearchResult,
        *,
        query: str,
    ) -> AvatarMetadata:
        payload: bytes | None = None
        content_type = ""
        errors: list[str] = []
        # Prefer the original image, but a provider thumbnail is a useful
        # fallback when a source rejects direct server downloads.
        urls = list(dict.fromkeys([candidate.image_url, candidate.thumbnail_url]))
        for url in urls:
            if not url:
                continue
            try:
                payload, content_type = self._download_image(url)
                break
            except (RuntimeError, ValueError) as exc:
                errors.append(str(exc))
        if payload is None:
            raise RuntimeError("avatar download failed: " + " | ".join(errors or ["no downloadable URL"]))

        extension = _CONTENT_EXTENSIONS[content_type]
        directory = self._character_dir(character_id)
        filename = f"avatar{extension}"
        target = directory / filename
        temp = directory / f".{filename}.{uuid.uuid4().hex}.tmp"
        temp.write_bytes(payload)
        temp.replace(target)
        for old in directory.glob("avatar.*"):
            if old.name in {filename, "avatar.json"}:
                continue
            if old.is_file():
                old.unlink(missing_ok=True)

        metadata = AvatarMetadata(
            character_id=character_id,
            local_file=filename,
            source_page_url=candidate.source_page_url,
            source_image_url=candidate.image_url,
            source_domain=candidate.source_domain,
            title=candidate.title,
            search_query=query,
            content_type=content_type,
            selected_at=datetime.now(timezone.utc),
        )
        metadata_path = directory / "avatar.json"
        metadata_path.write_text(
            json.dumps(metadata.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(
            "avatar.saved character=%s bytes=%d source=%s",
            character_id,
            len(payload),
            candidate.source_domain or "-",
        )
        return metadata

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class AvatarSearchService:
    def __init__(
        self,
        provider: SearchProvider | None,
        store: AvatarStore,
        *,
        session_ttl_seconds: float = 15 * 60,
    ):
        self.provider = provider
        self.store = store
        self.session_ttl_seconds = max(60.0, float(session_ttl_seconds))
        self._sessions: dict[str, _SearchSession] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        expired = [key for key, value in self._sessions.items() if now - value.created_at > self.session_ttl_seconds]
        for key in expired:
            self._sessions.pop(key, None)

    @staticmethod
    def _normalize_queries(queries: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in queries:
            query = " ".join(str(raw or "").split()).strip()[:180]
            if not query:
                continue
            key = query.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(query)
            if len(result) >= 3:
                break
        return result

    def search(self, character_id: str, query: str, *, limit: int = 12) -> dict:
        return self.search_queries(character_id, [query], limit=limit)

    def search_queries(self, character_id: str, queries: list[str], *, limit: int = 12) -> dict:
        if self.provider is None:
            raise RuntimeError("image search is not configured")
        normalized = self._normalize_queries(queries)
        if not normalized:
            raise ValueError("avatar search query must not be empty")

        target = max(1, min(int(limit), 20))
        candidates: dict[str, _CachedCandidate] = {}
        seen_images: set[str] = set()
        used_queries: list[str] = []

        # Search the primary LLM query first. Extra planned queries are only used
        # when the provider returns too few viable candidates, preserving free
        # API quota in the common case.
        for query in normalized:
            remaining = target - len(candidates)
            if remaining <= 0:
                break
            raw = self.provider.search_images(query, limit=remaining)
            used_queries.append(query)
            for item in raw:
                dedup_key = str(item.image_url or item.thumbnail_url or item.source_page_url).strip().casefold()
                if not dedup_key or dedup_key in seen_images:
                    continue
                seen_images.add(dedup_key)
                candidate_id = uuid.uuid4().hex[:12]
                public = AvatarCandidate(
                    id=candidate_id,
                    title=item.title,
                    thumbnail_url=item.thumbnail_url,
                    source_page_url=item.source_page_url,
                    source_domain=item.source_domain,
                    width=item.width,
                    height=item.height,
                )
                candidates[candidate_id] = _CachedCandidate(
                    public=public,
                    image_url=item.image_url,
                    query=query,
                )
                if len(candidates) >= target:
                    break

        search_id = uuid.uuid4().hex
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            self._sessions[search_id] = _SearchSession(
                character_id=character_id,
                query=normalized[0],
                created_at=now,
                candidates=candidates,
            )
        return {
            "search_id": search_id,
            "query": normalized[0],
            "queries": normalized,
            "used_queries": used_queries,
            "candidates": [item.public.model_dump(mode="json") for item in candidates.values()],
        }

    def select(self, character_id: str, search_id: str, candidate_id: str) -> AvatarMetadata:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            session = self._sessions.get(str(search_id or ""))
            if session is None:
                raise ValueError("avatar search session expired or does not exist")
            if session.character_id != character_id:
                raise ValueError("avatar search session belongs to another character")
            cached = session.candidates.get(str(candidate_id or ""))
            if cached is None:
                raise ValueError("avatar candidate is not part of this search")
            query = cached.query or session.query

        result = ImageSearchResult(
            title=cached.public.title,
            image_url=cached.image_url,
            thumbnail_url=cached.public.thumbnail_url,
            source_page_url=cached.public.source_page_url,
            source_domain=cached.public.source_domain,
            width=cached.public.width,
            height=cached.public.height,
        )
        metadata = self.store.save_from_candidate(character_id, result, query=query)
        with self._lock:
            self._sessions.pop(search_id, None)
        return metadata

    def close(self) -> None:
        if self.provider is not None:
            self.provider.close()
        self.store.close()
