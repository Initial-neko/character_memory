from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from character_memory.ensemble_builder import (
    EnsembleBuilderService,
    EnsembleMemberResearch,
    EnsembleRepository,
    EnsembleResearch,
)
from character_memory.ensemble_web import attach_ensemble_routes
from character_memory.group_store import GroupRepository
from character_memory.storage.sqlite import SQLiteStore


class FakeWorldObserver:
    def __init__(self):
        self.calls = []

    def observe(self, query, *, max_pages, max_chars_per_page, search_limit):
        self.calls.append(
            {
                "query": query,
                "max_pages": max_pages,
                "max_chars_per_page": max_chars_per_page,
                "search_limit": search_limit,
            }
        )
        return {
            "query": query,
            "search_results": 2,
            "errors": [],
            "observations": [
                SimpleNamespace(
                    title="LAB MEM members",
                    url="https://example.org/labmem",
                    source_domain="example.org",
                    snippet="公开角色资料",
                    content="Okabe, Kurisu and Mayuri are core lab members. They have distinct personalities and relationships.",
                ),
                SimpleNamespace(
                    title="Character relationships",
                    url="https://example.net/relationships",
                    source_domain="example.net",
                    snippet="公开关系资料",
                    content="Kurisu debates Okabe; Mayuri is a long-time friend of Okabe.",
                ),
            ],
        }


class FakeEnsembleModel:
    attempts = 1

    def structured_for_session(self, prompt, schema, session_id):
        # Keep fixture descriptions above the production research-schema minimum;
        # these tests are about the ensemble lifecycle, not validation failures.
        assert "PUBLIC SOURCE" in prompt
        assert session_id.startswith("ensemble-research:")
        return EnsembleResearch(
            group_name="LAB MEM",
            overview="未来道具研究所的核心成员。",
            members=[
                EnsembleMemberResearch(
                    name="Kurisu",
                    age=18,
                    identity="研究者",
                    description="理性、反应快，对荒唐说法会直接吐槽，也会认真维护自己的专业判断和边界。",
                    speech_style="清晰直接，常会指出逻辑问题。",
                    relationship_notes=["经常与 Okabe 争论"],
                    tags=["研究", "吐槽"],
                ),
                EnsembleMemberResearch(
                    name="Okabe",
                    age=18,
                    identity="未来道具研究所成员",
                    description="表现夸张、喜欢戏剧化地说话，但在真正重要的事情上会认真负责并保护同伴。",
                    speech_style="戏剧化表达和认真语气并存。",
                    relationship_notes=["与 Kurisu 经常针锋相对", "重视 Mayuri"],
                    tags=["实验室", "夸张"],
                ),
                EnsembleMemberResearch(
                    name="Mayuri",
                    age=16,
                    identity="未来道具研究所成员",
                    description="温和自然，擅长注意周围人的情绪和气氛，也会按照自己的感受做出明确选择。",
                    speech_style="轻松自然，节奏偏慢。",
                    relationship_notes=["与 Okabe 是长期朋友"],
                    tags=["温和", "手工"],
                ),
            ],
        )

    @staticmethod
    def _json(text):
        return json.loads(text)

    def _request(self, messages, *, conversation_id=None, json_object=False):
        assert conversation_id == "persona-builder"
        prompt = messages[-1]["content"]
        if "名字偏好：Okabe" in prompt:
            name, identity, tagline = "Okabe", "未来道具研究所成员", "夸张外表下有自己的认真"
        elif "名字偏好：Mayuri" in prompt:
            name, identity, tagline = "Mayuri", "未来道具研究所成员", "温和但不是没有自己的选择"
        else:
            name, identity, tagline = "Kurisu", "研究者", "直接、理性，也会被荒唐事惹恼"
        return json.dumps(
            {
                "name": name,
                "age": 18,
                "identity": identity,
                "tagline": tagline,
                "description": f"{name} 是根据公开资料整理的人物草稿，有独立判断和清楚边界。",
                "personality": ["独立", "有自己的判断", "重视真实经历"],
                "conversation": "自然口语，保持人物自己的表达习惯。",
                "expression": "表达有辨识度但不过度机械化。",
                "questions": "真的好奇时才追问。",
                "silence": "没有自然想说的话时可以沉默。",
                "initiative": "遇到与共同经历有关的事情时会自然提起。",
                "disagreement": "不同意时会直接表达理由。",
                "care": "通过具体行动和记住细节表达关心。",
                "boundaries": ["不无条件迎合", "关系通过共同经历发展"],
            },
            ensure_ascii=False,
        )


def _access(tmp_path):
    store = SQLiteStore(str(tmp_path / "ensemble.db"))
    profiles = [
        {"id": "kurisu", "name": "Kurisu", "identity": "研究者"},
    ]
    model = FakeEnsembleModel()
    observer = FakeWorldObserver()
    created = []

    access = SimpleNamespace(
        read_store=store,
        store=lambda: store,
        world_observer=observer,
        require_bundle=lambda: SimpleNamespace(model=model),
        character_profiles=lambda: list(profiles),
        soft_active_characters=10,
        max_active_characters=20,
    )

    def check_capacity(add_count, *, confirm_over_soft_limit=False):
        active = len(profiles)
        result = active + int(add_count)
        if result > 20:
            raise ValueError("hard limit")
        if result > 10 and not confirm_over_soft_limit:
            raise ValueError("soft confirmation required")
        return {"active_count": active, "result_count": result}

    def create_character(draft, requested_id="", *, confirm_over_soft_limit=False, skip_capacity_check=False):
        character_id = draft.name.lower()
        profile = {"id": character_id, "name": draft.name, "identity": draft.identity}
        profiles.append(profile)
        created.append(character_id)
        return profile

    def rollback(character_id):
        profiles[:] = [item for item in profiles if item["id"] != character_id]
        if character_id in created:
            created.remove(character_id)

    access.check_character_capacity = check_capacity
    access.create_character_from_draft = create_character
    access.rollback_created_character = rollback
    return access, store, observer, profiles, created


