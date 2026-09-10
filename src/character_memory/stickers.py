from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


_ALLOWED_EXTENSIONS = {".png", ".webp", ".gif", ".svg", ".jpg", ".jpeg"}


class Sticker(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    file: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=12)
    description: str = Field(default="", max_length=240)


class StickerCatalog:
    def __init__(self, root: Path, stickers: list[Sticker], *, source: str):
        self.root = root
        self.stickers = stickers
        self.source = source
        self._by_id = {item.id: item for item in stickers}

    def get(self, sticker_id: str) -> Sticker | None:
        return self._by_id.get(sticker_id)

    def asset_path(self, sticker_id: str) -> Path | None:
        item = self.get(sticker_id)
        if item is None:
            return None
        candidate = (self.root / item.file).resolve()
        root = self.root.resolve()
        if candidate.parent != root or candidate.suffix.lower() not in _ALLOWED_EXTENSIONS:
            return None
        if not candidate.is_file():
            return None
        return candidate

    def prompt_text(self) -> str:
        if not self.stickers:
            return "- 无"
        rows = []
        for item in self.stickers:
            meaning = "、".join(item.tags) or item.description or item.label
            rows.append(f"- {item.id}: {item.label}；适合：{meaning}")
        return "\n".join(rows)

    def public_items(self, character_id: str) -> list[dict]:
        return [
            {
                **item.model_dump(mode="json"),
                "url": f"/v1/stickers/{character_id}/{item.id}/asset",
            }
            for item in self.stickers
            if self.asset_path(item.id) is not None
        ]


def _default_manifest() -> Path:
    return Path(__file__).with_name("web") / "stickers" / "default" / "manifest.yaml"


def _read_manifest(path: Path) -> list[Sticker]:
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    values = data.get("stickers") or []
    stickers = [Sticker.model_validate(value) for value in values]
    seen: set[str] = set()
    result: list[Sticker] = []
    for item in stickers:
        if item.id in seen:
            raise ValueError(f"duplicate sticker id: {item.id}")
        seen.add(item.id)
        result.append(item)
    return result


def load_sticker_catalog(persona_path: str | Path) -> StickerCatalog:
    persona_path = Path(persona_path)
    local_manifest = persona_path.parent / "stickers" / "manifest.yaml"
    if local_manifest.is_file():
        return StickerCatalog(local_manifest.parent, _read_manifest(local_manifest), source="character")
    default_manifest = _default_manifest()
    return StickerCatalog(default_manifest.parent, _read_manifest(default_manifest), source="default")
