from pathlib import Path

from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.async_web import attach_async_routes
from character_memory.group_web import attach_group_routes


ROOT = Path(__file__).resolve().parents[1]


def _create_test_app(tmp_path):
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
    app = create_api(str(config))
    attach_group_routes(app, str(config))
    attach_async_routes(app)

    # Keep the HTTP regression finite. We are testing FastAPI parameter binding
    # and the SSE response contract here, not the long-lived hub loop itself.
    app.state.character_memory.stream_hub.stream = (
        lambda channel, after_id=0: iter(["retry: 1500\n\n"])
    )
    return app


def test_sse_route_does_not_expose_request_as_query_parameter(tmp_path):
    app = _create_test_app(tmp_path)
    operation = app.openapi()["paths"]["/v1/events/stream"]["get"]
    parameters = {(item["name"], item["in"]) for item in operation.get("parameters", [])}

    assert ("request", "query") not in parameters
    assert ("scope", "query") in parameters
    assert ("conversation_id", "query") in parameters
    assert ("character_id", "query") in parameters
    assert ("Last-Event-ID", "header") in parameters


def test_direct_and_group_sse_routes_return_event_stream_without_request_query(tmp_path):
    app = _create_test_app(tmp_path)

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
