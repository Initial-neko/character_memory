from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
import time
from typing import Callable
from urllib.parse import urljoin

import httpx

from character_memory.remote_media import ensure_public_http_url
from character_memory.time_utils import epoch_us


SUCCESS_STATUSES = {"success", "succeeded", "completed", "done"}
FAILED_STATUSES = {"failed", "fail", "cancelled", "canceled", "expired", "error"}


@dataclass(frozen=True)
class VideoGenerationRequest:
    prompt: str
    resolution: str = "2K"
    duration_seconds: int = 5
    ratio: str = "16:9"


@dataclass(frozen=True)
class VideoGenerationResult:
    provider: str
    model: str
    task_id: str
    payload: bytes
    mime_type: str
    video_url: str
    resolution: str
    duration_seconds: int
    ratio: str


class MetasoMiniMaxVideoProvider:
    """Synchronous adapter around MetaSo's asynchronous MiniMax-H3 v2 API."""

    provider_id = "metaso-minimax-h3"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://metaso.cn/api/minimax",
        model: str = "MiniMax-H3",
        timeout_seconds: float = 900.0,
        poll_interval_seconds: float = 8.0,
        max_bytes: int = 64 * 1024 * 1024,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        url_validator: Callable[[str], None] = ensure_public_http_url,
    ):
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "").rstrip("/")
        self.model = str(model or "MiniMax-H3").strip() or "MiniMax-H3"
        self.timeout_seconds = max(30.0, float(timeout_seconds))
        self.poll_interval_seconds = max(1.0, float(poll_interval_seconds))
        self.max_bytes = max(1024 * 1024, int(max_bytes))
        self._client = client or httpx.Client(timeout=min(self.timeout_seconds, 600.0))
        self._owns_client = client is None
        self._sleep = sleep
        self._url_validator = url_validator

    def available(self) -> bool:
        return bool(self.api_key and self.base_url)

    def _safe_error(self, value: object) -> str:
        text = str(value or "")
        # An upstream may echo back only the token it was handed, without the
        # header name, so the Bearer shape is matched on its own rather than
        # only after the word "authorization".
        text = re.sub(r"(?i)\bbearer\s+[^\s\"']+", "Bearer [REDACTED]", text)
        text = re.sub(r"\bmk-[A-Za-z0-9_-]{12,}\b", "[REDACTED_API_KEY]", text)
        # Provider keys that do not carry the mk- prefix are still secrets.
        if self.api_key:
            text = text.replace(self.api_key, "[REDACTED_API_KEY]")
        return text[:1200]

    def _headers(self) -> dict[str, str]:
        if not self.available():
            raise RuntimeError("MetaSo MiniMax H3 API key is not configured")
        return {"Authorization": f"Bearer {self.api_key}"}

    def _json(self, method: str, path: str, *, payload: dict | None = None) -> dict:
        response = self._client.request(
            method,
            f"{self.base_url}{path}",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"MiniMax H3 {method} {path} failed with HTTP {response.status_code}: "
                f"{self._safe_error(response.text)}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"MiniMax H3 {method} {path} returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise RuntimeError(f"MiniMax H3 {method} {path} returned non-object JSON")
        base_resp = body.get("base_resp")
        if isinstance(base_resp, dict) and int(base_resp.get("status_code") or 0) != 0:
            raise RuntimeError(
                "MiniMax H3 rejected request: "
                + self._safe_error(base_resp.get("status_msg") or base_resp)
            )
        return body

    @staticmethod
    def _validate(request: VideoGenerationRequest) -> None:
        if not str(request.prompt or "").strip():
            raise ValueError("video prompt must not be empty")
        if request.resolution not in {"768P", "2K"}:
            raise ValueError("video resolution must be 768P or 2K")
        if not 5 <= int(request.duration_seconds) <= 15:
            raise ValueError("video duration must be between 5 and 15 seconds")
        if not str(request.ratio or "").strip():
            raise ValueError("video ratio must not be empty")

    def _download(self, url: str) -> tuple[bytes, str]:
        current = str(url or "").strip()
        for _ in range(5):
            try:
                self._url_validator(current)
            except ValueError as exc:
                raise RuntimeError(f"MiniMax H3 returned an unsafe video URL: {exc}") from exc

            # The generation API credential must never be forwarded to a returned
            # CDN/signed URL. Validate every redirect as a separate trust boundary.
            with self._client.stream(
                "GET",
                current,
                headers={
                    "Accept": "video/*",
                    "User-Agent": "character-memory/0.13 video-generation",
                },
                follow_redirects=False,
            ) as response:
                if 300 <= response.status_code < 400:
                    location = str(response.headers.get("location") or "").strip()
                    if not location:
                        raise RuntimeError("MiniMax H3 video download redirected without Location")
                    current = urljoin(current, location)
                    continue
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"MiniMax H3 video download failed with HTTP {response.status_code}: "
                        f"{self._safe_error(response.read().decode('utf-8', 'replace'))}"
                    )

                raw_length = response.headers.get("content-length")
                if raw_length:
                    try:
                        if int(raw_length) > self.max_bytes:
                            raise RuntimeError("generated video exceeds configured byte limit")
                    except ValueError:
                        pass

                # The ceiling is applied while the body arrives, so a chunked
                # response that never declares Content-Length cannot be buffered
                # whole before the limit is consulted.
                buffer = bytearray()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if len(buffer) + len(chunk) > self.max_bytes:
                        raise RuntimeError("generated video exceeds configured byte limit")
                    buffer.extend(chunk)
                payload = bytes(buffer)
                if not payload:
                    raise RuntimeError("MiniMax H3 returned an empty video")
                mime_type = str(
                    response.headers.get("content-type") or "video/mp4"
                ).split(";", 1)[0].strip().lower()
                if not mime_type.startswith("video/"):
                    mime_type = "video/mp4"
                return payload, mime_type
        raise RuntimeError("MiniMax H3 video download exceeded redirect limit")

    def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        self._validate(request)
        created = self._json(
            "POST",
            "/v2/video_generation",
            payload={
                "model": self.model,
                "content": [{"type": "text", "text": request.prompt}],
                "resolution": request.resolution,
                "duration": int(request.duration_seconds),
                "ratio": request.ratio,
            },
        )
        task_id = str(created.get("task_id") or "").strip()
        if not task_id:
            raise RuntimeError("MiniMax H3 create task returned no task_id")

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            body = self._json("GET", f"/v2/query/video_generation/{task_id}")
            task = body.get("task") if isinstance(body.get("task"), dict) else body
            status = str(task.get("status") or "").strip().lower()
            content = task.get("content") if isinstance(task.get("content"), dict) else {}
            video_url = str(content.get("url") or "").strip()
            if video_url:
                payload, mime_type = self._download(video_url)
                return VideoGenerationResult(
                    provider=self.provider_id,
                    model=self.model,
                    task_id=task_id,
                    payload=payload,
                    mime_type=mime_type,
                    video_url=video_url,
                    resolution=request.resolution,
                    duration_seconds=int(request.duration_seconds),
                    ratio=request.ratio,
                )
            if status in FAILED_STATUSES:
                error = task.get("error")
                raise RuntimeError(
                    f"MiniMax H3 task {task_id} failed: {self._safe_error(error or status)}"
                )
            if status in SUCCESS_STATUSES:
                raise RuntimeError(f"MiniMax H3 task {task_id} completed without a video URL")
            self._sleep(self.poll_interval_seconds)
        raise RuntimeError(f"MiniMax H3 task {task_id} timed out")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class VideoGenerationBudget:
    """Durable conservative spend ledger.

    Every reserved attempt counts against the day even when generation later
    fails, because an accepted provider task may still be billable. This favors
    cost safety over optimistic accounting.
    """

    def __init__(self, store):
        self.store = store
        self._init_schema()

    def _init_schema(self) -> None:
        with self.store._lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS video_generation_usage(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    estimated_cost_cny REAL NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT,
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_video_generation_usage_day
                    ON video_generation_usage(created_at_epoch);
                """
            )
            self.store._maybe_commit()

    def reserve(
        self,
        *,
        character_id: str,
        provider: str,
        model: str,
        resolution: str,
        duration_seconds: int,
        estimated_cost_cny: float,
        daily_budget_cny: float,
        now: datetime,
    ) -> int:
        if daily_budget_cny <= 0:
            raise RuntimeError("Space video daily budget is zero")
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        with self.store._lock:
            row = self.store.conn.execute(
                """
                SELECT COALESCE(SUM(estimated_cost_cny),0)
                FROM video_generation_usage
                WHERE created_at_epoch>=? AND created_at_epoch<?
                """,
                (epoch_us(start), epoch_us(end)),
            ).fetchone()
            spent = float(row[0] or 0.0)
            if spent + estimated_cost_cny > daily_budget_cny + 1e-9:
                raise RuntimeError(
                    f"Space video daily budget exceeded: spent ¥{spent:.2f}, "
                    f"next ¥{estimated_cost_cny:.2f}, limit ¥{daily_budget_cny:.2f}"
                )
            cursor = self.store.conn.execute(
                """
                INSERT INTO video_generation_usage(
                    character_id,provider,model,resolution,duration_seconds,
                    estimated_cost_cny,status,task_id,created_at,created_at_epoch
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    character_id,
                    provider,
                    model,
                    resolution,
                    int(duration_seconds),
                    float(estimated_cost_cny),
                    "RESERVED",
                    None,
                    now.isoformat(),
                    epoch_us(now),
                ),
            )
            self.store._maybe_commit()
            return int(cursor.lastrowid)

    def finish(self, usage_id: int, *, status: str, task_id: str | None = None) -> None:
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE video_generation_usage SET status=?, task_id=? WHERE id=?",
                (str(status or "UNKNOWN")[:32], str(task_id or "")[:160] or None, int(usage_id)),
            )
            self.store._maybe_commit()
