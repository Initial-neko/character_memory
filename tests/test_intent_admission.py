"""Server-side Intent admission: the delay floor, the pending ceiling, dedup.

The model is not a trusted source for any of these. It was never told that
``earliest_hours`` exists, so every candidate used to arrive with the field's
default of 0 -- "due on the next poll". The floor is therefore enforced on the
write path, not requested in the prompt.
"""

from datetime import datetime, timedelta, timezone

import pytest

from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.domain.models import (
    ActionType,
    DailyLifePlan,
    DiaryResult,
    IntentCandidate,
    PersonReaction,
)
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding, EmbeddingProvider
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import IntentPolicy, PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class CandidateModel(PersonModel):
    """Returns whatever Intent candidates the test hands it, and no actions."""

    def __init__(self, candidates):
        self.candidates = list(candidates)

    def react(self, context):
        return PersonReaction(actions=[], intent_candidates=list(self.candidates))

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


class FlatEmbedding(EmbeddingProvider):
    """One vector for every text, so the *threshold logic* is what gets tested.

    DeterministicEmbedding is a hash, not a semantic model; using it here would
    assert coincidence rather than the admission rule.
    """

    def __init__(self, vector=None):
        self.vector = list(vector or [1.0, 0.0, 0.0, 0.0])

    def embed(self, text):
        return list(self.vector)


class BrokenEmbedding(EmbeddingProvider):
    """Fails for the Intent text only, so the recall path still works."""

    def __init__(self, fail_on: str):
        self.fail_on = fail_on
        self.working = FlatEmbedding()

    def embed(self, text):
        if text == self.fail_on:
            raise RuntimeError("embedding provider down")
        return self.working.embed(text)


def _send(store, model, at, *, embeddings=None, message="帮我记着明天看叶子", **policy):
    embeddings = embeddings or DeterministicEmbedding()
    runtime = PersonRuntime(
        store,
        VectorRecall(store, embeddings),
        embeddings,
        model,
        "persona",
        intent_policy=IntentPolicy(**policy),
    )
    service = ChatService(store, {"momo": runtime}, FixedClock(at))
    return service.send(message, character_id="momo", conversation_id="cli", at=at)


def _stored(store, intent_id):
    return next(row for row in store.list_intents("momo") if row["id"] == intent_id)


def _seed_intent(store, at, content, *, embedding=None, earliest_delta=0):
    return store.add_intent(
        "momo",
        content,
        ActionType.PROACTIVE_MESSAGE.value,
        at,
        at + timedelta(hours=earliest_delta),
        at + timedelta(hours=4),
        embedding=embedding,
    )


def test_intent_earliest_is_clamped_to_the_server_floor(tmp_path):
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "a.db")
    model = CandidateModel([IntentCandidate(content="明早看叶子", earliest_hours=0)])

    result = _send(store, model, t0, min_delay_minutes=10)

    assert datetime.fromisoformat(_stored(store, result.created_intent_ids[0])["earliest_at"]) == t0 + timedelta(minutes=10)
    store.close()


def test_requested_delay_above_the_floor_is_not_clamped_down(tmp_path):
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "a.db")
    model = CandidateModel([IntentCandidate(content="明早看叶子", earliest_hours=8)])

    result = _send(store, model, t0, min_delay_minutes=10)

    assert datetime.fromisoformat(_stored(store, result.created_intent_ids[0])["earliest_at"]) == t0 + timedelta(hours=8)
    store.close()


def test_clamp_never_pushes_an_intent_past_its_own_expiry(tmp_path):
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "a.db")
    model = CandidateModel([IntentCandidate(content="马上看一眼", earliest_hours=0, expires_hours=0.05)])

    result = _send(store, model, t0, min_delay_minutes=10)

    row = _stored(store, result.created_intent_ids[0])
    assert datetime.fromisoformat(row["earliest_at"]) == t0 + timedelta(hours=0.05)
    assert row["earliest_at"] == row["expires_at"]
    store.close()