def test_ensemble_start_creates_only_an_invisible_build_record(tmp_path):
    access, store, _, _, _ = _access(tmp_path)
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

    build = service.start("复刻命运石之门的 LAB MEM，并形成群聊", now=now)

    group = GroupRepository(store).get_group(build["group_id"])
    assert group is None
    assert build["group"] is None
    assert build["status"] == "BUILDING"
    store.close()


def test_ensemble_research_uses_world_observation_and_reuses_existing_character(tmp_path):
    access, store, observer, _, _ = _access(tmp_path)
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    started = service.start("复刻命运石之门的 LAB MEM，并形成群聊", now=now)

    build = service.research(started["group_id"], now=now)

    assert observer.calls
    assert "LAB MEM" in observer.calls[0]["query"]
    assert build["status"] == "READY"
    assert build["group_name"] == "LAB MEM"
    assert len(build["sources"]) == 2
    assert len(build["drafts"]) == 3
    kurisu = next(item for item in build["drafts"] if item["canonical_name"] == "Kurisu")
    assert kurisu["existing_character_id"] == "kurisu"
    assert GroupRepository(store).get_group(started["group_id"]) is None
    store.close()


def test_ensemble_confirmation_fills_same_group_and_only_creates_missing_characters(tmp_path):
    access, store, _, profiles, created = _access(tmp_path)
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    started = service.start("复刻命运石之门的 LAB MEM，并形成群聊", now=now)
    ready = service.research(started["group_id"], now=now)

    result = service.confirm(
        started["group_id"],
        [0, 1, 2],
        confirm_over_soft_limit=False,
        now=now,
    )

    assert result["group_id"] != started["group_id"]
    assert result["status"] == "ACTIVE"
    assert result["group"]["id"] == result["group_id"]
    assert result["group"]["status"] == "ACTIVE"
    assert result["group"]["member_ids"] == ["kurisu", "okabe", "mayuri"]
    assert created == ["okabe", "mayuri"]
    assert {item["id"] for item in profiles} == {"kurisu", "okabe", "mayuri"}
    store.close()


def test_failed_build_cannot_confirm_stale_drafts(tmp_path):
    access, store, _, _, _ = _access(tmp_path)
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    started = service.start("复刻命运石之门的 LAB MEM，并形成群聊", now=now)
    service.research(started["group_id"], now=now)
    repository.set_status(started["group_id"], "FAILED", now, error="forced")

    try:
        service.confirm(started["group_id"], [0, 1], now=now)
        assert False, "FAILED build must not be confirmable"
    except ValueError as exc:
        assert "重新开始" in str(exc)

    assert GroupRepository(store).list_groups() == []
    store.close()


def test_prepare_failure_leaves_no_real_group_or_build_record(tmp_path):
    access, store, observer, _, _ = _access(tmp_path)
    observer.observe = lambda *args, **kwargs: {
        "query": "none",
        "search_results": 0,
        "errors": [{"error": "offline"}],
        "observations": [],
    }
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

    try:
        service.prepare("复刻一个不存在的群聊", now=now)
        assert False, "prepare should fail without observations"
    except RuntimeError:
        pass

    assert GroupRepository(store).list_groups() == []
    with store._lock:
        count = store.conn.execute("SELECT COUNT(*) AS total FROM ensemble_builds").fetchone()["total"]
    assert count == 0
    store.close()


def test_prepare_route_maps_research_value_error_to_502_and_cleans_build(tmp_path):
    store = SQLiteStore(str(tmp_path / "ensemble-web.db"))
    access = SimpleNamespace(
        read_store=store,
        store=lambda: store,
        character_profiles=lambda: [],
        soft_active_characters=10,
        max_active_characters=20,
    )
    app = FastAPI()
    app.state.character_memory = access
    attach_ensemble_routes(app)

    def fail_research(*args, **kwargs):
        raise ValueError("media host could not be resolved: example.invalid")

    access.ensemble_service.research = fail_research

    with TestClient(app) as client:
        response = client.post(
            "/v1/ensembles/prepare",
            json={"prompt": "复刻一个公开作品群聊"},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "资料整理失败：media host could not be resolved: example.invalid"
    )
    with store._lock:
        total = store.conn.execute(
            "SELECT COUNT(*) AS total FROM ensemble_builds"
        ).fetchone()["total"]
    assert total == 0
    store.close()


def test_ensemble_web_assets_are_loaded():
    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "character_memory" / "web"
    index = (web / "index.html").read_text(encoding="utf-8")
    script = (web / "ensemble.js").read_text(encoding="utf-8")
    css = (web / "ensemble.css").read_text(encoding="utf-8")

    assert "/static/ensemble.js" in index
    assert "/static/ensemble.css" in index
    for token in [
        "/v1/ensembles/prepare",
        "/confirm",
        "data-ensemble-member",
        "确认并开始群聊",
        "groups?.enter",
    ]:
        assert token in script
    for token in [
        ".ensemble-builder",
        ".ensemble-member-card",
        ".ensemble-capacity",
        ".character-overflow-entry",
        ".character-overflow-card",
    ]:
        assert token in css
