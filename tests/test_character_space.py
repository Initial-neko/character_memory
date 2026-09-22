from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient
import yaml

from character_memory.api import create_api
from character_memory.config import load_settings, set_character_archived
from character_memory.space_media import MAX_SPACE_MEDIA_PER_POST, SpacePostMediaRepository
from character_memory.space_store import MAX_COMMENTERS_PER_POST, SpaceRepository
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
                f'media_dir: "{(tmp_path / "media").as_posix()}"',
                f'persona_path: "{(personas / "c00" / "persona.yaml").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    return config


def _save_test_media(app, character_id: str, index: int, *, source: str = "USER_UPLOAD"):
    access = app.state.character_memory
    asset = access.media_storage.save_bytes(
        character_id=character_id,
        original_name=f"space-{index}.png",
        payload=b"\x89PNG\r\n\x1a\n" + f"space-{index}".encode("utf-8"),
        created_at=datetime(2026, 9, 22, 8, index, tzinfo=timezone.utc),
        source=source,
    )
    access.read_store.add_media_asset(asset)
    return asset


def _save_test_audio(app, character_id: str, index: int, *, source: str = "SPACE_VOICE"):
    access = app.state.character_memory
    asset = access.media_storage.save_bytes(
        character_id=character_id,
        original_name=f"space-{index}.wav",
        payload=b"RIFF" + (b"\x00" * 4) + b"WAVEfmt " + (b"\x00" * 24),
        created_at=datetime(2026, 9, 22, 8, index, tzinfo=timezone.utc),
        source=source,
    )
    access.read_store.add_media_asset(asset)
    return asset


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


def _legacy_asset(store: SQLiteStore, media_id: str, mime_type: str) -> None:
    from character_memory.media import MediaAsset

    store.add_media_asset(
        MediaAsset(
            id=media_id,
            character_id="c00",
            source="USER_UPLOAD",
            original_name="legacy",
            mime_type=mime_type,
            storage_name=f"{media_id}.bin",
            created_at=datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc),
            size_bytes=8,
        )
    )


def test_legacy_space_media_backfill_reads_the_kind_from_the_asset_mime(tmp_path: Path):
    """A legacy attachment is not assumed to be an image.

    The backfill is an INSERT OR IGNORE against UNIQUE(post_id, media_id), so a
    wrong kind is written once and skipped forever -- the repair pass is what
    makes a previously mislabelled voice attachment recoverable.
    """
    store = SQLiteStore(tmp_path / "legacy-kind.db")
    posts = SpaceRepository(store)
    now = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)

    _legacy_asset(store, "legacy-voice", "audio/wav")
    _legacy_asset(store, "legacy-photo", "image/png")
    _legacy_asset(store, "legacy-unknown", "application/octet-stream")
    voice = posts.create_post("c00", "旧的语音动态", now, media_id="legacy-voice")
    photo = posts.create_post("c00", "旧的图片动态", now, media_id="legacy-photo")
    unknown = posts.create_post("c00", "旧的其他附件", now, media_id="legacy-unknown")
    dangling = posts.create_post("c00", "资产已丢失", now, media_id="missing-asset")

    media = SpacePostMediaRepository(store)
    kinds = {
        item.media_id: (item.media_type, item.source_type)
        for item in media.list_for_post(voice.id)
        + media.list_for_post(photo.id)
        + media.list_for_post(unknown.id)
        + media.list_for_post(dangling.id)
    }
    assert kinds["legacy-voice"] == ("VOICE", "LEGACY")
    assert kinds["legacy-photo"] == ("IMAGE", "LEGACY")
    assert kinds["legacy-unknown"] == ("IMAGE", "LEGACY")
    assert kinds["missing-asset"] == ("IMAGE", "LEGACY")

    # A row written by the earlier hardcoded-IMAGE build is corrected on the
    # next construction rather than staying wrong for the life of the database.
    with store._lock:
        store.conn.execute(
            "UPDATE space_post_media SET media_type='IMAGE' WHERE media_id='legacy-voice'"
        )
        store._maybe_commit()
    repaired = SpacePostMediaRepository(store)
    assert repaired.list_for_post(voice.id)[0].media_type == "VOICE"
    assert repaired.list_for_post(photo.id)[0].media_type == "IMAGE"
    store.close()


