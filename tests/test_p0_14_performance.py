from __future__ import annotations

from datetime import datetime, timezone
import threading
import time

from character_memory.llm.client import OpenAICompatibleModel
from character_memory.runtime.person_runtime import PersonRuntime


def test_shared_provider_allows_overlapping_character_calls(monkeypatch):
    model = OpenAICompatibleModel("test-key", attempts=1)
    gate = threading.Barrier(2)
    guard = threading.Lock()
    active = 0
    max_active = 0
    results = []

    def fake_request(messages, *, conversation_id=None, json_object=False, model=None):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        gate.wait(timeout=2)
        time.sleep(0.03)
        with guard:
            active -= 1
        return '{"perception":"","reaction":"","mental_state_update":"","actions":[],"memory_candidates":[],"intent_candidates":[]}'

    monkeypatch.setattr(model, "_request", fake_request)

    def run(session_id):
        results.append(model.react_call_for_session("hello", session_id))

    left = threading.Thread(target=run, args=("character-a",))
    right = threading.Thread(target=run, args=("character-b",))
    left.start()
    right.start()
    left.join(timeout=3)
    right.join(timeout=3)
    model.close()

    assert not left.is_alive()
    assert not right.is_alive()
    assert len(results) == 2
    assert max_active == 2
    assert all(result.trace.model == "deepseek-flash" for result in results)


def test_empty_memory_candidates_do_not_scan_long_term_memory():
    class Store:
        def list_memories(self, character_id):
            raise AssertionError("empty admission must not scan all memories")

    runtime = PersonRuntime(
        Store(),
        recall=None,
        embeddings=None,
        model=None,
        persona="test",
    )

    accepted, decisions = runtime._prepare_memory_writes(
        "rin",
        datetime.now(timezone.utc),
        [],
    )

    assert accepted == []
    assert decisions == []
