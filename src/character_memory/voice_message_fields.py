"""Canonical metadata keys for a persisted VOICE_MESSAGE.

Three sites read these keys: the direct history payload, the group history
payload, and the state-transition service. They share this one definition so a
rename cannot drift between them -- a drifted key fails silently, dropping the
audio reference on reload instead of raising.
"""

from __future__ import annotations

from typing import Any

VOICE_STATUS = "voice_status"
VOICE_MEDIA_ID = "voice_media_id"
VOICE_DURATION_MS = "voice_duration_ms"
VOICE_ERROR = "voice_error"


def voice_pending_fields() -> dict[str, Any]:
    """The metadata written when a voice message is first persisted.

    The message is written before any synthesis is attempted, so a synthesis
    failure still leaves the text in the transcript.
    """
    return {
        VOICE_STATUS: "pending",
        VOICE_MEDIA_ID: None,
        VOICE_DURATION_MS: None,
        VOICE_ERROR: None,
    }


def voice_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    """The four keys exposed on a history payload; None for non-voice messages."""
    return {
        VOICE_STATUS: metadata.get(VOICE_STATUS),
        VOICE_MEDIA_ID: metadata.get(VOICE_MEDIA_ID),
        VOICE_DURATION_MS: metadata.get(VOICE_DURATION_MS),
        VOICE_ERROR: metadata.get(VOICE_ERROR),
    }
