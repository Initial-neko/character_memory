from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
import re
import uuid

from pydantic import BaseModel, Field


_DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,(.+)$", re.S)
_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


class MediaAsset(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    character_id: str = Field(min_length=1, max_length=64)
    source: str = "USER_UPLOAD"
    original_name: str = Field(default="image", max_length=180)
    mime_type: str = Field(min_length=1, max_length=80)
    storage_name: str = Field(min_length=1, max_length=120)
    created_at: datetime
    size_bytes: int = Field(ge=1)


class MediaStorage:
    """Small local image store for chat and generated visual assets.

    The browser sends a base64 data URL so FastAPI does not need multipart
    dependencies. Generated provider bytes are normalized through the same gate.
    Raw image bytes never go into SQLite or Runtime Trace.
    """

    def __init__(self, root: str | Path, *, max_bytes: int = 8 * 1024 * 1024):
        self.root = Path(root)
        self.max_bytes = max_bytes

    @staticmethod
    def _sniff_mime(data: bytes) -> str | None:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
        return None

    def save_bytes(
        self,
        *,
        character_id: str,
        original_name: str,
        payload: bytes,
        created_at: datetime,
        source: str = "GENERATED",
    ) -> MediaAsset:
        data = bytes(payload or b"")
        if not data:
            raise ValueError("image is empty")
        if len(data) > self.max_bytes:
            raise ValueError(f"image exceeds {self.max_bytes // (1024 * 1024)} MiB limit")
        mime_type = self._sniff_mime(data)
        if mime_type is None:
            raise ValueError("unsupported image format; use JPEG, PNG, GIF or WebP")
        media_id = uuid.uuid4().hex
        storage_name = f"{media_id}{_MIME_TO_EXT[mime_type]}"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / storage_name).write_bytes(data)
        safe_name = Path(original_name or "image").name[:180] or "image"
        return MediaAsset(
            id=media_id,
            character_id=character_id,
            source=str(source or "GENERATED").strip()[:64] or "GENERATED",
            original_name=safe_name,
            mime_type=mime_type,
            storage_name=storage_name,
            created_at=created_at,
            size_bytes=len(data),
        )

    def save_data_url(
        self,
        *,
        character_id: str,
        original_name: str,
        data_url: str,
        created_at: datetime,
        source: str = "USER_UPLOAD",
    ) -> tuple[MediaAsset, str]:
        match = _DATA_URL_RE.match((data_url or "").strip())
        if match is None:
            raise ValueError("image must be a base64 data URL")
        try:
            data = base64.b64decode(match.group(2), validate=True)
        except ValueError as exc:
            raise ValueError("image base64 is invalid") from exc
        asset = self.save_bytes(
            character_id=character_id,
            original_name=original_name,
            payload=data,
            created_at=created_at,
            source=source,
        )
        normalized = f"data:{asset.mime_type};base64,{base64.b64encode(data).decode('ascii')}"
        return asset, normalized

    def data_url(self, asset: MediaAsset) -> str:
        path = self.asset_path(asset)
        if path is None:
            raise FileNotFoundError(asset.storage_name)
        return f"data:{asset.mime_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"

    def asset_path(self, asset: MediaAsset) -> Path | None:
        candidate = (self.root / asset.storage_name).resolve()
        root = self.root.resolve()
        if candidate.parent != root or candidate.suffix.lower() not in set(_MIME_TO_EXT.values()):
            return None
        return candidate if candidate.is_file() else None

    def delete(self, asset: MediaAsset) -> None:
        path = self.asset_path(asset)
        if path is not None:
            path.unlink(missing_ok=True)
