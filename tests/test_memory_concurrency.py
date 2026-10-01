from __future__ import annotations

from datetime import datetime, timezone
import threading

from character_memory.domain.models import (
    DailyLifePlan,
    DiaryResult,
    Event,
    EventType,
    MemoryCandidate,
    PersonReaction,
)
from character_memory.llm.client import ModelCallResult, ModelCallTrace, PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class ConcurrentMemoryModel(PersonModel):
    def react(self, context):
        return self.react_call_for_session(context, "test").value

    def react_call_for_session(self, context, session_id):
        return ModelCallResult(
            value=PersonReaction(
                actions=[],
                memory_candidates=[
                    MemoryCandidate(
                        content="用户下周三第一次做项目汇报",
                        memory_type="USER",
                        importance=0.8,
                    )
                ],
            ),
            trace=ModelCallTrace(
                request_messages=[{"role": "user", "content": context}],
                response_text='{"actions":[]}',
                attempt=1,
                model="concurrent-memory-test",
            ),
        )

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


class CandidateBarrierEmbedding(DeterministicEmbedding):
    def __init__(self, barrier):
        super().__init__()
        self.barrier = barrier

    def embed(self, text):
        if text == "用户下周三第一次做项目汇报":
            self.barrier.wait(timeout=5)
        return super().embed(text)


def test_concurrent_channels_recheck_memory_duplicate_at_commit(tmp_path):
    store = SQLiteStore(tmp_path / "memory-concurrency.db")
    embeddings = CandidateBarrierEmbedding(threading.Barrier(2))
    runtime = PersonRuntime(
        store,
        VectorRecall(store, embeddings),
        embeddings,
        ConcurrentMemoryModel(),
        "persona",
    )
    now = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    results = []
    errors = []

    def run(channel):
        try:
            results.append(
                runtime.handle(
                    Event(
                        character_id="rin",
                        event_type=(
                            EventType.USER_MESSAGE
                            if channel == "DIRECT"
                            else EventType.SPACE_COMMENT_RECEIVED
                        ),
                        event_time=now,
                        content="下周三是第一次项目汇报",
                        metadata={
                            "channel": channel,
                            "conversation_id": f"{channel.lower()}:test",
                        },
                    )
                )
            )
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(channel,)) for channel in ("DIRECT", "SPACE")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    memories = store.list_memories("rin")
    assert [item.content for item in memories] == ["用户下周三第一次做项目汇报"]
    assert sorted(len(item.created_memory_ids) for item in results) == [0, 1]
    decisions = [
        store.get_runtime_trace(int(item.event.id))["memory_decisions"][0]["decision"]
        for item in results
    ]
    assert sorted(decisions) == ["SKIP_DUPLICATE_AT_COMMIT", "WRITE"]
    store.close()
