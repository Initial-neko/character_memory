from __future__ import annotations

from collections import defaultdict
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
from typing import Callable
import zipfile

import yaml
from pydantic import BaseModel, Field


_ALLOWED_EXTENSIONS = {".png", ".webp", ".gif", ".svg", ".jpg", ".jpeg"}
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 160 * 1024 * 1024
_MAX_ARCHIVE_FILES = 500


class Sticker(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    file: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=12)
    description: str = Field(default="", max_length=240)
    pack_id: str = Field(default="default", min_length=1, max_length=64)
    pack_name: str = Field(default="内置", min_length=1, max_length=80)


class StickerTagSuggestion(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=12)
    description: str = Field(default="", max_length=240)


StickerTagger = Callable[[str, bytes], StickerTagSuggestion | dict]


class StickerCatalog:
    def __init__(
        self,
        root: Path,
        stickers: list[Sticker],
        *,
        source: str,
        asset_roots: dict[str, Path] | None = None,
    ):
        self.root = root
        self.stickers = stickers
        self.source = source
        self._by_id = {item.id: item for item in stickers}
        self._asset_roots = {key: value.resolve() for key, value in (asset_roots or {}).items()}

    def get(self, sticker_id: str) -> Sticker | None:
        return self._by_id.get(sticker_id)

    def asset_path(self, sticker_id: str) -> Path | None:
        item = self.get(sticker_id)
        if item is None:
            return None
        root = self._asset_roots.get(sticker_id, self.root.resolve())
        candidate = (root / item.file).resolve()
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


def _merge_catalogs(default_manifest: Path, local_manifest: Path) -> StickerCatalog:
    defaults = _read_manifest(default_manifest)
    locals_ = _read_manifest(local_manifest)
    merged: dict[str, Sticker] = {item.id: item for item in defaults}
    merged.update({item.id: item for item in locals_})
    roots = {item.id: default_manifest.parent for item in defaults}
    roots.update({item.id: local_manifest.parent for item in locals_})
    return StickerCatalog(
        local_manifest.parent,
        list(merged.values()),
        source="default+character",
        asset_roots=roots,
    )


def load_sticker_catalog(persona_path: str | Path) -> StickerCatalog:
    persona_path = Path(persona_path)
    local_manifest = persona_path.parent / "stickers" / "manifest.yaml"
    default_manifest = _default_manifest()
    if local_manifest.is_file():
        return _merge_catalogs(default_manifest, local_manifest)
    return StickerCatalog(default_manifest.parent, _read_manifest(default_manifest), source="default")


def _safe_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe zip path: {name}")
    return str(path)


def _safe_id(value: object, fallback: str) -> str:
    raw = str(value or fallback).strip()
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")
    if not normalized:
        normalized = fallback
    return normalized[:64]


