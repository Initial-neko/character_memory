from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from character_memory.domain.models import PersonReaction
from character_memory.llm.client import OpenAICompatibleModel
from character_memory.llm.usage import (
    LlmUsageRecorder,
    LlmUsageStore,
    infer_usage_context,
    llm_usage_scope,
    provider_label,
)


def test_usage_store_aggregates_requests_logical_calls_retries_and_features(tmp_path):
    store = LlmUsageStore(tmp_path / "usage.db")
    try:
        common = {
            "provider": "example.test",
            "model": "demo-model",
            "feature": "GROUP",
            "purpose": "GROUP_REACTION",
            "session_id": "session-1",
            "conversation_id": "group:lab",
            "character_id": "kurisu",
            "logical_call_id": "logical-1",
            "status": "SUCCESS",
            "input_chars": 100,
            "output_chars": 20,
            "duration_ms": 50,
            "usage_source": "PROVIDER",
        }
        store.add(
            {
                **common,
                "attempt": 1,
                "input_tokens": 40,
                "output_tokens": 5,
                "total_tokens": 45,
            }
        )
        store.add(
            {
                **common,
                "attempt": 2,
                "input_tokens": 50,
                "output_tokens": 6,
                "total_tokens": 56,
            }
        )
        store.add(
            {
                **common,
                "feature": "SPACE",
                "purpose": "SPACE_REPLY",
                "logical_call_id": "logical-2",
                "conversation_id": "space:1:thread:2",
                "attempt": 1,
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "usage_source": "UNAVAILABLE",
            }
        )

        usage = store.usage(hours=24, limit=20)
    finally:
        store.close()

    summary = usage["summary"]
    assert summary["requests"] == 3
    assert summary["logical_calls"] == 2
    assert summary["retried_logical_calls"] == 1
    assert summary["input_tokens"] == 90
    assert summary["output_tokens"] == 11
    assert summary["total_tokens"] == 101
    assert summary["token_known_requests"] == 2
    assert summary["token_coverage"] == 0.6667
    grouped = {(row["feature"], row["purpose"]): row for row in usage["by_feature"]}
    assert grouped[("GROUP", "GROUP_REACTION")]["requests"] == 2
    assert grouped[("GROUP", "GROUP_REACTION")]["total_tokens"] == 101
    assert grouped[("SPACE", "SPACE_REPLY")]["token_known_requests"] == 0


def test_openai_compatible_request_records_exact_provider_usage_and_attribution(tmp_path):
    usage_path = tmp_path / "provider-usage.db"

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            request=request,
            headers={"x-request-id": "req-usage-1"},
            json={
                "choices": [{"message": {"content": "pong"}}],
                "usage": {
                    "prompt_tokens": 123,
                    "completion_tokens": 7,
                    "total_tokens": 130,
                },
            },
        )

    model = OpenAICompatibleModel(
        "key",
        model="demo-model",
        base_url="https://example.test/v1",
        usage_recorder=LlmUsageRecorder(usage_path),
    )
    model.client.close()
    model.client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)
    try:
        with llm_usage_scope(
            feature="GROUP",
            purpose="GROUP_REACTION",
            character_id="kurisu",
            conversation_id="group:lab",
            override=True,
        ):
            assert model._request(
                [{"role": "user", "content": "ping"}],
                conversation_id="group:lab",
                usage_logical_call_id="logical-group-1",
            ) == "pong"
    finally:
        model.close()

    store = LlmUsageStore(usage_path)
    try:
        usage = store.usage(hours=24, limit=10)
    finally:
        store.close()

    assert usage["summary"]["requests"] == 1
    assert usage["summary"]["total_tokens"] == 130
    row = usage["recent"][0]
    assert row["feature"] == "GROUP"
    assert row["purpose"] == "GROUP_REACTION"
    assert row["character_id"] == "kurisu"
    assert row["conversation_id"] == "group:lab"
    assert row["logical_call_id"] == "logical-group-1"
    assert row["input_tokens"] == 123
    assert row["output_tokens"] == 7
    assert row["usage_source"] == "PROVIDER"
    assert row["request_id"] == "req-usage-1"


