from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient
import yaml

from character_memory.api import create_api
from character_memory.config import load_settings, resolve_media_dir, set_character_archived
from character_memory.media import MediaStorage
from character_memory.space_store import (
    MAX_COMMENTERS_PER_POST,
    MAX_IMAGES_PER_POST,
    SpaceAttachmentInput,
    SpaceRepository,
)
from character_memory.space_web import attach_space_routes
from character_memory.storage.sqlite import SQLiteStore


def _config(tmp_path: Path, count: int = 12) -> Path:
    personas = tmp_path / "personas"
    for index in range(count):
        character_id = f"c{index:02d}"
        directory = personas / character_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "persona.yaml").write_text(
            yaml.safe_dump(
                {"id": character_id, "name": f"角色{index:02d}", "identity": "测试角色"},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                'api_key: ""',
                'embedding_provider: "deterministic"',
                f'db_path: "{(tmp_path / "space.db").as_posix()}"',
                f'persona_path: "{(personas / "c00" / "persona.yaml").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    return config


def test_space_repository_keeps_shared_social_facts_and_caps_distinct_commenters(tmp_path: Path):
    store = SQLiteStore(tmp_path / "space-store.db")
    repo = SpaceRepository(store)
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    post = repo.create_post("c00", "今天第一次发动态。", now)

    repo.record_view(post.id, "c01", now + timedelta(minutes=1))
    repo.set_reaction(post.id, "c01", "LIKE", True, now + timedelta(minutes=2))
    for index in range(MAX_COMMENTERS_PER_POST):
        repo.add_comment(post.id, f"c{index + 1:02d}", f"评论 {index}", now + timedelta(minutes=3 + index))

    # The same commenter may continue a conversation without increasing the
    # number of people participating in the thread.
    repo.add_comment(post.id, "c01", "再说一句", now + timedelta(minutes=20))

    try:
        repo.add_comment(post.id, "c11", "第十一个评论者", now + timedelta(minutes=21))
        assert False, "the eleventh distinct commenter must be rejected"
    except ValueError as exc:
        assert "at most" in str(exc)

    assert [item.character_id for item in repo.list_views(post.id)] == ["c01"]
    assert [item.character_id for item in repo.list_reactions(post.id)] == ["c01"]
    assert len(repo.list_comments(post.id)) == MAX_COMMENTERS_PER_POST + 1
    assert "space/001-core" in store.list_schema_migrations()
    store.close()


def test_space_repository_supports_up_to_nine_images_voice_and_link_preview(tmp_path: Path):
    store = SQLiteStore(tmp_path / "space-multimedia.db")
    repo = SpaceRepository(store)
    now = datetime(2026, 9, 22, 7, 0, tzinfo=timezone.utc)

    images = [
        SpaceAttachmentInput(kind="IMAGE", source="SEARCH", media_id=f"image-{index}")
        for index in range(MAX_IMAGES_PER_POST)
    ]
    post = repo.create_post("c00", "九宫格测试", now, attachments=images)
    assert post.post_type == "IMAGE_SET"
    assert len(repo.list_attachments(post.id)) == 9
    assert [item.order_index for item in repo.list_attachments(post.id)] == list(range(9))

    voice = repo.create_post(
        "c00",
        "",
        now + timedelta(minutes=1),
        attachments=[
            SpaceAttachmentInput(
                kind="AUDIO",
                source="TTS",
                media_id="voice-1",
                transcript="今天想用语音说。",
                duration_ms=4200,
            )
        ],
    )
    assert voice.post_type == "VOICE"
    assert repo.list_attachments(voice.id)[0].transcript == "今天想用语音说。"

    link = repo.create_post(
        "c00",
        "这个链接有点意思。",
        now + timedelta(minutes=2),
        attachments=[
            SpaceAttachmentInput(
                kind="LINK_PREVIEW",
                source="WEB",
                url="https://example.com/story",
                title="Example Story",
                description="A small preview.",
                thumbnail_url="https://example.com/thumb.jpg",
            )
        ],
    )
    assert link.post_type == "LINK"
    assert repo.list_attachments(link.id)[0].url == "https://example.com/story"

    try:
        repo.create_post(
            "c00",
            "十张不允许",
            now + timedelta(minutes=3),
            attachments=[
                SpaceAttachmentInput(kind="IMAGE", source="SEARCH", media_id=f"too-many-{index}")
                for index in range(10)
            ],
        )
        assert False, "the tenth image must be rejected"
    except ValueError as exc:
        assert "at most 9 images" in str(exc)

    assert "space/004-multimedia-attachments" in store.list_schema_migrations()
    store.close()


def test_space_api_projects_image_grid_voice_and_link_attachments(tmp_path: Path):
    config = _config(tmp_path, count=2)
    settings = load_settings(str(config))
    media_store = MediaStorage(resolve_media_dir(settings))
    store = SQLiteStore(settings.db_path)
    now = datetime(2026, 9, 22, 7, 10, tzinfo=timezone.utc)

    image_ids = []
    for index in range(3):
        asset = media_store.save_bytes(
            character_id="c00",
            original_name=f"image-{index}.png",
            payload=b"\x89PNG\r\n\x1a\n" + bytes([index + 1]) * 16,
            created_at=now,
            source="SEARCH",
        )
        store.add_media_asset(asset)
        image_ids.append(asset.id)

    voice_asset = media_store.save_bytes(
        character_id="c00",
        original_name="voice.wav",
        payload=b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 24,
        created_at=now,
        source="TTS",
    )
    store.add_media_asset(voice_asset)
    store.close()

    app = create_api(str(config))
    attach_space_routes(app)
    attachments = [
        *[
            {"kind":"IMAGE", "source":"SEARCH", "media_id":media_id}
            for media_id in image_ids
        ],
        {
            "kind":"AUDIO",
            "source":"TTS",
            "media_id":voice_asset.id,
            "transcript":"这是一条语音动态。",
            "duration_ms":3200,
        },
        {
            "kind":"LINK_PREVIEW",
            "source":"WEB",
            "url":"https://example.com/article",
            "title":"一个网页标题",
            "description":"网页摘要",
            "thumbnail_url":"https://example.com/thumb.jpg",
        },
    ]

    with TestClient(app) as client:
        created = client.post(
            "/v1/space/posts",
            json={"character_id":"c00", "content":"混合动态", "attachments":attachments},
        )
        assert created.status_code == 200, created.text
        post = created.json()["post"]
        assert post["post_type"] == "MIXED"
        assert post["image_count"] == 3
        assert post["max_images"] == 9
        assert [item["kind"] for item in post["attachments"]] == [
            "IMAGE", "IMAGE", "IMAGE", "AUDIO", "LINK_PREVIEW"
        ]
        assert post["attachments"][0]["media"]["url"].startswith("/v1/media/")
        assert post["attachments"][3]["transcript"] == "这是一条语音动态。"
        assert post["attachments"][4]["title"] == "一个网页标题"

        fetched = client.get(f"/v1/space/posts/{post['id']}")
        assert fetched.status_code == 200
        assert len(fetched.json()["post"]["attachments"]) == 5


def test_space_api_archive_rules_preserve_history_but_stop_new_participation(tmp_path: Path):
    config = _config(tmp_path)
    settings = load_settings(str(config))
    app = create_api(str(config))
    attach_space_routes(app)

    with TestClient(app) as client:
        created = client.post("/v1/space/posts", json={"character_id":"c00", "content":"今天有点想出去走走。"})
        assert created.status_code == 200
        post_id = created.json()["post"]["id"]
        assert client.get("/health").json()["runtime_loaded"] is False

        assert client.put(f"/v1/space/posts/{post_id}/views/c01").status_code == 200
        assert client.put(f"/v1/space/posts/{post_id}/likes/c01").status_code == 200
        assert client.post(
            f"/v1/space/posts/{post_id}/comments",
            json={"character_id":"c01", "content":"去呀，天气看起来不错。"},
        ).status_code == 200

        set_character_archived(settings, "c01", True)
        assert client.post(
            f"/v1/space/posts/{post_id}/comments",
            json={"character_id":"c01", "content":"归档后不应继续参与"},
        ).status_code == 409
        assert client.put(f"/v1/space/posts/{post_id}/likes/c01").status_code == 409
        assert client.put(f"/v1/space/posts/{post_id}/views/c01").status_code == 409

        # Historical participation remains visible after archive.
        feed = client.get("/v1/space/posts").json()
        assert feed["posts"][0]["comments"][0]["author"]["archived"] is True
        assert feed["posts"][0]["likes"][0]["character"]["archived"] is True

        set_character_archived(settings, "c00", True)
        assert client.post("/v1/space/posts", json={"character_id":"c00", "content":"归档后不能发"}).status_code == 409
        # The old post is not deleted merely because its author was archived.
        still_there = client.get(f"/v1/space/posts/{post_id}")
        assert still_there.status_code == 200
        assert still_there.json()["post"]["author"]["archived"] is True
        assert client.get("/health").json()["runtime_loaded"] is False


def test_space_feed_is_capped_to_ten_items_and_can_filter_one_character(tmp_path: Path):
    config = _config(tmp_path, count=2)
    app = create_api(str(config))
    attach_space_routes(app)

    with TestClient(app) as client:
        for index in range(13):
            author = "c00" if index % 2 == 0 else "c01"
            response = client.post("/v1/space/posts", json={"character_id":author, "content":f"动态 {index}"})
            assert response.status_code == 200

        feed = client.get("/v1/space/posts?limit=50").json()
        assert len(feed["posts"]) == 10
        assert feed["max_feed_items"] == 10
        assert feed["total"] == 13
        assert feed["has_more"] is True

        filtered = client.get("/v1/space/posts?character_id=c01&limit=10").json()
        assert filtered["total"] == 6
        assert all(item["character_id"] == "c01" for item in filtered["posts"])


def test_space_frontend_has_global_and_character_entry_without_a_second_app_controller():
    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "character_memory" / "web"
    index = (web / "index.html").read_text(encoding="utf-8")
    script_path = web / "space.js"
    script = script_path.read_text(encoding="utf-8")
    css = (web / "space.css").read_text(encoding="utf-8")

    assert "/static/space.css" in index
    assert "/static/space.js" in index
    for token in [
        "space-nav-button",
        "characterSpaceButton",
        "/v1/space/posts",
        "limit:\"10\"",
        "space-media-grid",
        "space-voice-attachment",
        "space-link-preview",
        "data-space-image",
        "CM.registerFeature(\"space\"",
    ]:
        assert token in script
    for token in [
        "space-shell",
        "space-post",
        "space-comments",
        "space-character-entry",
        "space-media-grid",
        "space-media-cell",
        "space-voice-attachment",
        "space-link-preview",
    ]:
        assert token in css
    assert 'addEventListener("submit"' not in script

    node = shutil.which("node")
    if node:
        checked = subprocess.run([node, "--check", str(script_path)], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr
