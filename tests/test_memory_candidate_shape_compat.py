from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from character_memory.application.clock import FixedClock
from character_memory.application.group_conversation_service import GroupConversationService
from character_memory.domain.models import DiaryResult, PersonReaction
from character_memory.llm.client import OpenAICompatibleModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


def test_person_reaction_normalizes_string_memory_candidate():
    reaction = PersonReaction.model_validate(
        {
            "actions": [],
            "memory_candidates": ["User 为未来研究记录了一张设备照片。"],
        }
    )

    assert len(reaction.memory_candidates) == 1
    candidate = reaction.memory_candidates[0]
    assert candidate.content == "User 为未来研究记录了一张设备照片。"
    assert candidate.memory_type == "EPISODIC"
    assert candidate.importance == 0.5


def test_existing_memory_candidate_object_shape_is_unchanged():
    reaction = PersonReaction.model_validate(
        {
            "actions": [],
            "memory_candidates": [
                {
                    "content": "用户明确说下周继续这个实验。",
                    "memory_type": "SHARED",
                    "importance": 0.84,
                }
            ],
        }
    )

    candidate = reaction.memory_candidates[0]
    assert candidate.content == "用户明确说下周继续这个实验。"
    assert candidate.memory_type == "SHARED"
    assert candidate.importance == 0.84


def test_complex_invalid_memory_candidate_shape_still_fails_validation():
    with pytest.raises(ValidationError):
        PersonReaction.model_validate(
            {
                "actions": [],
                "memory_candidates": [["not", "a", "memory", "object"]],
            }
        )


def test_diary_memory_candidates_share_the_same_string_compatibility():
    result = DiaryResult.model_validate(
        {
            "diary": "今天记录了一件事。",
            "mental_state_update": "",
            "memory_candidates": ["用户希望下次继续讨论。"],
        }
    )

    assert result.memory_candidates[0].content == "用户希望下次继续讨论。"


def test_vision_reaction_string_memory_candidate_parses_without_retry():
    model = OpenAICompatibleModel(
        "key",
        model="text-test",
        vision_model="vision-test",
        attempts=1,
    )
    captured = {}

    def fake_request(messages, **kwargs):
        captured["messages"] = messages
        captured.update(kwargs)
        return json.dumps(
            {
                "actions": [],
                "memory_candidates": ["User 分享了一张设备照片。"],
            },
            ensure_ascii=False,
        )

    model._request = fake_request
    try:
        result = model.react_call_with_images_for_session(
            "请根据图片自然回应",
            ["data:image/png;base64,AAAA"],
            "group:test:rin",
        )
    finally:
        model.close()

    assert result.trace.attempt == 1
    assert result.trace.model == "vision-test"
    assert result.value.memory_candidates[0].content == "User 分享了一张设备照片。"
    assert captured["model"] == "vision-test"
    assert captured["json_object"] is True
    assert isinstance(captured["messages"][1]["content"], list)
    assert any(block.get("type") == "image_url" for block in captured["messages"][1]["content"])


def test_group_vision_reaction_with_string_memory_candidate_does_not_fail(tmp_path):
    store = SQLiteStore(tmp_path / "group-vision-memory-shape.db")
    embeddings = DeterministicEmbedding()
    model = OpenAICompatibleModel(
        "key",
        model="text-test",
        vision_model="vision-test",
        attempts=1,
    )

    def fake_request(messages, **kwargs):
        return json.dumps(
            {
                "perception": "看到用户发来的图片",
                "reaction": "先观察内容",
                "actions": [],
                "memory_candidates": ["User 分享了一张用于后续研究的设备照片。"],
            },
            ensure_ascii=False,
        )

    model._request = fake_request
    recall = VectorRecall(store, embeddings)
    runtimes = {
        "rin": PersonRuntime(store, recall, embeddings, model, "Rin"),
        "momo": PersonRuntime(store, recall, embeddings, model, "Momo"),
    }
    now = datetime(2026, 9, 11, 22, 33, tzinfo=timezone.utc)
    service = GroupConversationService(
        store,
        runtimes,
        FixedClock(now),
        profiles=[{"id":"rin", "name":"Rin"}, {"id":"momo", "name":"Momo"}],
    )

    try:
        group = service.create_group("图片测试群", ["rin", "momo"])
        source = service.persist_user_event(
            group.id,
            "看看这张图",
            image={
                "id":"media-test",
                "original_name":"device.png",
                "mime_type":"image/png",
                "size_bytes":4,
            },
        )
        result = service.react_from_event(
            source,
            image_data_urls=["data:image/png;base64,AAAA"],
        )

        assert len(result["decisions"]) == 2
        assert all(item["actions"] == [] for item in result["decisions"])
        assert [memory.content for memory in store.list_memories("rin")] == [
            "User 分享了一张用于后续研究的设备照片。"
        ]
        assert [memory.content for memory in store.list_memories("momo")] == [
            "User 分享了一张用于后续研究的设备照片。"
        ]
    finally:
        model.close()
        store.close()
