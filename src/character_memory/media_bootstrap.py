from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

# Keep add_dll_directory handles alive for the lifetime of the media process.
_DLL_DIRECTORY_HANDLES: list[object] = []


def _candidate_package_dirs(package_name: str) -> list[Path]:
    spec = importlib.util.find_spec(package_name)
    if spec is None or not spec.origin:
        return []
    package_dir = Path(spec.origin).resolve().parent
    return [package_dir, package_dir / "lib", package_dir / "capi"]


def prepare_windows_native_runtime() -> list[str]:
    """Prefer wheel-bundled native DLLs over stale system-wide copies.

    sherpa-onnx wheels bundle a matching onnxruntime.dll. Some Windows 11
    installations also contain an older C:\\Windows\\System32\\onnxruntime.dll;
    if that DLL wins resolution, importing/loading sherpa native components can
    abort the process with an ORT API-version mismatch. Keep this workaround
    process-local instead of modifying System32.
    """
    if sys.platform != "win32" or not hasattr(os, "add_dll_directory"):
        return []

    added: list[str] = []
    seen: set[str] = set()
    for package_name in ("sherpa_onnx", "onnxruntime"):
        for candidate in _candidate_package_dirs(package_name):
            if not candidate.is_dir():
                continue
            value = str(candidate)
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            try:
                handle = os.add_dll_directory(value)
            except OSError:
                continue
            _DLL_DIRECTORY_HANDLES.append(handle)
            os.environ["PATH"] = value + os.pathsep + os.environ.get("PATH", "")
            added.append(value)
    return added


def main() -> None:
    added = prepare_windows_native_runtime()
    if added:
        print("media: Windows native DLL directories:")
        for path in added:
            print(f"  {path}")

    # Import only after Windows native search paths have been fixed.
    from character_memory.media_server import main as media_main

    media_main()


if __name__ == "__main__":
    main()