def _dedupe_tags(values: list[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text[:40])
        if len(result) >= 12:
            break
    return result


def _metadata_rows(archive: zipfile.ZipFile, names: list[str], *, required: bool = True) -> list[dict]:
    all_tags = [name for name in names if PurePosixPath(name).name == "all_tags.json"]
    candidates = all_tags[:1] if all_tags else [name for name in names if PurePosixPath(name).name == "tags.json"]
    rows: list[dict] = []
    for name in candidates:
        try:
            value = json.loads(archive.read(name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid sticker metadata JSON: {name}") from exc
        if not isinstance(value, list):
            raise ValueError(f"sticker metadata must be a JSON list: {name}")
        rows.extend(item for item in value if isinstance(item, dict))
    if required and not rows:
        raise ValueError("zip must contain all_tags.json or one or more tags.json files")
    return rows


def _supported_asset_names(names: list[str]) -> list[str]:
    return [name for name in names if Path(PurePosixPath(name).name).suffix.lower() in _ALLOWED_EXTENSIONS]


def _as_tag_suggestion(value: StickerTagSuggestion | dict) -> StickerTagSuggestion:
    return value if isinstance(value, StickerTagSuggestion) else StickerTagSuggestion.model_validate(value)


def _needs_ai_fill(row: dict) -> bool:
    has_label = bool(str(row.get("tag_zh") or row.get("label") or row.get("tag_en") or "").strip())
    aliases = row.get("aliases") if isinstance(row.get("aliases"), list) else []
    has_semantics = bool(row.get("category") or row.get("tag_zh") or row.get("tag_en") or aliases or row.get("description"))
    return not has_label or not has_semantics


def import_sticker_bundle(
    persona_path: str | Path,
    archive_bytes: bytes,
    *,
    tagger: StickerTagger | None = None,
    default_pack_name: str = "自定义",
) -> dict:
    """Import a sticker ZIP into one character's local sticker library.

    Preferred format is the tagged bundle used by this project: `all_tags.json` or
    per-pack `tags.json` with filename/id/tag_zh/aliases/description and optional
    set_id/display_name. If metadata is absent and `tagger` is provided, every
    supported image is tagged by that callback. Existing metadata wins; AI is only
    asked to fill rows whose label/semantic fields are missing.
    """
    if not archive_bytes:
        raise ValueError("empty sticker archive")
    if len(archive_bytes) > _MAX_ARCHIVE_BYTES:
        raise ValueError("sticker archive is too large (max 64 MiB)")

    try:
        archive = zipfile.ZipFile(BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid sticker zip archive") from exc

    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > _MAX_ARCHIVE_FILES:
            raise ValueError("sticker archive contains too many files")
        total_uncompressed = sum(info.file_size for info in infos)
        if total_uncompressed > _MAX_UNCOMPRESSED_BYTES:
            raise ValueError("sticker archive expands beyond 160 MiB")
        names = [_safe_member_name(info.filename) for info in infos]
        info_by_name = {name: info for name, info in zip(names, infos)}
        by_basename: dict[str, list[str]] = defaultdict(list)
        for name in names:
            by_basename[PurePosixPath(name).name].append(name)

        rows = _metadata_rows(archive, names, required=False)
        metadata_present = bool(rows)
        if not rows:
            if tagger is None:
                raise ValueError("zip has no sticker metadata; enable AI auto-tag or add all_tags.json/tags.json")
            asset_names = _supported_asset_names(names)
            if not asset_names:
                raise ValueError("no supported sticker images were found in the archive")
            rows = [
                {
                    "id": _safe_id(PurePosixPath(name).stem, f"sticker_{index:03d}"),
                    "filename": PurePosixPath(name).name,
                    "_member_name": name,
                    "set_id": "custom_ai",
                    "display_name": default_pack_name or "AI 自动标签",
                }
                for index, name in enumerate(asset_names, start=1)
            ]

        target_dir = Path(persona_path).parent / "stickers"
        target_dir.mkdir(parents=True, exist_ok=True)
        local_manifest = target_dir / "manifest.yaml"
        existing = {item.id: item for item in _read_manifest(local_manifest)}

        imported: list[Sticker] = []
        pack_names: dict[str, str] = {}
        ai_tagged = 0
        for index, source_row in enumerate(rows, start=1):
            row = dict(source_row)
            filename = str(row.get("filename") or row.get("file") or "").strip()
            if not filename:
                continue
            basename = PurePosixPath(filename.replace("\\", "/")).name
            suffix = Path(basename).suffix.lower()
            if suffix not in _ALLOWED_EXTENSIONS:
                continue

            member_name = str(row.get("_member_name") or "")
            if member_name:
                if member_name not in info_by_name:
                    raise ValueError(f"cannot locate sticker asset: {basename}")
            else:
                matches = by_basename.get(basename, [])
                if len(matches) != 1:
                    raise ValueError(f"cannot uniquely locate sticker asset: {basename}")
                member_name = matches[0]

            payload = archive.read(info_by_name[member_name])
            if tagger is not None and _needs_ai_fill(row):
                suggestion = _as_tag_suggestion(tagger(basename, payload))
                row.setdefault("label", suggestion.label)
                if not row.get("tag_zh") and not row.get("tag_en"):
                    row["label"] = suggestion.label
                if not row.get("aliases"):
                    row["aliases"] = suggestion.tags
                if not row.get("description"):
                    row["description"] = suggestion.description
                ai_tagged += 1

            sticker_id = _safe_id(row.get("id"), f"sticker_{index:03d}")
            pack_id = _safe_id(row.get("set_id") or row.get("pack_id"), "custom")
            pack_name = str(row.get("display_name") or row.get("pack_name") or row.get("set_name") or default_pack_name or "自定义").strip()[:80] or "自定义"
            label = str(row.get("tag_zh") or row.get("label") or row.get("tag_en") or sticker_id).strip()[:80] or sticker_id
            aliases = row.get("aliases") if isinstance(row.get("aliases"), list) else []
            tags = _dedupe_tags([
                row.get("category"),
                row.get("tag_zh"),
                row.get("tag_en"),
                *aliases,
            ])
            description = str(row.get("description") or "").strip()[:240]
            output_name = f"{sticker_id}{suffix}"
            (target_dir / output_name).write_bytes(payload)
            sticker = Sticker(
                id=sticker_id,
                file=output_name,
                label=label,
                tags=tags,
                description=description,
                pack_id=pack_id,
                pack_name=pack_name,
            )
            existing[sticker.id] = sticker
            imported.append(sticker)
            pack_names[pack_id] = pack_name

        if not imported:
            raise ValueError("no supported sticker images were found in the archive")

        manifest_payload = {
            "stickers": [item.model_dump(mode="json") for item in existing.values()]
        }
        local_manifest.write_text(
            yaml.safe_dump(manifest_payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return {
            "imported": len(imported),
            "ai_tagged": ai_tagged,
            "metadata_present": metadata_present,
            "packs": [{"id": key, "name": value} for key, value in pack_names.items()],
            "manifest": str(local_manifest),
        }
