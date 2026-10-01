from pathlib import Path


def test_voice_call_prefers_streaming_asr_and_keeps_batch_fallback():
    source = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")

    assert "health?.asr?.streaming" in source
    assert "window.StreamingAsr?.createSession" in source
    assert 'source: "call"' in source
    assert "await session.connect();" in source
    assert "await session.attach(context, source);" in source

    # Compatibility remains first-class for older/non-streaming runtimes and
    # becomes the in-place recovery path if a live WebSocket dies.
    assert "createScriptProcessor(2048, 1, 1)" in source
    assert "/v1/asr" in source
    assert '"X-ASR-Source":"call"' in source
    assert "streaming ASR failed; switching to batch capture" in source


def test_streaming_call_final_preserves_interrupt_queue_and_visual_segment_window():
    source = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")

    assert "function handleStreamingPartial(data)" in source
    assert "function handleStreamingFinal(data)" in source
    assert "voice.streamingSegmentId = key;" in source
    assert "voice.streamingSegmentStartedAt = performance.now();" in source
    assert "voice.streamingSegmentId === key && voice.streamingSegmentStartedAt" in source
    assert "fromMs:Math.max(0, startedAt - 1000)" in source

    # A recognized user turn while the character is thinking/speaking must be
    # queued exactly like the old batch path, not dropped or dispatched into an
    # already-running reaction.
    final_start = source.index("async function handleStreamingFinal(data)")
    final_end = source.index("async function attachStreamingAsr", final_start)
    final_body = source[final_start:final_end]
    assert "if (replyInFlight())" in final_body
    assert "voice.pendingTurns.push(turn)" in final_body
    assert "await dispatchRecognizedTurn(turn)" in final_body

    # Streaming does not fabricate a fake zero-latency ASR metric.
    assert "asrMs:null" in final_body


def test_stopping_call_cancels_streaming_session_and_retires_pending_mic_owner():
    source = Path("src/character_memory/web/voice.js").read_text(encoding="utf-8")

    stop_start = source.index("async function stopMicrophone")
    stop_end = source.index("async function startMicrophone", stop_start)
    stop_body = source[stop_start:stop_end]

    assert "micOwnerSeq = claimMicTicket()" in stop_body
    assert "voice.asrSession?.cancel?.()" in stop_body
    assert "voice.streamingAsr = false" in stop_body
    assert "mediaAudio.closeCapture" in stop_body
    assert "stream:voice.stream" in stop_body
    assert "context:voice.audioContext" in stop_body