def test_space_media_relation_migrates_legacy_media_and_caps_ordered_items(tmp_path: Path):
    store = SQLiteStore(tmp_path / "space-media.db")
    posts = SpaceRepository(store)
    now = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)
    post = posts.create_post("c00", "旧单图动态", now, media_id="legacy-image")

    media = SpacePostMediaRepository(store)
    migrated = media.list_for_post(post.id)
    assert [(item.media_id, item.media_type, item.source_type, item.sort_order) for item in migrated] == [
        ("legacy-image", "IMAGE", "LEGACY", 0)
    ]
    assert "space/004-post-media" in store.list_schema_migrations()

    items = [
        {
            "media_id": f"asset-{index}",
            "media_type": "IMAGE",
            "source_type": "CHARACTER",
            "metadata": {"index": index},
        }
        for index in range(MAX_SPACE_MEDIA_PER_POST)
    ]
    replaced = media.replace_for_post(post.id, items, now + timedelta(minutes=1))
    assert [item.media_id for item in replaced] == [f"asset-{index}" for index in range(9)]
    assert [item.sort_order for item in replaced] == list(range(9))
    assert replaced[4].metadata == {"index": 4}

    try:
        media.replace_for_post(
            post.id,
            items + [{
                "media_id": "asset-9",
                "media_type": "IMAGE",
                "source_type": "CHARACTER",
            }],
            now,
        )
        assert False, "the tenth media item must be rejected"
    except ValueError as exc:
        assert "at most 9" in str(exc)

    store.close()


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


def test_space_api_supports_ordered_multi_image_posts_and_legacy_single_media(tmp_path: Path):
    config = _config(tmp_path, count=2)
    app = create_api(str(config))
    attach_space_routes(app)

    with TestClient(app) as client:
        assets = [_save_test_media(app, "c00", index, source="GENERATED") for index in range(3)]
        media_ids = [asset.id for asset in assets]

        created = client.post(
            "/v1/space/posts",
            json={"character_id":"c00", "content":"三张图一起发。", "media_ids":media_ids},
        )
        assert created.status_code == 200
        post = created.json()["post"]
        assert post["media_id"] == media_ids[0]
        assert post["media_count"] == 3
        assert post["media_limit"] == 9
        assert [item["media_id"] for item in post["media_items"]] == media_ids
        assert [item["sort_order"] for item in post["media_items"]] == [0, 1, 2]
        assert {item["media_type"] for item in post["media_items"]} == {"IMAGE"}
        assert {item["source_type"] for item in post["media_items"]} == {"GENERATED"}
        assert all(item["available"] for item in post["media_items"])
        assert post["media"]["media_id"] == media_ids[0]

        legacy_asset = _save_test_media(app, "c00", 8)
        legacy = client.post(
            "/v1/space/posts",
            json={"character_id":"c00", "media_id":legacy_asset.id},
        )
        assert legacy.status_code == 200
        legacy_post = legacy.json()["post"]
        assert legacy_post["media_id"] == legacy_asset.id
        assert [item["media_id"] for item in legacy_post["media_items"]] == [legacy_asset.id]

        too_many = client.post(
            "/v1/space/posts",
            json={
                "character_id":"c00",
                "content":"不应接受十张图",
                "media_ids":[f"missing-{index}" for index in range(10)],
            },
        )
        assert too_many.status_code == 422

        missing = client.post(
            "/v1/space/posts",
            json={"character_id":"c00", "content":"坏资源", "media_ids":["missing-one"]},
        )
        assert missing.status_code == 400


def test_space_api_projects_audio_assets_as_voice_media(tmp_path: Path):
    config = _config(tmp_path, count=2)
    app = create_api(str(config))
    attach_space_routes(app)

    with TestClient(app) as client:
        audio = _save_test_audio(app, "c00", 1)
        created = client.post(
            "/v1/space/posts",
            json={"character_id":"c00", "content":"听一下。", "media_ids":[audio.id]},
        )
        assert created.status_code == 200
        post = created.json()["post"]
        assert post["media_count"] == 1
        item = post["media_items"][0]
        assert item["media_type"] == "VOICE"
        assert item["mime_type"] == "audio/wav"
        assert item["available"] is True
        assert item["url"] == f"/v1/media/{audio.id}"


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
        assert feed["max_media_per_post"] == 9
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
        'limit:"10"',
        'CM.registerFeature("space"',
        "media_items",
        "slice(0, 9)",
        "data-space-media-count",
        'images.length === 1 ? "single"',
        'images.length <= 4 ? "quad" : "nine"',
        "space-voice-bubble",
        "data-space-voice-play",
        "data-space-voice-text",
        "new Audio(",
    ]:
        assert token in script
    for token in [
        "space-shell",
        "space-post",
        "space-comments",
        "space-character-entry",
        ".space-media-grid",
        ".space-media-single",
        ".space-media-quad",
        ".space-media-nine",
        ".space-voice-bubble",
        ".space-voice-transcript",
    ]:
        assert token in css
    assert 'addEventListener("submit"' not in script

    node = shutil.which("node")
    if node:
        checked = subprocess.run([node, "--check", str(script_path)], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr
