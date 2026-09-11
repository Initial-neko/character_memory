import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import character_memory.async_web as async_web
from character_memory.api import create_api
from character_memory.application.async_conversation import ConversationEventHub
from character_memory.async_web import _stream_hub_events, attach_async_routes
from character_memory.group_web import attach_group_routes


ROOT = Path(__file__).resolve().parents[1]


def _create_test_app(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                'api_key: ""',
                'embedding_provider: "deterministic"',
                f'db_path: "{(tmp_path / "sse-route.db").as_posix()}"',
                f'persona_path: "{(ROOT / "personas" / "rin" / "persona.yaml").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )

    async def finite_stream(*args, **kwargs):
        yield "retry: 1500\n\n"

    # Keep the HTTP regression finite. We test cancellation and live delivery
    # separately below; these route tests only need a complete SSE response.
    monkeypatch.setattr(async_web, "_stream_hub_events", finite_stream)

    app = create_api(str(config))
    attach_group_routes(app, str(config))
    attach_async_routes(app)
    return app


def test_sse_route_does_not_expose_request_as_query_parameter(tmp_path, monkeypatch):
    app = _create_test_app(tmp_path, monkeypatch)
    operation = app.openapi()["paths"]["/v1/events/stream"]["get"]
    parameters = {(item["name"], item["in"]) for item in operation.get("parameters", [])}

    assert ("request", "query") not in parameters
    assert ("scope", "query") in parameters
    assert ("conversation_id", "query") in parameters
    assert ("character_id", "query") in parameters
    assert ("Last-Event-ID", "header") in parameters


def test_direct_and_group_sse_routes_return_event_stream_without_request_query(tmp_path, monkeypatch):
    app = _create_test_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        profiles = client.get("/v1/characters").json()["characters"]
        character_id = profiles[0]["id"]

        direct = client.get(
            "/v1/events/stream",
            params={
                "scope": "direct",
                "conversation_id": "sse-direct-test",
                "character_id": character_id,
            },
        )
        assert direct.status_code == 200, direct.text
        assert direct.headers["content-type"].startswith("text/event-stream")
        assert "retry: 1500" in direct.text

        group = client.post(
            "/v1/groups",
            json={
                "name": "SSE测试群",
                "member_ids": [profiles[0]["id"], profiles[1]["id"]],
            },
        ).json()["group"]
        group_stream = client.get(
            "/v1/events/stream",
            params={"scope": "group", "conversation_id": group["id"]},
        )
        assert group_stream.status_code == 200, group_stream.text
        assert group_stream.headers["content-type"].startswith("text/event-stream")
        assert "retry: 1500" in group_stream.text


def test_async_sse_stream_delivers_events_without_blocking_thread_condition():
    async def scenario():
        hub = ConversationEventHub()
        stream = _stream_hub_events(
            hub,
            "direct:rin:test",
            poll_seconds=0.01,
            heartbeat_seconds=60,
        )
        try:
            assert await anext(stream) == "retry: 1500\n\n"
            hub.publish("direct:rin:test", "character_event", {"id": 7, "content": "hello"})
            frame = await asyncio.wait_for(anext(stream), timeout=0.5)
            assert "event: character_event" in frame
            assert '"content":"hello"' in frame
        finally:
            await stream.aclose()
            hub.close()

    asyncio.run(scenario())


def test_async_sse_stream_is_immediately_cancellable_for_server_shutdown():
    async def scenario():
        hub = ConversationEventHub()
        stream = _stream_hub_events(
            hub,
            "direct:rin:shutdown",
            poll_seconds=30,
            heartbeat_seconds=60,
        )
        assert await anext(stream) == "retry: 1500\n\n"

        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        # If this were the old synchronous Condition.wait() generator, cancelling
        # the response task would leave its worker thread blocked. The async loop
        # reaches cancellation directly at asyncio.sleep().
        await stream.aclose()
        hub.close()

    asyncio.run(scenario())
