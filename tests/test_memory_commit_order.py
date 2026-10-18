"""Offline evidence for source-time-filtered Memory commit deduplication.

Two strict XFAILs describe an unfixed defect. An XPASS requires review and
removal of the marker; it does not silently count as accepted behavior.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import threading

import pytest

from character_memory import app as app_module
from character_memory.domain.models import Event, EventType, MemoryCandidate, PersonReaction
from character_memory.llm.client import ModelCallResult, ModelCallTrace, PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


NOW = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
OLD_TEXT = "用户下周三第一次做项目汇报"
NEAR_TEXT = "用户下周三将首次做项目汇报"


class MissingCommitDuplicate(Exception):
    """Only the fully observed two-WRITE/current-active defect may raise this."""


class UnexpectedProviderFactory(Exception):
    """Environment/provider setup is not the known product defect."""


class CandidateModel(PersonModel):
    def __init__(self, near):
        self.near = near
        self.calls = []

    def react(self, context):
        return self.react_call_for_session(context, "old").value

    def react_call_for_session(self, context, session_id):
        self.calls.append(session_id)
        text = NEAR_TEXT if self.near and session_id == "new" else OLD_TEXT
        return ModelCallResult(
            value=PersonReaction(actions=[], memory_candidates=[MemoryCandidate(content=text, importance=0.8)]),
            trace=ModelCallTrace(request_messages=[{"role": "user", "content": context}],
                                 response_text='{"actions":[]}', attempt=1, model="offline-commit-order-stub"),
        )


class OrderedEmbedding(DeterministicEmbedding):
    def __init__(self, entered, release):
        super().__init__()
        self.entered, self.release = entered, release
        self.candidate_calls = []

    def embed(self, text):
        if text in {OLD_TEXT, NEAR_TEXT}:
            self.candidate_calls.append(text)
            if threading.current_thread().name.startswith("older"):
                self.entered.set()
                assert self.release.wait(10), "test synchronization failed"
            return [1.0, 0.0]  # Deterministic candidate cosine=1; no semantic model claim.
        return [0.0, 1.0]


def _source_identity():
    names = ["character_memory.memory.recall", "character_memory.runtime.person_context",
             "character_memory.runtime.person_runtime", "character_memory.storage.sqlite"]
    root = Path(app_module.__file__).resolve().parents[2]
    rows = []
    for name in names:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        assert path.is_relative_to(root / "src"), f"mixed source roots: {name}"
        rows.append({"module": name, "__file__": str(path), "relative_path": path.relative_to(root).as_posix(),
                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return rows


@pytest.mark.parametrize("near", [False, True], ids=["exact", "near"])
@pytest.mark.parametrize("delta", [
    pytest.param(0, id="same-event-time"),
    pytest.param(1, id="different-event-time"),
])
def test_memory_commit_order_current_active_duplicate(tmp_path, monkeypatch, near, delta):
    factory_calls = []

    def forbidden_factory(*args, **kwargs):
        factory_calls.append("called")
        raise UnexpectedProviderFactory("offline test must not construct a production provider")

    monkeypatch.setattr(app_module, "build_embedding", forbidden_factory)
    monkeypatch.setattr(app_module, "build_model", forbidden_factory)
    sources = _source_identity()
    store = SQLiteStore(tmp_path / "memory-commit-order.db")
    entered, release = threading.Event(), threading.Event()
    embeddings = OrderedEmbedding(entered, release)
    model = CandidateModel(near)
    runtime = PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, "persona")
    old = Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=NOW,
                content="旧 Direct 来源", metadata={"channel": "DIRECT", "conversation_id": "old"})
    new = Event(character_id="rin", event_type=EventType.SPACE_COMMENT_RECEIVED,
                event_time=NOW + timedelta(seconds=delta), content="新 Space 来源",
                metadata={"channel": "SPACE", "conversation_id": "new"})
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="older") as pool:
            older = pool.submit(runtime.handle, old)
            try:
                assert entered.wait(10), "older reaction did not reach candidate embedding"
                newer_result = runtime.handle(new)
            finally:
                release.set()
            older_result = older.result(timeout=10)
        results = [older_result, newer_result]
        rows = store.list_memories("rin")
        at_old = runtime.context_builder.build("rin", query="query", at=NOW).memories
        at_new = runtime.context_builder.build("rin", query="query", at=NOW + timedelta(seconds=1)).memories
        decisions = [store.get_runtime_trace(r.event.id)["memory_decisions"][0] for r in results]
        report = {
            "case": {"near": near, "source_time_delta_seconds": delta},
            "test_module": __name__, "test_file_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "source_identity": sources,
            "source_events": [{"id": r.event.id, "event_time": r.event.event_time.isoformat(),
                               "channel": r.event.metadata["channel"]} for r in results],
            "created_ids_old_new": [r.created_memory_ids for r in results],
            "active_memories": [m.model_dump(mode="json", exclude={"embedding"}) for m in rows],
            "trace_decisions_old_new": decisions,
            "contexts": {"at_old": [m.model_dump(mode="json", exclude={"embedding"}) for m in at_old],
                         "at_new": [m.model_dump(mode="json", exclude={"embedding"}) for m in at_new]},
            "boundaries": {"production_factory_calls": len(factory_calls), "stub_model_calls": len(model.calls),
                           "candidate_embedding_calls": len(embeddings.candidate_calls),
                           "temporary_sqlite": True, "semantic_model_validated": False,
                           "production_runtime_validated": False},
        }
        out = Path(os.environ.get("MEMORY_COMMIT_ORDER_EVIDENCE_DIR", "artifacts/core-visual-evidence/memory-commit-order"))
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{'near' if near else 'exact'}-delta{delta}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        # Apparatus, provenance, context-time and provider errors must remain FAIL,
        # never the expected MissingCommitDuplicate exception.
        assert factory_calls == []
        assert model.calls == ["old", "new"]
        assert len(embeddings.candidate_calls) == 2
        assert [r.event.id for r in results] == [1, 2]
        assert newer_result.created_memory_ids == [1]
        assert all(m.character_id == "rin" and m.active for m in rows)
        assert all(m.event_time <= NOW for m in at_old)
        assert {m.id for m in at_new} == {m.id for m in rows}
        if len(rows) == 2:
            assert delta == 1
            assert older_result.created_memory_ids == [2]
            assert [d["decision"] for d in decisions] == ["WRITE", "WRITE"]
            assert {m.source_event_id for m in rows} == {1, 2}
            assert {m.content for m in rows} == ({OLD_TEXT, NEAR_TEXT} if near else {OLD_TEXT})
            assert [m.id for m in at_old] == [2]
            raise MissingCommitDuplicate("newer active memory is excluded by older source-time commit filter")
        assert len(rows) == 1
        assert older_result.created_memory_ids == []
        assert [d["decision"] for d in decisions] == ["SKIP_DUPLICATE_AT_COMMIT", "WRITE"]
        assert decisions[0]["duplicate_memory_id"] == 1
    finally:
        store.close()
