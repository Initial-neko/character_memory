from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import character_memory.media_bootstrap as bootstrap


def test_non_windows_bootstrap_is_noop(monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "platform", "linux")
    assert bootstrap.prepare_windows_native_runtime() == []
    assert bootstrap.require_windows_bundled_onnxruntime() is None


def test_windows_bootstrap_prefers_package_native_dirs(monkeypatch, tmp_path):
    sherpa = tmp_path / "sherpa_onnx"
    sherpa_lib = sherpa / "lib"
    ort = tmp_path / "onnxruntime"
    ort_capi = ort / "capi"
    for directory in (sherpa_lib, ort_capi):
        directory.mkdir(parents=True)
    (sherpa / "__init__.py").write_text("", encoding="utf-8")
    (ort / "__init__.py").write_text("", encoding="utf-8")

    def fake_find_spec(name):
        if name == "sherpa_onnx":
            return SimpleNamespace(origin=str(sherpa / "__init__.py"))
        if name == "onnxruntime":
            return SimpleNamespace(origin=str(ort / "__init__.py"))
        return None

    calls = []

    class Handle:
        pass

    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(bootstrap.importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(bootstrap.os, "add_dll_directory", lambda value: calls.append(value) or Handle(), raising=False)
    monkeypatch.setenv("PATH", "ORIGINAL")
    bootstrap._DLL_DIRECTORY_HANDLES.clear()
    bootstrap._PRELOADED_DLL_HANDLES.clear()

    added = bootstrap.prepare_windows_native_runtime()

    assert str(sherpa_lib.resolve()) in added
    assert str(ort_capi.resolve()) in added
    assert calls == added
    assert len(bootstrap._DLL_DIRECTORY_HANDLES) == len(added)
    assert bootstrap.os.environ["PATH"].endswith("ORIGINAL")


def test_windows_bootstrap_preloads_core_wheel_ort_from_scripts(monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    python_exe = scripts / "python.exe"
    python_exe.write_bytes(b"")
    ort_dll = scripts / "onnxruntime.dll"
    ort_dll.write_bytes(b"fake")

    added_dirs = []
    loaded = []

    class Handle:
        pass

    def fake_loader(path, **kwargs):
        loaded.append((str(path), kwargs))
        return Handle()

    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(bootstrap.sys, "executable", str(python_exe))
    monkeypatch.setattr(bootstrap.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(bootstrap.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(bootstrap.os, "add_dll_directory", lambda value: added_dirs.append(value) or Handle(), raising=False)
    monkeypatch.setattr(bootstrap.ctypes, "WinDLL", fake_loader, raising=False)
    bootstrap._DLL_DIRECTORY_HANDLES.clear()
    bootstrap._PRELOADED_DLL_HANDLES.clear()

    added = bootstrap.prepare_windows_native_runtime()

    assert str(scripts.resolve()) in added
    assert loaded == [(str(ort_dll.resolve()), {"winmode": 0x00001100})]
    assert bootstrap.bundled_onnxruntime_path() == str(ort_dll.resolve())
    assert bootstrap.require_windows_bundled_onnxruntime() == str(ort_dll.resolve())
    assert len(bootstrap._PRELOADED_DLL_HANDLES) == 1


def test_windows_bootstrap_refuses_system_ort_fallback(monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    python_exe = scripts / "python.exe"
    python_exe.write_bytes(b"")

    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(bootstrap.sys, "executable", str(python_exe))
    monkeypatch.setattr(bootstrap.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(bootstrap.importlib.util, "find_spec", lambda name: None)

    with pytest.raises(RuntimeError, match="native ONNX Runtime is missing"):
        bootstrap.require_windows_bundled_onnxruntime()
