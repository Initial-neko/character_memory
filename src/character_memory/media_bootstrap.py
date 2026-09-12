from __future__ import annotations

import ctypes
import importlib.util
import os
from pathlib import Path
import sys

# Keep Windows DLL directory and library handles alive for the lifetime of the
# media process. In particular, sherpa-onnx-core installs native DLLs into the
# virtualenv Scripts directory on Windows, while some machines also contain a
# stale C:\\Windows\\System32\\onnxruntime.dll.
_DLL_DIRECTORY_HANDLES: list[object] = []
_PRELOADED_DLL_HANDLES: list[object] = []


def _candidate_package_dirs(package_name: str) -> list[Path]:
    spec = importlib.util.find_spec(package_name)
    if spec is None or not spec.origin:
        return []
    package_dir = Path(spec.origin).resolve().parent
    return [package_dir, package_dir / "lib", package_dir / "capi"]


def _candidate_native_dirs() -> list[Path]:
    """Return native-library directories in deliberate Windows priority order."""
    candidates: list[Path] = []

    # sherpa-onnx-core Windows wheels install onnxruntime.dll and sherpa native
    # DLLs into the environment Scripts directory. This must come before
    # System32 and before generic package directories.
    executable_dir = Path(sys.executable).resolve().parent
    candidates.append(executable_dir)
    candidates.append(Path(sys.prefix).resolve() / "Scripts")

    for package_name in ("sherpa_onnx", "onnxruntime"):
        candidates.extend(_candidate_package_dirs(package_name))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def bundled_onnxruntime_path() -> str | None:
    """Return a project/environment-local ORT DLL, never a system-wide copy."""
    for directory in _candidate_native_dirs():
        dll = directory / "onnxruntime.dll"
        if dll.is_file():
            return str(dll.resolve())
    return None


def _preload_bundled_onnxruntime(native_dirs: list[Path]) -> str | None:
    """Load the environment's ORT by absolute path before sherpa imports."""
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return None

    for directory in native_dirs:
        dll = directory / "onnxruntime.dll"
        if not dll.is_file():
            continue
        try:
            # LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS
            handle = loader(str(dll.resolve()), winmode=0x00001100)
        except TypeError:
            handle = loader(str(dll.resolve()))
        except OSError:
            continue
        _PRELOADED_DLL_HANDLES.append(handle)
        return str(dll.resolve())
    return None


def prepare_windows_native_runtime() -> list[str]:
    """Prefer environment-local native DLLs over stale system-wide copies."""
    if sys.platform != "win32" or not hasattr(os, "add_dll_directory"):
        return []

    native_dirs = _candidate_native_dirs()
    added: list[str] = []
    for candidate in native_dirs:
        if not candidate.is_dir():
            continue
        value = str(candidate.resolve())
        try:
            handle = os.add_dll_directory(value)
        except OSError:
            continue
        _DLL_DIRECTORY_HANDLES.append(handle)
        os.environ["PATH"] = value + os.pathsep + os.environ.get("PATH", "")
        added.append(value)

    _preload_bundled_onnxruntime([Path(value) for value in added])
    return added


def require_windows_bundled_onnxruntime() -> str | None:
    """Fail before sherpa import instead of falling back to System32 ORT."""
    if sys.platform != "win32":
        return None
    bundled = bundled_onnxruntime_path()
    if bundled:
        return bundled
    raise RuntimeError(
        "Media Runtime native ONNX Runtime is missing from this environment. "
        "Install the media extra with the matching sherpa native core "
        "(sherpa-onnx==1.13.5 and sherpa-onnx-core==1.13.5). "
        "Refusing to continue because Windows may otherwise load an incompatible "
        "C:\\Windows\\System32\\onnxruntime.dll and crash the process."
    )


def main() -> None:
    added = prepare_windows_native_runtime()
    if added:
        print("media: Windows native DLL directories:")
        for path in added:
            print(f"  {path}")

    try:
        bundled = require_windows_bundled_onnxruntime()
    except RuntimeError as exc:
        raise SystemExit(f"media: FATAL {exc}") from exc
    if bundled:
        print(f"media: bundled onnxruntime: {bundled}")

    # Import only after Windows native search paths and explicit ORT preload have
    # been applied and a local ORT DLL has been verified.
    from character_memory.media_server import main as media_main

    media_main()


if __name__ == "__main__":
    main()
