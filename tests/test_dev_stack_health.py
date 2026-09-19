"""Behaviour tests for the stack readiness probe.

`tests/test_dev_stack.py` asserts on source strings, so it cannot catch the bug
these cover: GSV-TTS-Lite answers /health with HTTP 200 and `ready: false` when
its model assets are unconfigured, so a status-code-only probe reports a runtime
that cannot synthesise as ready.

No sockets here -- `urlopen` is monkeypatched.
"""

from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from character_memory import dev_stack


class FakeResponse:
    def __init__(self, status: int, body: bytes, content_type: str | None):
        self.status = status
        self._body = body
        self.headers = {} if content_type is None else {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _patch(monkeypatch, response=None, error=None):
    def fake_urlopen(url, timeout=None):
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(dev_stack, "urlopen", fake_urlopen)


def _json(payload: dict, status: int = 200) -> FakeResponse:
    return FakeResponse(status, json.dumps(payload).encode("utf-8"), "application/json")


def test_ready_false_is_not_healthy_and_reports_the_reason(monkeypatch):
    _patch(
        monkeypatch,
        _json(
            {
                "ok": True,
                "ready": False,
                "reason": "Missing GSV configuration: GSV_TTS_GPT_MODEL",
            }
        ),
    )
    listening, reason = dev_stack._probe("http://127.0.0.1:9014/health")
    assert listening is True
    assert reason == "Missing GSV configuration: GSV_TTS_GPT_MODEL"
    assert dev_stack._healthy("http://127.0.0.1:9014/health") is False


def test_ready_false_without_a_reason_still_reports_something(monkeypatch):
    _patch(monkeypatch, _json({"ok": True, "ready": False}))
    listening, reason = dev_stack._probe("http://127.0.0.1:9014/health")
    assert listening is True
    assert reason == "not ready"


def test_ready_true_is_healthy(monkeypatch):
    _patch(monkeypatch, _json({"ok": True, "ready": True, "loaded": False}))
    assert dev_stack._healthy("http://127.0.0.1:9014/health") is True


def test_missing_ready_field_keeps_the_listening_meaning(monkeypatch):
    """Media Runtime and Settings Center report no top-level `ready`."""
    _patch(monkeypatch, _json({"ok": True, "service": "character-media"}))
    assert dev_stack._healthy("http://127.0.0.1:8001/health") is True


@pytest.mark.parametrize("value", [0, "", None, []])
def test_falsy_but_not_false_ready_stays_healthy(monkeypatch, value):
    """`ready: 0` is not the same statement as `ready: false`.

    Only an explicit JSON false is a readiness contract; anything else is a
    field that happens to be falsy and must not reclassify a live service.
    """
    _patch(monkeypatch, _json({"ok": True, "ready": value}))
    assert dev_stack._healthy("http://127.0.0.1:9002/health") is True


def test_html_body_keeps_the_listening_meaning(monkeypatch):
    """TTS Lab is probed through /tts, which serves HTML."""
    _patch(monkeypatch, FakeResponse(200, b"<!doctype html><html></html>", "text/html; charset=utf-8"))
    assert dev_stack._healthy("http://127.0.0.1:9002/tts") is True


def test_json_content_type_with_charset_is_parsed(monkeypatch):
    _patch(
        monkeypatch,
        FakeResponse(
            200,
            json.dumps({"ready": False, "reason": "no assets"}).encode("utf-8"),
            "application/json; charset=utf-8",
        ),
    )
    assert dev_stack._healthy("http://127.0.0.1:9014/health") is False


def test_invalid_json_is_treated_as_listening(monkeypatch):
    _patch(monkeypatch, FakeResponse(200, b"<not json>", "application/json"))
    assert dev_stack._healthy("http://127.0.0.1:9014/health") is True


def test_non_2xx_is_not_listening(monkeypatch):
    _patch(monkeypatch, _json({"ready": True}, status=503))
    assert dev_stack._healthy("http://127.0.0.1:9014/health") is False


def test_connection_refused_is_not_listening(monkeypatch):
    _patch(monkeypatch, error=URLError("connection refused"))
    listening, reason = dev_stack._probe("http://127.0.0.1:9014/health")
    assert listening is False
    assert reason is None
    assert dev_stack._healthy("http://127.0.0.1:9014/health") is False


def test_os_error_is_not_listening(monkeypatch):
    _patch(monkeypatch, error=OSError("timed out"))
    assert dev_stack._healthy("http://127.0.0.1:9014/health") is False
