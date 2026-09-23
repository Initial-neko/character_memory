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


def test_existing_group_can_remove_character_without_deleting_character(tmp_path: Path):
    config = _config(tmp_path)
    app = create_api(str(config))
    attach_group_routes(app, str(config))
    attach_group_member_routes(app)

    with TestClient(app) as client:
        profiles = client.get("/v1/characters").json()["characters"]
        first, second, third = [item["id"] for item in profiles[:3]]
        created = client.post("/v1/groups", json={"name": "缩容群", "member_ids": [first, second, third]})
        group_id = created.json()["group"]["id"]

        removed = client.delete(f"/v1/groups/{group_id}/members/{second}")
        assert removed.status_code == 200
        payload = removed.json()
        assert payload["removed_member_id"] == second
        assert payload["group"]["member_ids"] == [first, third]

        remaining_profiles = {item["id"] for item in client.get("/v1/characters").json()["characters"]}
        assert second in remaining_profiles

        repeated = client.delete(f"/v1/groups/{group_id}/members/{second}")
        assert repeated.status_code == 404


def test_group_member_api_keeps_two_character_minimum(tmp_path: Path):
    config = _config(tmp_path)
    app = create_api(str(config))
    attach_group_routes(app, str(config))
    attach_group_member_routes(app)

    with TestClient(app) as client:
        profiles = client.get("/v1/characters").json()["characters"]
        first, second = [item["id"] for item in profiles[:2]]
        created = client.post("/v1/groups", json={"name": "最小群", "member_ids": [first, second]})
        group_id = created.json()["group"]["id"]

        response = client.delete(f"/v1/groups/{group_id}/members/{first}")
        assert response.status_code == 400
        assert "至少 2" in response.json()["detail"]

        current = client.get(f"/v1/groups/{group_id}/history?limit=10")
        assert current.status_code == 200
        assert current.json()["group"]["member_ids"] == [first, second]


def test_group_settings_exposes_remove_member_flow():
    root = Path(__file__).resolve().parents[1]
    script = (root / "src" / "character_memory" / "web" / "group_settings.js").read_text(encoding="utf-8")
    assert "/members/\${encodeURIComponent(characterId)}" in script
    assert "移出群聊" in script
    assert "移出成员后也从下一轮起生效" in script


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


def test_group_can_hold_more_than_four_characters_for_ensemble_chat(tmp_path: Path):
    root = tmp_path / "personas"
    for index in range(8):
        directory = root / f"c{index}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "persona.yaml").write_text(
            f"id: c{index}\nname: C{index}\n",
            encoding="utf-8",
        )
    config = tmp_path / "large-group.yaml"
    config.write_text(
        "\n".join(
            [
                'api_key: ""',
                'embedding_provider: "deterministic"',
                f'db_path: "{(tmp_path / "large-group.db").as_posix()}"',
                f'persona_path: "{(root / "c0" / "persona.yaml").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    app = create_api(str(config))
    attach_group_routes(app, str(config))

    with TestClient(app) as client:
        ids = [item["id"] for item in client.get("/v1/characters").json()["characters"]]
        response = client.post("/v1/groups", json={"name": "群像", "member_ids": ids})
        assert response.status_code == 200
        assert len(response.json()["group"]["member_ids"]) == 8
        assert response.json()["group"]["status"] == "ACTIVE"


