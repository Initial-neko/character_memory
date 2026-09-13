from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    # Keep inline '#' characters as part of the value. Settings Center writes
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


def effective_env_value(name: str, path: str | Path, fallback: str = "") -> str:
    """Resolve one secret without mutating process environment.

    Process environment is the deployment override. Project-local .env is the
    persistent Settings Center store. The fallback is only for legacy config.
    Keeping this read side-effect free prevents one config root/test from leaking
    its .env values into another config root in the same Python process.
    """

    if name in os.environ:
        return os.environ[name]
    return parse_env_file(path).get(name, fallback)


def env_source(name: str, path: str | Path) -> str | None:
    if name in os.environ:
        return "system"
    if name in parse_env_file(path):
        return ".env"
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
    return True
