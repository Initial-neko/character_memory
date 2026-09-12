from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import character_memory.media_bootstrap as bootstrap


def test_non_windows_bootstrap_is_noop(monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "platform", "linux")
    assert bootstrap.prepare_windows_native_runtime() == []


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

    added = bootstrap.prepare_windows_native_runtime()

    assert str(sherpa_lib.resolve()) in added
    assert str(ort_capi.resolve()) in added
    assert calls == added
    assert len(bootstrap._DLL_DIRECTORY_HANDLES) == len(added)
    assert bootstrap.os.environ["PATH"].endswith("ORIGINAL")