def test_structured_retry_is_two_requests_under_one_logical_call(tmp_path):
    usage_path = tmp_path / "retry-usage.db"
    responses = iter(
        [
            {
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
            {
                "choices": [{"message": {"content": json.dumps({"actions": []})}}],
                "usage": {"prompt_tokens": 14, "completion_tokens": 3, "total_tokens": 17},
            },
        ]
    )

    def handler(request: httpx.Request):
        return httpx.Response(200, request=request, json=next(responses))

    model = OpenAICompatibleModel(
        "key",
        model="demo-model",
        base_url="https://example.test/v1",
        attempts=2,
        usage_recorder=LlmUsageRecorder(usage_path),
    )
    model.client.close()
    model.client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)
    try:
        with llm_usage_scope(
            feature="DIRECT",
            purpose="DIRECT_REACTION",
            character_id="rin",
            conversation_id="rin:web",
            override=True,
        ):
            result = model.structured_call_for_session(
                "context",
                PersonReaction,
                "rin:web",
            )
    finally:
        model.close()

    assert result.trace.attempt == 2
    assert result.trace.logical_call_id

    store = LlmUsageStore(usage_path)
    try:
        usage = store.usage(hours=24, limit=10)
    finally:
        store.close()

    assert usage["summary"]["requests"] == 2
    assert usage["summary"]["logical_calls"] == 1
    assert usage["summary"]["retried_logical_calls"] == 1
    assert usage["summary"]["total_tokens"] == 29
    rows = list(reversed(usage["recent"]))
    assert [row["attempt"] for row in rows] == [1, 2]
    assert {row["logical_call_id"] for row in rows} == {result.trace.logical_call_id}


def test_missing_provider_usage_is_explicitly_unknown_not_estimated(tmp_path):
    usage_path = tmp_path / "unknown-usage.db"

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    model = OpenAICompatibleModel(
        "key",
        model="demo-model",
        base_url="https://example.test/v1",
        usage_recorder=LlmUsageRecorder(usage_path),
    )
    model.client.close()
    model.client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)
    try:
        model._request([{"role": "user", "content": "hello"}], conversation_id="dev-console")
    finally:
        model.close()

    store = LlmUsageStore(usage_path)
    try:
        usage = store.usage(hours=24, limit=10)
    finally:
        store.close()

    row = usage["recent"][0]
    assert row["feature"] == "DEV"
    assert row["purpose"] == "DEV_LLM_PROBE"
    assert row["total_tokens"] is None
    assert row["usage_source"] == "UNAVAILABLE"
    assert row["input_chars"] > 0
    assert usage["summary"]["token_known_requests"] == 0
    assert usage["summary"]["token_coverage"] == 0.0


def test_success_http_with_malformed_payload_is_still_metered(tmp_path):
    usage_path = tmp_path / "malformed-usage.db"

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            request=request,
            headers={"x-request-id": "req-malformed-1"},
            json={
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 1,
                    "total_tokens": 9,
                }
            },
        )

    model = OpenAICompatibleModel(
        "key",
        model="demo-model",
        base_url="https://example.test/v1",
        usage_recorder=LlmUsageRecorder(usage_path),
    )
    model.client.close()
    model.client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)
    try:
        with pytest.raises((KeyError, IndexError, TypeError)):
            model._request(
                [{"role": "user", "content": "hello"}],
                conversation_id="dev-console",
                usage_logical_call_id="logical-malformed-1",
            )
    finally:
        model.close()

    store = LlmUsageStore(usage_path)
    try:
        usage = store.usage(hours=24, limit=10)
    finally:
        store.close()

    assert usage["summary"]["requests"] == 1
    assert usage["summary"]["logical_calls"] == 1
    assert usage["summary"]["errors"] == 1
    assert usage["summary"]["total_tokens"] == 9
    row = usage["recent"][0]
    assert row["status"] == "ERROR"
    assert row["logical_call_id"] == "logical-malformed-1"
    assert row["error_type"].startswith("RESPONSE_")
    assert row["request_id"] == "req-malformed-1"


def test_provider_label_distinguishes_local_openai_compatible_ports():
    assert provider_label("http://127.0.0.1:1234/v1") == "127.0.0.1:1234"
    assert provider_label("http://127.0.0.1:9010/v1") == "127.0.0.1:9010"
    assert provider_label("https://api.openai.com/v1") == "api.openai.com"


def test_usage_context_inference_covers_non_runtime_feature_sessions():
    cases = {
        "ensemble-research:2026-09-23T11:00": ("ENSEMBLE", "ENSEMBLE_RESEARCH"),
        "space-opportunity:rin:2026-09-23T11:00:daily": ("SPACE", "SPACE_POST_PLAN"),
        "space-world-explore:rin:2026-09-23T11:00:daily": ("SPACE", "SPACE_WORLD_EXPLORE"),
        "avatar-intent:rin": ("AVATAR", "AVATAR_SEARCH_INTENT"),
        "visual-plan:rin:selfie": ("VISUAL", "VISUAL_PROMPT"),
        "sticker-tag:global:happy.png": ("STICKER", "STICKER_AUTO_TAG"),
        "encounter-chat:7": ("ENCOUNTER", "ENCOUNTER_CHAT"),
        "dev-console-vision": ("DEV", "DEV_VISION_PROBE"),
        # World Activity sets an explicit scope; these prefixes keep a call that
        # misses the scope out of the anonymous OTHER bucket.
        "world-pulse:2026-09-24T20": ("WORLD", "WORLD_PULSE_SUMMARY"),
        "world-pulse-comment:37:rei": ("WORLD", "WORLD_PULSE_TAKE"),
        "personal-browse-plan:rei:2026-09-24T19:54": ("WORLD", "WORLD_BROWSE_PLAN"),
        "personal-browse-appraise:rei:2026-09-24T19:54": ("WORLD", "WORLD_BROWSE_APPRAISAL"),
    }
    for session_id, expected in cases.items():
        context = infer_usage_context(session_id)
        assert (context.feature, context.purpose) == expected


