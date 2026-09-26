"""The capture instrument: what it stores, what it refuses, and when it stays quiet."""

import json

import numpy as np
import pytest

from character_memory.asr_capture import AsrCaptureStore
from character_memory.media_runtime import (
    MediaRuntime,
    SpeechRecognitionProvider,
    TextToSpeechProvider,
    TranscriptionResult,
    float_audio_to_wav,
)


def make_wav(duration_ms: int = 200, sample_rate: int = 16000) -> bytes:
    count = int(sample_rate * duration_ms / 1000)
    t = np.arange(count, dtype=np.float32) / sample_rate
    return float_audio_to_wav(0.1 * np.sin(2 * np.pi * 440 * t), sample_rate)


class _Result:
    """Stands in for TranscriptionResult in store-only tests."""

    def __init__(self, text: str = "你好", **overrides):
        self.text = text
        self.provider = overrides.get("provider", "fake-asr")
        self.model = overrides.get("model", "fake")
        self.device = overrides.get("device", "cpu")
        self.inference_ms = overrides.get("inference_ms", 12.5)
        self.audio_ms = overrides.get("audio_ms", 200.0)


class _FakeAsr(SpeechRecognitionProvider):
    def __init__(self, text: str = "测试语音"):
        self.text = text
        self.calls = 0

    def transcribe(self, samples, sample_rate):
        self.calls += 1
        return TranscriptionResult(
            text=self.text,
            provider="fake-asr",
            model="fake",
            device="cpu",
            inference_ms=12.5,
            audio_ms=len(samples) * 1000.0 / sample_rate,
        )

    def status(self):
        return {"ready": True, "loaded": True, "provider": "fake-asr", "device": "cpu"}


class _UnusedTts(TextToSpeechProvider):
    def synthesize(self, text, *, speaker_id=0, speed=1.0):
        raise AssertionError("capture tests do not synthesize")

    def status(self):
        return {"ready": True, "provider": "unused"}


def test_nothing_is_written_until_a_test_run_turns_it_on(tmp_path):
    store = AsrCaptureStore(tmp_path / "captures")

    assert store.enabled is False
    assert store.record(wav=b"RIFF....", result=_Result()) is None
    assert list((tmp_path / "captures").glob("*")) == []


def test_recording_keeps_the_raw_upload_next_to_its_transcript(tmp_path):
    store = AsrCaptureStore(tmp_path / "captures")
    store.set_enabled(True)
    wav = make_wav()

    record = store.record(wav=wav, result=_Result("世界"), source="call")

    assert record is not None
    assert record.text == "世界"
    assert record.source == "call"
    assert record.bytes == len(wav)
    # The stored bytes are the upload itself, not a re-encode: comparing a bad
    # transcript against the audio that caused it is the whole point.
    assert (tmp_path / "captures" / f"{record.id}.wav").read_bytes() == wav

    items = store.list()
    assert [item["id"] for item in items] == [record.id]
    assert items[0]["vad"] is None


def test_list_reads_newest_first_and_skips_records_whose_audio_is_gone(tmp_path):
    store = AsrCaptureStore(tmp_path / "captures")
    store.set_enabled(True)
    first = store.record(wav=make_wav(), result=_Result("一"))
    second = store.record(wav=make_wav(), result=_Result("二"))

    assert [item["text"] for item in store.list()] == ["二", "一"]

    (tmp_path / "captures" / f"{second.id}.wav").unlink()
    assert [item["text"] for item in store.list()] == ["一"]


@pytest.mark.parametrize("item_id", ["", "../index", "a/b", ".hidden", "nope"])
def test_audio_lookup_refuses_anything_that_is_not_a_plain_record_id(tmp_path, item_id):
    store = AsrCaptureStore(tmp_path / "captures")
    store.set_enabled(True)
    record = store.record(wav=make_wav(), result=_Result())

    assert store.audio_path(item_id) is None
    assert store.audio_path(record.id) is not None


