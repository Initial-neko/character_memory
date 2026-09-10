from __future__ import annotations

from collections import defaultdict
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import threading
from typing import Callable, Iterable
from uuid import uuid4
import zipfile

import yaml
from pydantic import BaseModel, Field


_ALLOWED_EXTENSIONS = {".png", ".webp", ".gif", ".svg", ".jpg", ".jpeg"}
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 160 * 1024 * 1024
_MAX_ARCHIVE_FILES = 500
_IMPORT_LOCKS_GUARD = threading.Lock()
_IMPORT_LOCKS: dict[str, threading.RLock] = {}


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

    def public_items(self, character_id: str | None = None) -> list[dict]:
        result = []
        for item in self.stickers:
            if self.asset_path(item.id) is None:
                continue
            if character_id:
                url = f"/v1/stickers/{character_id}/{item.id}/asset"
            else:
                url = f"/v1/stickers/{item.id}/asset"
            result.append({**item.model_dump(mode="json"), "url": url})
        return result


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


def _catalog_from_manifests(manifests: Iterable[Path], *, root: Path, source: str) -> StickerCatalog:
    merged: dict[str, Sticker] = {}
    roots: dict[str, Path] = {}
    for manifest in manifests:
        for item in _read_manifest(manifest):
            merged[item.id] = item
            roots[item.id] = manifest.parent
    return StickerCatalog(root, list(merged.values()), source=source, asset_roots=roots)


def load_global_sticker_catalog(
    global_dir: str | Path,
    *,
    persona_paths: Iterable[str | Path] = (),
) -> StickerCatalog:
    root = Path(global_dir)
    manifests: list[Path] = [_default_manifest()]
    seen: set[Path] = set()
    for persona_path in persona_paths:
        legacy = Path(persona_path).parent / "stickers" / "manifest.yaml"
        key = legacy.resolve()
        if legacy.is_file() and key not in seen:
            manifests.append(legacy)
            seen.add(key)
    manifests.append(root / "manifest.yaml")
    return _catalog_from_manifests(manifests, root=root, source="default+global+legacy")


def load_sticker_catalog(persona_path: str | Path) -> StickerCatalog:
    persona_path = Path(persona_path)
    local_manifest = persona_path.parent / "stickers" / "manifest.yaml"
    manifests = [_default_manifest()]
    if local_manifest.is_file():
        manifests.append(local_manifest)
    return _catalog_from_manifests(
        manifests,
        root=local_manifest.parent if local_manifest.is_file() else _default_manifest().parent,
        source="default+character" if local_manifest.is_file() else "default",
    )


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


def _import_lock_for(output_dir: Path) -> threading.RLock:
    key = str(output_dir.resolve())
    with _IMPORT_LOCKS_GUARD:
        lock = _IMPORT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _IMPORT_LOCKS[key] = lock
        return lock


def _durable_write(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _commit_prepared_import(output_dir: Path, prepared: list[tuple[Sticker, bytes]]) -> None:
    """Publish assets first under immutable content-addressed names, manifest last.

    Existing referenced assets are never overwritten. Until the final atomic
    manifest replace succeeds, readers keep seeing the previous complete pack.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = output_dir / "manifest.yaml"
    transaction_id = uuid4().hex
    created_assets: list[Path] = []
    temp_paths: list[Path] = []
    committed = False
    try:
        existing = {item.id: item for item in _read_manifest(manifest)}
        for sticker, payload in prepared:
            target = output_dir / sticker.file
            if target.exists():
                if target.read_bytes() != payload:
                    raise RuntimeError(f"content-address collision for sticker asset: {target.name}")
            else:
                temp = output_dir / f".{target.name}.{transaction_id}.tmp"
                temp_paths.append(temp)
                _durable_write(temp, payload)
                os.replace(temp, target)
                temp_paths.remove(temp)
                created_assets.append(target)
            existing[sticker.id] = sticker

        manifest_payload = {"stickers": [item.model_dump(mode="json") for item in existing.values()]}
        manifest_text = yaml.safe_dump(manifest_payload, allow_unicode=True, sort_keys=False).encode("utf-8")
        temp_manifest = output_dir / f".manifest.{transaction_id}.tmp"
        temp_paths.append(temp_manifest)
        _durable_write(temp_manifest, manifest_text)
        _read_manifest(temp_manifest)
        for sticker, _ in prepared:
            if not (output_dir / sticker.file).is_file():
                raise RuntimeError(f"prepared sticker asset missing before manifest commit: {sticker.file}")
        os.replace(temp_manifest, manifest)
        temp_paths.remove(temp_manifest)
        committed = True
    finally:
        for path in temp_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if not committed:
            for path in created_assets:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def import_sticker_bundle(
    persona_path: str | Path,
    archive_bytes: bytes,
    *,
    tagger: StickerTagger | None = None,
    default_pack_name: str = "自定义",
    target_dir: str | Path | None = None,
) -> dict:
    """Import a sticker ZIP using validate-first, manifest-last publication."""
    if not archive_bytes:
        raise ValueError("empty sticker archive")
    if len(archive_bytes) > _MAX_ARCHIVE_BYTES:
        raise ValueError("sticker archive is too large (max 64 MiB)")

    try:
        archive = zipfile.ZipFile(BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid sticker zip archive") from exc

    prepared: list[tuple[Sticker, bytes]] = []
    pack_names: dict[str, str] = {}
    ai_tagged = 0
    metadata_present = False

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

        # Phase 1: resolve, read, tag and validate every row without mutating the
        # current sticker library. Any later missing/ambiguous file aborts here.
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
            digest = hashlib.sha256(payload).hexdigest()[:16]
            output_name = f"{sticker_id}-{digest}{suffix}"
            prepared.append(
                (
                    Sticker(
                        id=sticker_id,
                        file=output_name,
                        label=label,
                        tags=tags,
                        description=description,
                        pack_id=pack_id,
                        pack_name=pack_name,
                    ),
                    payload,
                )
            )
            pack_names[pack_id] = pack_name

    if not prepared:
        raise ValueError("no supported sticker images were found in the archive")

    # Phase 2: serialize publication for this library. New immutable assets are
    # published first and the manifest pointer changes atomically only at the end.
    output_dir = Path(target_dir) if target_dir is not None else Path(persona_path).parent / "stickers"
    with _import_lock_for(output_dir):
        _commit_prepared_import(output_dir, prepared)

    return {
        "imported": len(prepared),
        "ai_tagged": ai_tagged,
        "metadata_present": metadata_present,
        "packs": [{"id": key, "name": value} for key, value in pack_names.items()],
        "manifest": str(output_dir / "manifest.yaml"),
    }
