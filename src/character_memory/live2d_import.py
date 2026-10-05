"""Validated local model import; an atomic pointer publishes immutable bundles."""
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import uuid
import zipfile

MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_FILES = 512
BINDING = "_binding.json"
ASSET_SUFFIXES = {".json", ".moc3", ".png", ".jpg", ".jpeg", ".webp"}


def safe_path(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or name.startswith("/"):
        raise ValueError("模型资源路径无效")
    parts = name.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}
    if any(p in {"", ".", ".."} or p.endswith((".", " ")) or p.split(".")[0].upper() in reserved or any(ord(c) < 32 or c in '<>"|?*' for c in p) for p in parts):
        raise ValueError("模型资源路径无效")
    return PurePosixPath(name)


def active_manifest(folder):
    pointer = folder / BINDING
    if not pointer.exists():
        return False, None
    try:
        if pointer.resolve().parent != folder or pointer.stat().st_size > 4096:
            return True, None
        data = json.loads(pointer.read_text(encoding="utf-8"))
        version = data.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{32}", version):
            return True, None
        name = safe_path(data["manifest"])
        base = (folder / "_versions" / version).resolve()
        if not base.is_relative_to(folder):
            return True, None
        path = (base / str(name)).resolve()
        if path.is_relative_to(base) and path.is_file() and path.stat().st_size <= 1024 * 1024:
            return True, path
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return True, None


def publish(folder, value):
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / (".binding-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, folder / BINDING)
    finally:
        temporary.unlink(missing_ok=True)


def import_model(folder: Path, content: bytes):
    """Validate before touching the binding. Never extract arbitrary ZIP paths."""
    from PIL import Image
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES or sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
                raise ValueError("模型 ZIP 文件过多或解压后超过 256 MiB")
            files, seen = {}, set()
            for item in entries:
                name = item.filename.rstrip("/") if item.is_dir() else item.filename
                path = safe_path(name)
                if name.casefold() in seen or stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("模型 ZIP 存在重名文件或符号链接")
                seen.add(name.casefold())
                if item.is_dir():
                    continue
                if item.file_size > MAX_FILE_BYTES or item.flag_bits & 1:
                    raise ValueError("模型资源超过 64 MiB 或 ZIP 已加密")
                if path.suffix.lower() not in ASSET_SUFFIXES | {".cmo3", ".psd"}:
                    raise ValueError("模型 ZIP 包含不支持的文件类型")
                files[name] = item
            folded = {name.casefold() for name in files}
            if any("/".join(name.casefold().split("/")[:i]) in folded for name in files for i in range(1, len(name.split("/")))):
                raise ValueError("模型 ZIP 的文件与目录路径冲突")
            candidates = [name for name in files if name.endswith(".model3.json") or PurePosixPath(name).name == "model3.json"]
            if len(candidates) != 1:
                raise ValueError("ZIP 必须包含且只包含一个 model3.json 入口")
            manifest_name = candidates[0]
            if files[manifest_name].file_size > 1024 * 1024:
                raise ValueError("模型清单超过 1 MiB")
            manifest_data = archive.read(files[manifest_name])
            model = json.loads(manifest_data)
            refs = model.get("FileReferences")
            if model.get("Version") != 3 or not isinstance(refs, dict) or not isinstance(refs.get("Moc"), str) or not isinstance(refs.get("Textures"), list) or not refs["Textures"]:
                raise ValueError("模型清单缺少 Moc 或纹理引用")
            if PurePosixPath(refs["Moc"]).suffix.lower() != ".moc3" or any(not isinstance(t, str) or PurePosixPath(t).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} for t in refs["Textures"]):
                raise ValueError("Moc 或纹理资源类型无效")
            motions = refs.get("Motions", {})
            expressions = refs.get("Expressions", [])
            if not isinstance(motions, dict) or not isinstance(expressions, list):
                raise ValueError("动作或表情清单格式无效")
            for group, items in motions.items():
                if not isinstance(group, str) or not isinstance(items, list) or any(not isinstance(item, dict) or not isinstance(item.get("File"), str) for item in items):
                    raise ValueError("动作资源引用格式无效")
            if any(not isinstance(item, dict) or not isinstance(item.get("Name"), str) or not isinstance(item.get("File"), str) for item in expressions):
                raise ValueError("表情资源引用格式无效")
            if len({item["Name"] for item in expressions}) != len(expressions):
                raise ValueError("表情名称重复")
            paths = set()
            def collect(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key in {"Moc", "Physics", "Pose", "DisplayInfo", "UserData", "File", "Sound"}:
                            paths.add(str(safe_path(item)))
                        elif key == "Textures":
                            for texture in item:
                                paths.add(str(safe_path(texture)))
                        else:
                            collect(item)
                elif isinstance(value, list):
                    for item in value:
                        collect(item)
            collect(refs)
            parent = PurePosixPath(manifest_name).parent
            assets = {PurePosixPath(manifest_name).name: manifest_data}
            for relative in paths:
                entry = files.get(str(parent / relative))
                if entry is None or not entry.file_size:
                    raise ValueError("模型引用的资源缺失或为空：" + relative)
                if PurePosixPath(relative).suffix.lower() not in ASSET_SUFFIXES:
                    raise ValueError("模型引用了不支持的资源：" + relative)
                data = archive.read(entry)
                suffix = PurePosixPath(relative).suffix.lower()
                if suffix == ".json":
                    if len(data) > 1024 * 1024 or not isinstance(json.loads(data), dict):
                        raise ValueError("模型 JSON 资源无效：" + relative)
                elif suffix == ".moc3":
                    if len(data) < 64 or data[:4] != b"MOC3" or data[4] not in range(1, 6):
                        raise ValueError("MOC3 文件头无效或版本不支持")
                else:
                    with Image.open(io.BytesIO(data)) as image:
                        if image.width * image.height > 32 * 1024 * 1024:
                            raise ValueError("纹理尺寸过大")
                        image.verify()
                    with Image.open(io.BytesIO(data)) as image:
                        image.load()
                assets[relative] = data
    except (zipfile.BadZipFile, RuntimeError, OSError, UnicodeError, TypeError, AttributeError, KeyError, RecursionError, Image.DecompressionBombError) as exc:
        raise ValueError("模型 ZIP 或资源损坏，请重新导出") from exc
    version = uuid.uuid4().hex
    versions = (folder / "_versions").resolve()
    if not versions.is_relative_to(folder):
        raise ValueError("模型目录无效")
    staging = versions / version
    staging.mkdir(parents=True)
    published = False
    try:
        for relative, data in assets.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        publish(folder, {"version": version, "manifest": PurePosixPath(manifest_name).name})
        published = True
    finally:
        if not published:
            shutil.rmtree(staging)
    return {"resource_count": len(assets), "size_bytes": sum(map(len, assets.values()))}
