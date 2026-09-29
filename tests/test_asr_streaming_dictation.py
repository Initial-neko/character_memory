from pathlib import Path


def test_dictation_prefers_streaming_asr_but_keeps_batch_fallback():
    source = Path("src/character_memory/web/dictation.js").read_text(encoding="utf-8")

    assert "health?.asr?.streaming" in source
    assert "window.StreamingAsr?.createSession" in source
    assert 'source: "dictation"' in source
    assert "await session.pauseInput();" in source
    assert 'await session.flush("user_stop");' in source

    # Older/non-streaming ASR configurations remain usable during migration.
    assert "createScriptProcessor(2048, 1, 1)" in source
    assert "/v1/asr" in source
    assert '"X-ASR-Source":"dictation"' in source

    # Tail preservation is contractual: stop input -> drain posted Worklet
    # messages -> flush server -> close the session.
    pause = source.index("await session.pauseInput();")
    flush = source.index('await session.flush("user_stop");')
    close = source.index("session.close()", flush)
    assert pause < flush < close


def test_stream_client_waits_for_server_flush_ack():
    source = Path("src/character_memory/web/asr_stream.js").read_text(encoding="utf-8")

    assert 'data.kind === "flushed"' in source
    assert "settleFlush(null, data)" in source
    assert 'new Error("ASR flush timed out")' in source
    assert "pauseInput" in source
