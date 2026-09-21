from pathlib import Path

from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.group_members_web import attach_group_member_routes
from character_memory.group_web import attach_group_routes


def _config(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            [
                'api_key: ""',
                'embedding_provider: "deterministic"',
                f'db_path: "{(tmp_path / "members.db").as_posix()}"',
                f'persona_path: "{(root / "personas" / "rin" / "persona.yaml").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_existing_group_can_add_character_without_loading_runtime(tmp_path: Path):
    config = _config(tmp_path)
    app = create_api(str(config))
    attach_group_routes(app, str(config))
    attach_group_member_routes(app)

    with TestClient(app) as client:
        profiles = client.get("/v1/characters").json()["characters"]
        assert len(profiles) >= 3
        first, second, third = [item["id"] for item in profiles[:3]]
        created = client.post("/v1/groups", json={"name": "扩容群", "member_ids": [first, second]})
        assert created.status_code == 200
        group_id = created.json()["group"]["id"]
        assert client.get("/health").json()["runtime_loaded"] is False

        added = client.post(f"/v1/groups/{group_id}/members", json={"member_ids": [third]})
        assert added.status_code == 200
        payload = added.json()
        assert payload["added_member_ids"] == [third]
        assert payload["group"]["member_ids"] == [first, second, third]
        assert client.get("/health").json()["runtime_loaded"] is False

        repeated = client.post(f"/v1/groups/{group_id}/members", json={"member_ids": [third]})
        assert repeated.status_code == 200
        assert repeated.json()["added_member_ids"] == []
        assert repeated.json()["group"]["member_ids"] == [first, second, third]


def test_group_member_api_rejects_unknown_character(tmp_path: Path):
    config = _config(tmp_path)
    app = create_api(str(config))
    attach_group_routes(app, str(config))
    attach_group_member_routes(app)

    with TestClient(app) as client:
        profiles = client.get("/v1/characters").json()["characters"]
        created = client.post("/v1/groups", json={"name": "扩容群", "member_ids": [profiles[0]["id"], profiles[1]["id"]]})
        group_id = created.json()["group"]["id"]
        response = client.post(f"/v1/groups/{group_id}/members", json={"member_ids": ["not-a-character"]})
        assert response.status_code == 404


def test_group_settings_exposes_add_member_flow():
    root = Path(__file__).resolve().parents[1]
    script = (root / "src" / "character_memory" / "web" / "group_settings.js").read_text(encoding="utf-8")
    assert "/members`" in script
    assert "添加选中成员" in script
    assert "checked disabled" in script
    assert "新成员从加入后的下一轮消息开始参与" in script


def test_core_launchers_start_only_character_runtime():
    root = Path(__file__).resolve().parents[1]
    for name in ["core-start.sh", "mobile-core-start.sh"]:
        text = (root / "scripts" / name).read_text(encoding="utf-8")
        assert "character_memory.cli" in text
        assert "--port 8000" in text
        assert "character-stack" not in text or "when you need the full" in text
        assert "character_memory.media_bootstrap" not in text
        assert "character_memory.dev_server" not in text
        assert "character_memory.settings_server" not in text
        assert "character_memory.tts_lab" not in text
