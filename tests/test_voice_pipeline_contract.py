from __future__ import annotations

import inspect
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
VOICE_JS = ROOT / "src" / "character_memory" / "web" / "voice.js"


def test_voice_pipeline_prefetches_next_tts_without_parallel_playback():
    source = VOICE_JS.read_text(encoding="utf-8")

    assert 'ttsTail: Promise.resolve()' in source
    assert 'function scheduleSynthesis(item)' in source
    assert 'voice.ttsTail.then(run, run)' in source
    assert 'function prefetchNext()' in source
    assert 'if (voice.playing) prefetchNext();' in source
    assert 'await playAudio(url);' in source
    assert 'voice.currentAudio = audio;' in source


def test_voice_pipeline_keeps_capture_independent_from_playback_phase():
    source = VOICE_JS.read_text(encoding="utf-8")

    assert 'capturePhase: "idle"' in source
    assert 'pendingTurns: []' in source
    assert '!["listening", "recording"].includes(voice.capturePhase)' in source
    assert 'setCapturePhase("listening");' in source
    # What "the reply is still running" means, and the one place that decides it. The
    # behaviour is pinned by test_voice_v0_web.py; this pins that the predicate still
    # covers the window where the model is thinking but nothing is playing yet.
    assert 'function replyInFlight()' in source
    assert 'return voice.recoveryPending || voice.playing || voice.queue.length > 0 || voice.phase === "waiting";' in source
    assert 'if (replyInFlight())' in source
    assert '已听到' in source
    assert 'async function flushPendingTurns()' in source
    assert 'echoCancellation:true' in source
    assert 'noiseSuppression:true' in source
    assert 'autoGainControl:true' in source


def test_media_asr_route_is_sync_so_blocking_inference_uses_threadpool():
    pytest.importorskip("fastapi")
    from character_memory.media_server import create_media_app

    class StubRuntime:
        pass

    app = create_media_app(runtime=StubRuntime())
    route = next(route for route in app.routes if getattr(route, "path", None) == "/v1/asr")

    assert not inspect.iscoroutinefunction(route.endpoint)