def test_pending_ceiling_drops_further_candidates(tmp_path):
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "a.db")
    _seed_intent(store, t0, "旧意图一")
    _seed_intent(store, t0, "旧意图二")
    model = CandidateModel([IntentCandidate(content="全新的意图")])

    result = _send(store, model, t0, max_pending=2)

    assert result.created_intent_ids == []
    assert store.pending_intent_count("momo") == 2
    trace = store.get_runtime_trace(result.event.id)
    assert [item["decision"] for item in trace["intent_decisions"]] == ["SKIP_PENDING_LIMIT"]
    store.close()


def test_zero_pending_ceiling_means_unlimited(tmp_path):
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "a.db")
    for index in range(3):
        _seed_intent(store, t0, f"旧意图{index}")
    model = CandidateModel([IntentCandidate(content="全新的意图")])

    result = _send(store, model, t0, max_pending=0)

    assert len(result.created_intent_ids) == 1
    store.close()


def test_exact_duplicate_intent_is_dropped(tmp_path):
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "a.db")
    existing = _seed_intent(store, t0, "明早给茉莉叶子拍照发给铃")
    model = CandidateModel([IntentCandidate(content="明早给茉莉叶子拍照发给铃")])

    result = _send(store, model, t0)

    assert result.created_intent_ids == []
    trace = store.get_runtime_trace(result.event.id)
    assert trace["intent_decisions"][0]["duplicate_intent_id"] == existing
    assert trace["intent_decisions"][0]["similarity"] == 1.0
    store.close()


def test_near_duplicate_intent_is_dropped_by_similarity(tmp_path):
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "a.db")
    embeddings = FlatEmbedding()
    existing = _seed_intent(store, t0, "明早给茉莉叶子拍张照", embedding=embeddings.embed("x"))
    # Different wording, same vector -> the similarity branch, not exact match.
    model = CandidateModel([IntentCandidate(content="明早给那盆茉莉拍一张")])

    result = _send(store, model, t0, embeddings=embeddings)

    assert result.created_intent_ids == []
    trace = store.get_runtime_trace(result.event.id)
    assert trace["intent_decisions"][0]["duplicate_intent_id"] == existing
    store.close()


def test_duplicate_lookup_ignores_intents_outside_the_window(tmp_path):
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "a.db")
    _seed_intent(store, t0 - timedelta(hours=100), "明早给茉莉叶子拍照发给铃")
    model = CandidateModel([IntentCandidate(content="明早给茉莉叶子拍照发给铃")])

    result = _send(store, model, t0, dedup_window_hours=72)

    assert len(result.created_intent_ids) == 1
    store.close()


def test_dedup_can_be_disabled(tmp_path):
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "a.db")
    _seed_intent(store, t0, "明早给茉莉叶子拍照发给铃")
    model = CandidateModel([IntentCandidate(content="明早给茉莉叶子拍照发给铃")])

    result = _send(store, model, t0, dedup_enabled=False)

    assert len(result.created_intent_ids) == 1
    store.close()


def test_embedding_failure_does_not_discard_a_valid_intent(tmp_path):
    """Unlike Memory, an Intent is something the person meant to do later."""
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "a.db")
    model = CandidateModel([IntentCandidate(content="明早看叶子")])

    result = _send(store, model, t0, embeddings=BrokenEmbedding(fail_on="明早看叶子"))

    assert len(result.created_intent_ids) == 1
    trace = store.get_runtime_trace(result.event.id)
    decision = trace["intent_decisions"][0]
    assert decision["decision"] == "WRITE"
    assert "embedding provider down" in decision["error"]
    store.close()


def test_trace_records_the_clamp_for_every_candidate(tmp_path):
    t0 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "a.db")
    model = CandidateModel(
        [
            IntentCandidate(content="立刻做", earliest_hours=0),
            IntentCandidate(content="明早做", earliest_hours=12),
        ]
    )

    result = _send(store, model, t0, min_delay_minutes=10)

    trace = store.get_runtime_trace(result.event.id)
    decisions = trace["intent_decisions"]
    assert [item["clamped"] for item in decisions] == [True, False]
    assert decisions[0]["requested_earliest_hours"] == 0
    assert decisions[0]["effective_earliest_hours"] == pytest.approx(10 / 60)
    assert decisions[1]["effective_earliest_hours"] == 12
    store.close()