def test_the_oldest_captures_are_dropped_once_the_cap_holds(tmp_path):
    store = AsrCaptureStore(tmp_path / "captures", max_items=2)
    store.set_enabled(True)
    records = [store.record(wav=make_wav(), result=_Result(str(index))) for index in range(3)]

    kept = [item["id"] for item in store.list()]
    assert kept == [records[2].id, records[1].id]
    assert not (tmp_path / "captures" / f"{records[0].id}.wav").exists()
    # The index has to shrink with the files, or a listing keeps reporting audio
    # that is no longer there.
    assert len((tmp_path / "captures" / "index.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 2


def test_clear_removes_every_capture_and_reports_how_many(tmp_path):
    store = AsrCaptureStore(tmp_path / "captures")
    store.set_enabled(True)
    store.record(wav=make_wav(), result=_Result())

    assert store.clear() == 1
    assert store.list() == []
    assert list((tmp_path / "captures").glob("*.wav")) == []


def test_a_corrupt_index_line_does_not_hide_the_records_around_it(tmp_path):
    store = AsrCaptureStore(tmp_path / "captures")
    store.set_enabled(True)
    record = store.record(wav=make_wav(), result=_Result())
    with (tmp_path / "captures" / "index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"id": "half-writ')

    assert [item["id"] for item in store.list()] == [record.id]


def test_the_transcribe_route_records_only_while_the_test_mode_is_on(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from character_memory.media_server import create_media_app

    monkeypatch.setenv("CHARACTER_MEDIA_ASR_CAPTURE_DIR", str(tmp_path / "captures"))
    asr = _FakeAsr()
    client = TestClient(create_media_app(MediaRuntime(asr, _UnusedTts())))

    off = client.get("/v1/dev/asr-capture").json()
    assert off["enabled"] is False

    wav = make_wav()
    assert client.post("/v1/asr", content=wav, headers={"Content-Type": "audio/wav"}).status_code == 200
    assert client.get("/v1/dev/asr-capture").json()["items"] == []
    assert not (tmp_path / "captures").exists()

    assert client.post("/v1/dev/asr-capture/test-mode", json={"enabled": True}).json()["enabled"] is True
    response = client.post(
        "/v1/asr",
        content=wav,
        headers={"Content-Type": "audio/wav", "X-ASR-Source": "call"},
    )
    assert response.status_code == 200

    items = client.get("/v1/dev/asr-capture").json()["items"]
    assert len(items) == 1
    assert items[0]["source"] == "call"
    assert items[0]["text"] == "测试语音"
    assert items[0]["audio_ms"] == pytest.approx(200.0, abs=1.0)

    served = client.get(f"/v1/dev/asr-capture/{items[0]['id']}/audio")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("audio/wav")
    assert served.content == wav
    assert client.get("/v1/dev/asr-capture/missing/audio").status_code == 404


def test_a_capture_with_no_source_header_is_still_recorded(tmp_path, monkeypatch):
    """The browser sends no label today; that must not cost us the capture."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from character_memory.media_server import create_media_app

    monkeypatch.setenv("CHARACTER_MEDIA_ASR_CAPTURE_DIR", str(tmp_path / "captures"))
    client = TestClient(create_media_app(MediaRuntime(_FakeAsr(), _UnusedTts())))
    client.post("/v1/dev/asr-capture/test-mode", json={"enabled": True})
    client.post("/v1/asr", content=make_wav(), headers={"Content-Type": "audio/wav"})

    items = client.get("/v1/dev/asr-capture").json()["items"]
    assert [item["source"] for item in items] == ["unknown"]


def test_clearing_is_a_separate_intent_from_switching_the_recording_off(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from character_memory.media_server import create_media_app

    monkeypatch.setenv("CHARACTER_MEDIA_ASR_CAPTURE_DIR", str(tmp_path / "captures"))
    client = TestClient(create_media_app(MediaRuntime(_FakeAsr(), _UnusedTts())))
    client.post("/v1/dev/asr-capture/test-mode", json={"enabled": True})
    client.post("/v1/asr", content=make_wav(), headers={"Content-Type": "audio/wav"})

    payload = client.post("/v1/dev/asr-capture/test-mode", json={"enabled": False, "clear": True}).json()
    assert payload == {"enabled": False, "cleared": 1}

    # Turning it off must not have emptied anything on its own, and it must be off
    # now: a second upload lands nowhere.
    client.post("/v1/dev/asr-capture/test-mode", json={"enabled": True})
    client.post("/v1/asr", content=make_wav(), headers={"Content-Type": "audio/wav"})
    assert len(client.get("/v1/dev/asr-capture").json()["items"]) == 1

    index = tmp_path / "captures" / "index.jsonl"
    assert json.loads(index.read_text(encoding="utf-8").strip())["source"] == "unknown"
