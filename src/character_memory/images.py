from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


_ALLOWED_EXTENSIONS = {".png", ".webp", ".gif", ".jpg", ".jpeg"}


class CharacterImage(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    file: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=12)
    description: str = Field(default="", max_length=320)


class ImageCatalog:
    """Curated images a character is allowed to send as IMAGE actions."""

    def __init__(self, root: Path, images: list[CharacterImage], *, source: str):
        self.root = root
        self.images = images
        self.source = source
        self._by_id = {item.id: item for item in images}

    def get(self, image_id: str | None) -> CharacterImage | None:
        if not image_id:
            return None
        return self._by_id.get(image_id)

    def asset_path(self, image_id: str) -> Path | None:
        item = self.get(image_id)
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
        if not self.images:
            return "- 无"
        rows = []
        for item in self.images:
            meaning = "、".join(item.tags) or item.description or item.label
            rows.append(f"- {item.id}: {item.label}；适合：{meaning}")
        return "\n".join(rows)

    def public_items(self, character_id: str) -> list[dict]:
        return [
            {
                **item.model_dump(mode="json"),
                "url": f"/v1/images/{character_id}/{item.id}/asset",
            }
            for item in self.images
            if self.asset_path(item.id) is not None
        ]


def _read_manifest(path: Path) -> list[CharacterImage]:
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    values = data.get("images") or []
    images = [CharacterImage.model_validate(value) for value in values]
    seen: set[str] = set()
    result: list[CharacterImage] = []
    for item in images:
        if item.id in seen:
            raise ValueError(f"duplicate image id: {item.id}")
        seen.add(item.id)
        result.append(item)
    return result


def load_image_catalog(persona_path: str | Path) -> ImageCatalog:
    persona_path = Path(persona_path)
    manifest = persona_path.parent / "images" / "manifest.yaml"
    if manifest.is_file():
        return ImageCatalog(manifest.parent, _read_manifest(manifest), source="character")
    return ImageCatalog(manifest.parent, [], source="none")
