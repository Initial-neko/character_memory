"""Architecture baseline: real runtime/storage with strictly local providers."""
from datetime import timedelta
from pathlib import Path
import socket

import httpx
import pytest

from character_memory.domain.models import Event, EventType, PersonReaction
from character_memory.llm.client import ModelCallResult, ModelCallTrace
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.world_activity import PersonalBrowseAppraisal, WorldActivityScheduler, WorldActivityService, WorldPulseRepository
from character_memory.world_observation import WorldObservationService
from test_world_activity import FakeModel, NOW, make_access
from test_world_observation import AllReachableFetcher, FakeSearchProvider


class CountingEmbedding(DeterministicEmbedding):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def embed(self, text):
        self.calls += 1
        return super().embed(text)


class CountingModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.keep = True

    def react_call_for_session(self, context, session_id):
        self.calls.append(("PersonReaction", session_id, context))
        return ModelCallResult(value=PersonReaction(actions=[]), trace=ModelCallTrace(attempt=1, model="baseline-local"))

    def structured_for_session(self, prompt, schema, session_id):
        result = super().structured_for_session(prompt, schema, session_id)
        if schema is PersonalBrowseAppraisal and not self.keep:
            return PersonalBrowseAppraisal(keep=False)
        return result


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    network_attempts = []

    def reject_network(*args, **kwargs):
        network_attempts.append(str(args[:1]))
        raise AssertionError("baseline must not access network")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket, "getaddrinfo", reject_network)
    monkeypatch.setattr(httpx.Client, "send", reject_network)
    store, access, _ = make_access(tmp_path, count=1)
    access.settings.world_pulse_enabled = False
    model = CountingModel()
    embeddings = CountingEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, "id: c00\nname: 本地测试人物")
    bundle = access.require_bundle()
    bundle.model = model
    bundle.runtimes = {"c00": runtime}
    search = FakeSearchProvider()
    fetcher = AllReachableFetcher()
    access.world_observer = WorldObservationService(search, fetcher)
    repo = WorldPulseRepository(store)
    assert Path(access.settings.db_path).parent == tmp_path
    try:
        yield store, access, model, embeddings, repo, search, fetcher, network_attempts
    finally:
        store.close()
    assert network_attempts == []


def test_direct_baseline_one_reaction_and_legal_silence(baseline):
    store, access, model, embedding, *_ = baseline
    result = access.require_bundle().runtimes["c00"].handle(Event(character_id="c00", event_type=EventType.USER_MESSAGE, event_time=NOW, content="先不用回复", metadata={"conversation_id": "baseline-direct"}))
    assert [name for name, _, _ in model.calls] == ["PersonReaction"]
    assert result.reaction.actions == []
    assert result.created_memory_ids == []
    assert embedding.calls == 1  # Existing recall still embeds its query.
    assert not any(event.event_type == EventType.CHARACTER_MESSAGE for event in store.list_events("c00"))
    assert store.get_runtime_trace(result.event.id) is not None


@pytest.mark.parametrize("browse,keep,expected_calls,expected_observations", [(False, True, 1, 0), (True, False, 2, 0), (True, True, 2, 1)])
def test_world_baseline_real_context_search_fetch_appraisal_storage(baseline, browse, keep, expected_calls, expected_observations):
    store, access, model, embedding, repo, search, fetcher, _ = baseline
    model.browse, model.keep = browse, keep
    result = WorldActivityService(access, repo).browse_character("c00", now=NOW)
    assert len(model.calls) == expected_calls
    assert embedding.calls == 1
    assert len(search.queries) == int(browse)
    assert bool(fetcher.urls) == browse
    observations = [event for event in store.list_events("c00") if event.event_type == EventType.WORLD_OBSERVATION]
    assert len(observations) == expected_observations
    assert store.list_memories("c00") == []
    assert not any(event.event_type == EventType.CHARACTER_MESSAGE for event in store.list_events("c00"))
    if observations:
        assert result["source_event_id"] == observations[0].id
        assert observations[0].metadata["sources"]
        assert observations[0].event_time == NOW


def test_world_empty_search_does_not_purchase_appraisal(baseline):
    _, access, model, _, repo, search, fetcher, _ = baseline
    search.search_web = lambda query, **kwargs: []
    result = WorldActivityService(access, repo).browse_character("c00", now=NOW)
    assert [name for name, _, _ in model.calls] == ["PersonalBrowsePlan"]
    assert result["kept"] is False
    assert fetcher.urls == []


@pytest.mark.parametrize("gate", ["idle", "daily_budget"])
def test_world_gate_prevents_all_new_model_and_embedding_work(baseline, gate):
    _, access, model, embedding, repo, search, fetcher, _ = baseline
    access.settings.world_browse_interval_minutes = 30
    access.settings.world_browse_daily_max = 8 if gate == "idle" else 1
    model.browse = gate != "idle"
    scheduler = WorldActivityScheduler(access, repo, poll_seconds=10)
    scheduler.run_once(now=NOW)
    scheduler.force_due("BROWSE", "c00", now=NOW)
    scheduler.run_once(now=NOW)
    before = (len(model.calls), embedding.calls, len(search.queries), len(fetcher.urls))
    again = NOW + timedelta(minutes=31)
    scheduler.force_due("BROWSE", "c00", now=again)
    assert not any(row["kind"] == "BROWSE" for row in scheduler.run_once(now=again))
    assert (len(model.calls), embedding.calls, len(search.queries), len(fetcher.urls)) == before


def test_baseline_rejects_accidental_http_provider(baseline):
    *_, network_attempts = baseline
    with pytest.raises(AssertionError, match="must not access network"):
        httpx.Client().get("https://invalid.example")
    assert len(network_attempts) == 1
    network_attempts.clear()  # This case deliberately exercises the guard.

