import base64
from datetime import datetime, timezone
import io
import wave

import pytest

from character_memory.media import MediaStorage


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


def data_url(data=PNG_1X1, mime="image/png"):
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def test_media_storage_sniffs_bytes_and_does_not_trust_claimed_mime(tmp_path):
    storage = MediaStorage(tmp_path / "media")
    asset, normalized = storage.save_data_url(
        character_id="rin",
        original_name="picture.jpeg",
        data_url=data_url(mime="image/jpeg"),
        created_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )

    assert asset.mime_type == "image/png"
    assert asset.storage_name.endswith(".png")
    assert asset.size_bytes == len(PNG_1X1)
    assert storage.asset_path(asset).read_bytes() == PNG_1X1
    assert normalized.startswith("data:image/png;base64,")


def test_media_storage_rejects_non_image_and_oversized_payload(tmp_path):
    storage = MediaStorage(tmp_path / "media", max_bytes=16)
    with pytest.raises(ValueError, match="unsupported media format"):
        storage.save_data_url(
            character_id="rin",
            original_name="bad.png",
            data_url=data_url(b"not-an-image"),
            created_at=datetime.now(timezone.utc),
        )

    with pytest.raises(ValueError, match="exceeds"):
        storage.save_data_url(
            character_id="rin",
            original_name="large.png",
            data_url=data_url(PNG_1X1 + b"x" * 32),
            created_at=datetime.now(timezone.utc),
        )


MP3_ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
MP3_FRAME_SYNC = b"\xff\xfb\x90\x00" + b"\x00" * 64


def wav_bytes() -> bytes:
    """A real RIFF/WAVE payload, so the sniff test cannot pass on a stub."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(32000)
        handle.writeframes(b"\x00\x00" * 320)
    return buf.getvalue()


def test_media_storage_stores_wav_and_reads_it_back(tmp_path):
    storage = MediaStorage(tmp_path / "media")
    payload = wav_bytes()

    asset = storage.save_bytes(
        character_id="momo",
        original_name="clip.wav",
        payload=payload,
        created_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        source="GENERATED_VOICE",
    )

    assert asset.mime_type == "audio/wav"
    assert asset.storage_name.endswith(".wav")
    assert asset.size_bytes == len(payload)
    # The allowlist in asset_path() is what makes stored audio readable at all.
    assert storage.asset_path(asset).read_bytes() == payload


@pytest.mark.parametrize(
    "payload",
    [MP3_ID3, MP3_FRAME_SYNC],
    ids=["id3-tag", "frame-sync"],
)
def test_media_storage_stores_both_mp3_shapes(tmp_path, payload):
    storage = MediaStorage(tmp_path / "media")

    asset = storage.save_bytes(
        character_id="momo",
        original_name="clip.mp3",
        payload=payload,
        created_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        source="GENERATED_VOICE",
    )

    assert asset.mime_type == "audio/mpeg"
    assert asset.storage_name.endswith(".mp3")
    assert storage.asset_path(asset).read_bytes() == payload


def test_media_storage_sniffs_claimed_audio_mime_off_a_wav(tmp_path):
    storage = MediaStorage(tmp_path / "media")

    asset, normalized = storage.save_data_url(
        character_id="momo",
        original_name="clip.mp3",
        data_url=data_url(data=wav_bytes(), mime="audio/mpeg"),
        created_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )

    assert asset.mime_type == "audio/wav"
    assert normalized.startswith("data:audio/wav;base64,")
