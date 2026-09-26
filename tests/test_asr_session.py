import numpy as np
import pytest

from character_memory.asr_session import (
    AsrSession,
    AsrSessionState,
    EndpointReason,
    TranscriptKind,
    TranscriptReconciler,
)


class FakeResult:
    text = "你好，这是完整结果"
    provider = "fake-asr"
    inference_ms = 12.5


class FakeProvider:
    def __init__(self):
        self.calls = []

    def transcribe(self, samples, sample_rate):
        self.calls.append((samples.copy(), sample_rate))
        return FakeResult()


def test_session_preserves_chunk_order_and_finalizes_once():
    provider = FakeProvider()
    session = AsrSession(provider, source="dictation", sample_rate=16000)
    session.start()

    assert session.push_audio(np.ones(1600), sequence=0) is None
    assert session.push_audio(np.ones(800), sequence=1) is None

    event = session.flush_segment(EndpointReason.USER_STOP)

    assert event.kind is TranscriptKind.FINAL
    assert event.segment_id == 1
    assert event.revision == 1
    assert event.text == "你好，这是完整结果"
    assert len(provider.calls) == 1
    assert provider.calls[0][0].size == 2400
    assert provider.calls[0][1] == 16000
    assert session.active_segment_id() is None
    assert session.state is AsrSessionState.ACTIVE


def test_session_rejects_duplicate_or_reordered_audio():
    session = AsrSession(FakeProvider(), source="call")
    session.start()
    session.push_audio(np.ones(100), sequence=4)

    with pytest.raises(ValueError):
        session.push_audio(np.ones(100), sequence=4)

    with pytest.raises(ValueError):
        session.push_audio(np.ones(100), sequence=3)


def test_forced_duration_is_watchdog_signal_not_automatic_flush():
    session = AsrSession(
        FakeProvider(),
        source="call",
        sample_rate=1000,
        max_segment_ms=1000,
    )
    session.start()

    reason = session.push_audio(np.ones(1000), sequence=0)

    assert reason == EndpointReason.FORCED_MAX_DURATION.value
    assert session.active_segment_id() == 1
    assert session.state is AsrSessionState.ACTIVE


def test_reconciler_replaces_partial_and_final_is_idempotent():
    reconciler = TranscriptReconciler("session", 7)

    partial = reconciler.apply(
        kind=TranscriptKind.PARTIAL,
        text="我觉得这个角色",
        start_ms=0,
        end_ms=500,
    )
    final = reconciler.apply(
        kind=TranscriptKind.FINAL,
        text="我觉得这个角色现在已经稳定了",
        start_ms=0,
        end_ms=1200,
        provider="fake-asr",
    )
    repeated = reconciler.apply(
        kind=TranscriptKind.FINAL,
        text="different text must not replace terminal result",
        start_ms=0,
        end_ms=1200,
    )

    assert partial.revision == 1
    assert final.revision == 2
    assert repeated.revision == 2
    assert repeated.text == final.text
    assert reconciler.final is True

    with pytest.raises(ValueError):
        reconciler.apply(
            kind=TranscriptKind.PARTIAL,
            text="late partial",
            start_ms=0,
            end_ms=1300,
        )


def test_cancel_discards_unsubmitted_audio():
    provider = FakeProvider()
    session = AsrSession(provider, source="call")
    session.start()
    session.push_audio(np.ones(200), sequence=0)

    session.cancel()

    assert session.state is AsrSessionState.CANCELLED
    assert session.active_segment_id() is None
    assert provider.calls == []
