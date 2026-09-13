from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INJECTED: set[tuple[str, str]] = set()


def _decode_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith('"') and value.endswith('"'):
        try:
            decoded = json.loads(value)
            return str(decoded)
        except json.JSONDecodeError:
            return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    # Keep inline '#' characters as part of the value. The Settings Center writes
    # quoted values, so this intentionally avoids shell-style comment guessing.
    return value


def parse_env_file(path: str | Path) -> dict[str, str]:
    target = Path(path)
    if not target.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not _ENV_NAME_RE.fullmatch(name):
            continue
        values[name] = _decode_value(raw_value)
    return values


def load_env_file(path: str | Path, *, override: bool = False) -> dict[str, str]:
    target = Path(path).resolve()
    values = parse_env_file(target)
    for name, value in values.items():
        if override or name not in os.environ:
            os.environ[name] = value
            _INJECTED.add((str(target), name))
    return values


def env_source(name: str, path: str | Path) -> str | None:
    target = Path(path).resolve()
    file_values = parse_env_file(target)
    injected = (str(target), name) in _INJECTED
    if name in os.environ and not injected:
        return "system"
    if name in file_values:
        return ".env"
    if name in os.environ:
        return "system"
    return None


def _encode_value(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text if text.endswith("\n") else text + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def upsert_env_value(path: str | Path, name: str, value: str) -> None:
    if not _ENV_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid environment variable name: {name}")
    target = Path(path)
    lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    replacement = f"{name}={_encode_value(value)}"
    updated: list[str] = []
    replaced = False
    pattern = re.compile(rf"^(?:export\s+)?{re.escape(name)}\s*=")
    for line in lines:
        if pattern.match(line.strip()):
            if not replaced:
                updated.append(replacement)
                replaced = True
            continue
        updated.append(line)
    if not replaced:
        if updated and updated[-1].strip():
            updated.append("")
        updated.append(replacement)
    _atomic_write(target, "\n".join(updated))
    # The current process should immediately observe Settings Center writes unless
    # a system-level environment variable already owns the effective value.
    if env_source(name, target) != "system":
        os.environ[name] = str(value)
        _INJECTED.add((str(target.resolve()), name))


def delete_env_value(path: str | Path, name: str) -> bool:
    target = Path(path)
    if not target.is_file():
        return False
    pattern = re.compile(rf"^(?:export\s+)?{re.escape(name)}\s*=")
    lines = target.read_text(encoding="utf-8").splitlines()
    updated = [line for line in lines if not pattern.match(line.strip())]
    if updated == lines:
        return False
    _atomic_write(target, "\n".join(updated))
    key = (str(target.resolve()), name)
    if key in _INJECTED:
        _INJECTED.discard(key)
        os.environ.pop(name, None)
    return True
