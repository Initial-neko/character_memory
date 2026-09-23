from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.group_web import attach_group_routes
from character_memory.storage.message_search import MessageSearchRepository
from character_memory.storage.sqlite import SQLiteStore


def test_group_archive_is_reversible_and_preserves_durable_facts(tmp_path: Path):
    store = SQLiteStore(tmp_path / "archive.db")
    repo = GroupRepository(store)
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    group = repo.create_group("旧任务", ["rin", "momo"], now)
    event = repo.append_event(
        GroupEvent(
            conversation_id=group.id,
            turn_id="turn-archive",
            actor_type="USER",
            actor_id="user",
            event_type="USER_MESSAGE",
            event_time=now,
            content="这条消息必须保留",
        )
    )

    archived = repo.archive_group(group.id, now)
    assert archived is not None
    assert archived.archived_at is not None
    assert repo.get_group(group.id) is None
    assert repo.get_group(group.id, include_archived=True) is not None
    assert repo.list_groups() == []
    assert [item.id for item in repo.list_groups(archived=True)] == [group.id]
    assert [item.id for item in repo.list_events(group.id)] == [event.id]
    assert "group/003-conversation-archive" in store.list_schema_migrations()

    restored = repo.restore_group(group.id)
    assert restored is not None
    assert restored.archived_at is None
    assert [item.id for item in repo.list_groups()] == [group.id]
    assert repo.list_groups(archived=True) == []
    assert [item.id for item in repo.list_events(group.id)] == [event.id]
    store.close()


def test_archived_group_is_hidden_from_normal_search_but_not_deleted(tmp_path: Path):
    store = SQLiteStore(tmp_path / "archive-search.db")
    repo = GroupRepository(store)
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    group = repo.create_group("搜索测试", ["rin", "momo"], now)
    repo.append_event(
        GroupEvent(
            conversation_id=group.id,
            turn_id="turn-search",
            actor_type="USER",
            actor_id="user",
            event_type="USER_MESSAGE",
            event_time=now,
            content="归档之后默认搜索不要出现独角兽",
        )
    )
    search = MessageSearchRepository(store)
    assert len(search.search_group("独角兽")) == 1

    repo.archive_group(group.id, now)
    assert search.search_group("独角兽") == []
    assert len(search.search_group("独角兽", include_archived=True)) == 1
    assert len(repo.list_events(group.id)) == 1
    store.close()


def test_group_archive_api_hides_and_restores_without_loading_runtime(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                'api_key: ""',
                'embedding_provider: "deterministic"',
                f'db_path: "{(tmp_path / "archive-api.db").as_posix()}"',
                f'persona_path: "{(root / "personas" / "rin" / "persona.yaml").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    app = create_api(str(config))
    attach_group_routes(app, str(config))

    with TestClient(app) as client:
        profiles = client.get("/v1/characters").json()["characters"]
        created = client.post(
            "/v1/groups",
            json={"name": "可归档任务", "member_ids": [profiles[0]["id"], profiles[1]["id"]]},
        )
        assert created.status_code == 200
        group_id = created.json()["group"]["id"]
        assert client.get("/health").json()["runtime_loaded"] is False

        archived = client.post(f"/v1/groups/{group_id}/archive")
        assert archived.status_code == 200
        assert archived.json()["group"]["archived_at"]
        assert client.get("/v1/groups").json()["groups"] == []
        archived_list = client.get("/v1/groups?archived=true").json()["groups"]
        assert [item["id"] for item in archived_list] == [group_id]
        assert client.get(f"/v1/groups/{group_id}/history").status_code == 200
        # Archived tasks are read-only until restored; even the legacy sync chat
        # route rejects before it can initialize the model.
        rejected = client.post(f"/v1/groups/{group_id}/chat", json={"message": "不应该继续写入"})
        assert rejected.status_code == 404
        assert client.get("/health").json()["runtime_loaded"] is False

        restored = client.post(f"/v1/groups/{group_id}/restore")
        assert restored.status_code == 200
        assert restored.json()["group"]["archived_at"] is None
        assert [item["id"] for item in client.get("/v1/groups").json()["groups"]] == [group_id]
        assert client.get("/v1/groups?archived=true").json()["groups"] == []
        assert client.get("/health").json()["runtime_loaded"] is False


def test_group_list_hides_legacy_zero_member_ensemble_artifacts(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                'api_key: ""',
                'embedding_provider: "deterministic"',
                f'db_path: "{(tmp_path / "legacy-empty-group.db").as_posix()}"',
                f'persona_path: "{(root / "personas" / "rin" / "persona.yaml").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    app = create_api(str(config))
    attach_group_routes(app, str(config))
    now = datetime(2026, 9, 23, 7, 0, tzinfo=timezone.utc)
    legacy = GroupRepository(app.state.character_memory.read_store).create_group(
        "遗留 AI 构建",
        [],
        now,
    )

    with TestClient(app) as client:
        assert client.get("/v1/groups").json()["groups"] == []
        # The row still exists so a user upgrade is non-destructive; it is only
        # removed from the normal chat surface.
        stored = GroupRepository(app.state.character_memory.read_store).get_group(
            legacy.id,
            include_archived=True,
        )
        assert stored is not None
        assert stored.member_ids == []


def test_group_archive_frontend_contract_and_syntax():
    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "character_memory" / "web"
    groups_path = web / "groups.js"
    groups = groups_path.read_text(encoding="utf-8")
    css = (web / "p0_11.css").read_text(encoding="utf-8")

    for token in [
        "group-archive-list-button",
        "data-group-archive",
        "data-group-restore",
        "?archived=true",
        "/archive",
        "/restore",
        "await CM.switchCharacter(CM.state.characterId)",
    ]:
        assert token in groups
    for token in ["group-context-menu", "group-more-button", "group-archive-card"]:
        assert token in css

    node = shutil.which("node")
    if node:
        checked = subprocess.run([node, "--check", str(groups_path)], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr
