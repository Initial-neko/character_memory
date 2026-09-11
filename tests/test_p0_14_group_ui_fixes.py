from pathlib import Path

from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.group_web import attach_group_routes


def test_group_rename_api_persists_without_loading_model_runtime(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                'api_key: ""',
                'embedding_provider: "deterministic"',
                f'db_path: "{(tmp_path / "group-rename.db").as_posix()}"',
                f'persona_path: "{(root / "personas" / "rin" / "persona.yaml").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    app = create_api(str(config))
    attach_group_routes(app, str(config))

    with TestClient(app) as client:
        profiles = client.get("/v1/characters").json()["characters"]
        assert len(profiles) >= 2
        created = client.post(
            "/v1/groups",
            json={"name": "旧群名", "member_ids": [profiles[0]["id"], profiles[1]["id"]]},
        )
        assert created.status_code == 200
        group_id = created.json()["group"]["id"]
        assert client.get("/health").json()["runtime_loaded"] is False

        renamed = client.patch(f"/v1/groups/{group_id}", json={"name": "  新群名  "})
        assert renamed.status_code == 200
        assert renamed.json()["group"]["name"] == "新群名"

        history = client.get(f"/v1/groups/{group_id}/history")
        assert history.status_code == 200
        assert history.json()["group"]["name"] == "新群名"
        assert client.get("/health").json()["runtime_loaded"] is False


def test_group_rename_ui_is_wired_without_adding_a_second_composer_submit_owner():
    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "character_memory" / "web"
    groups = (web / "groups.js").read_text(encoding="utf-8")
    core = (web / "app.js").read_text(encoding="utf-8")
    css = (web / "p0_11.css").read_text(encoding="utf-8")

    assert 'method:"PATCH"' in groups
    assert "data-group-rename-name" in groups
    assert "data-group-rename-confirm" in groups
    assert "group-name-editable" in groups
    assert "group-name-editable" in css
    assert core.count('addEventListener("submit"') == 1
    assert 'addEventListener("submit"' not in groups