def _usage_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "provider": "example.test",
        "model": "demo-model",
        "feature": "GROUP",
        "purpose": "GROUP_REACTION",
        "session_id": "session-lock",
        "conversation_id": "group:lab",
        "character_id": "kurisu",
        "logical_call_id": "logical-lock",
        "attempt": 1,
        "status": "SUCCESS",
        "input_chars": 10,
        "output_chars": 5,
        "duration_ms": 12.0,
        "usage_source": "PROVIDER",
    }
    row.update(overrides)
    return row


def _hold_write_lock(db_path) -> sqlite3.Connection:
    """Take the same lock a concurrent writer (dev server, other process) takes."""

    blocker = sqlite3.connect(str(db_path), isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    return blocker


def test_record_gives_up_instead_of_waiting_when_the_database_is_locked(tmp_path, caplog):
    """Telemetry sits on the user's chat path: a busy database must cost it ~0."""

    db_path = tmp_path / "usage.db"
    recorder = LlmUsageRecorder(db_path)
    blocker = _hold_write_lock(db_path)
    try:
        started = time.perf_counter()
        with caplog.at_level(logging.WARNING, logger="character_memory.llm.usage"):
            row_id = recorder.record(_usage_row())
        elapsed = time.perf_counter() - started
    finally:
        blocker.rollback()
        blocker.close()
        recorder.close()

    assert row_id is None
    assert elapsed < 1.0, f"record() waited {elapsed:.2f}s behind a locked database"

    dropped = [item for item in caplog.records if item.levelno >= logging.WARNING]
    assert dropped, "a dropped telemetry row must leave a warning behind"
    assert dropped[-1].name == "character_memory.llm.usage"
    assert dropped[-1].exc_info is not None


def test_store_construction_gives_up_instead_of_waiting_when_the_database_is_locked(tmp_path):
    """The recorder is built during app startup; it must not hang there either."""

    db_path = tmp_path / "usage.db"
    sqlite3.connect(str(db_path)).close()
    blocker = _hold_write_lock(db_path)
    try:
        started = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError):
            LlmUsageStore(db_path)
        elapsed = time.perf_counter() - started
    finally:
        blocker.rollback()
        blocker.close()

    assert elapsed < 1.0, f"LlmUsageStore() waited {elapsed:.2f}s behind a locked database"


def test_record_still_writes_a_usable_row_when_the_database_is_free(tmp_path):
    db_path = tmp_path / "usage.db"
    recorder = LlmUsageRecorder(db_path)
    try:
        row_id = recorder.record(_usage_row(logical_call_id="logical-free"))
    finally:
        recorder.close()

    assert row_id is not None
    assert row_id > 0

    store = LlmUsageStore(db_path)
    try:
        usage = store.usage(hours=24, limit=10)
    finally:
        store.close()

    assert usage["summary"]["requests"] == 1
    assert usage["summary"]["logical_calls"] == 1
    row = usage["recent"][0]
    assert row["logical_call_id"] == "logical-free"
    assert (row["feature"], row["purpose"]) == ("GROUP", "GROUP_REACTION")
    assert row["character_id"] == "kurisu"
    assert row["status"] == "SUCCESS"


def test_world_pulse_comment_prefix_is_matched_before_world_pulse():
    """`world-pulse-comment:` starts with `world-pulse:`; order decides."""
    comment = infer_usage_context("world-pulse-comment:37:rei")
    summary = infer_usage_context("world-pulse:2026-09-24T20")

    assert comment.purpose == "WORLD_PULSE_TAKE"
    assert comment.character_id == "rei"
    assert summary.purpose == "WORLD_PULSE_SUMMARY"


def test_every_world_activity_session_prefix_stays_out_of_other():
    """The inference table is the fallback behind the explicit scope.

    A prefix this module emits must be attributable on its own, so a future call
    site that forgets `llm_usage_scope` still cannot land in the OTHER bucket.
    """
    source = (Path(__file__).resolve().parents[1] / "src" / "character_memory" / "world_activity.py").read_text(encoding="utf-8")
    prefixes = set(re.findall(r'f"((?:world-pulse|personal-browse)[a-z-]*):', source))

    assert prefixes, "no World Activity session prefixes found; did the naming change?"
    for prefix in sorted(prefixes):
        context = infer_usage_context(f"{prefix}:x")
        assert context.feature == "WORLD", prefix
        assert context.purpose != "OTHER", prefix
