from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import character_memory.ensemble_builder as ensemble_module
from character_memory.ensemble_builder import (
    EnsembleBuilderService,
    EnsembleMemberResearch,
    EnsembleRepository,
    EnsembleResearch,
    member_research_to_persona,
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
    creation_records = []

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

    def create_character(
        draft,
        requested_id="",
        *,
        confirm_over_soft_limit=False,
        skip_capacity_check=False,
        creation=None,
    ):
        character_id = draft.name.lower()
        profile = {"id": character_id, "name": draft.name, "identity": draft.identity}
        profiles.append(profile)
        created.append(character_id)
        creation_records.append({"character_id": character_id, **(creation or {})})
        return profile

    def rollback(character_id):
        profiles[:] = [item for item in profiles if item["id"] != character_id]
        if character_id in created:
            created.remove(character_id)

    access.check_character_capacity = check_capacity
    access.create_character_from_draft = create_character
    access.rollback_created_character = rollback
    access.creation_records = creation_records
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
    assert [item["source"] for item in access.creation_records] == [
        "ENSEMBLE_BUILDER",
        "ENSEMBLE_BUILDER",
    ]
    assert all(item["group_id"] == result["group_id"] or item["group_id"] == started["group_id"] for item in access.creation_records)
    assert all("LAB MEM" in item["prompt"] for item in access.creation_records)
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
        assert "重试整理" in str(exc)

    assert GroupRepository(store).list_groups() == []
    store.close()


def test_age_text_is_weak_metadata_and_does_not_block_persona_creation():
    member = EnsembleMemberResearch(
        name="Kurisu",
        age="18岁（东京电机大学一年级）",
        identity="研究者",
        description="理性、反应快，对荒唐说法会直接吐槽，也会认真维护自己的专业判断和边界。",
        speech_style="清晰直接，常会指出逻辑问题。",
        personality=["理性", "直接"],
        relationship_notes="经常与 Okabe 争论；重视专业判断",
        tags="研究、吐槽",
    )

    draft = member_research_to_persona(member)

    assert draft.age == 18
    assert draft.name == "Kurisu"
    assert "清晰直接" in draft.conversation
    assert "理性" in draft.personality


def test_unknown_age_is_allowed_and_character_traits_remain_primary():
    member = EnsembleMemberResearch(
        name="Mystery",
        age="年龄不详",
        identity="身份明确但年龄没有可靠公开资料",
        description="说话克制、观察细致，有自己的价值判断，不会为了配合别人随意改变立场。",
        speech_style="短句、节奏慢、语气平静。",
        tags=["克制", "细致"],
    )

    draft = member_research_to_persona(member)

    assert draft.age is None
    assert draft.identity.startswith("身份明确")
    assert draft.conversation.startswith("短句")


def test_prepare_failure_preserves_failed_build_for_retry(tmp_path):
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
        rows = store.conn.execute(
            "SELECT group_id,status,error FROM ensemble_builds"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "FAILED"
    assert "公开资料" in rows[0]["error"]
    store.close()


def test_prepare_route_returns_failed_build_without_raw_502_detail(tmp_path):
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

    assert response.status_code == 200
    build = response.json()["build"]
    assert build["status"] == "BUILDING" or build["status"] == "FAILED"
    # The product response must not dump provider/Pydantic internals into the UI.
    assert "media host could not be resolved" not in json.dumps(response.json(), ensure_ascii=False)
    with store._lock:
        total = store.conn.execute(
            "SELECT COUNT(*) AS total FROM ensemble_builds"
        ).fetchone()["total"]
    assert total == 1
    store.close()


def test_one_failed_member_does_not_fail_the_whole_ensemble(tmp_path, monkeypatch):
    access, store, _, _, _ = _access(tmp_path)
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    original = ensemble_module.member_research_to_persona

    def fail_mayuri(member):
        if member.name == "Mayuri":
            raise ValueError("simulated malformed member")
        return original(member)

    monkeypatch.setattr(ensemble_module, "member_research_to_persona", fail_mayuri)
    started = service.start("复刻命运石之门的 LAB MEM，并形成群聊")
    build = service.research(started["group_id"])

    assert build["status"] == "READY"
    assert build["ready_member_count"] == 2
    assert [item["canonical_name"] for item in build["failed_members"]] == ["Mayuri"]
    ready = [item for item in build["drafts"] if item.get("status") == "READY"]
    assert {item["canonical_name"] for item in ready} == {"Kurisu", "Okabe"}
    store.close()


def test_failed_member_can_be_retried_without_rerunning_group_research(tmp_path, monkeypatch):
    access, store, observer, _, _ = _access(tmp_path)
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    original = ensemble_module.member_research_to_persona
    failed_once = {"value": False}

    def fail_once(member):
        if member.name == "Mayuri" and not failed_once["value"]:
            failed_once["value"] = True
            raise ValueError("temporary member conversion failure")
        return original(member)

    monkeypatch.setattr(ensemble_module, "member_research_to_persona", fail_once)
    started = service.start("复刻命运石之门的 LAB MEM，并形成群聊")
    build = service.research(started["group_id"])
    observation_calls = len(observer.calls)

    retried = service.retry_member(started["group_id"], 2)

    assert len(observer.calls) == observation_calls
    assert retried["ready_member_count"] == 3
    assert retried["failed_members"] == []
    assert retried["drafts"][2]["status"] == "READY"
    store.close()


def test_voice_design_is_explicit_opt_in_and_never_part_of_default_confirm(tmp_path):
    access, store, _, _, _ = _access(tmp_path)
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    started = service.start("复刻命运石之门的 LAB MEM，并形成群聊")
    ready = service.research(started["group_id"])
    calls = []
    service._start_voice_design = lambda items: calls.append(list(items))

    service.confirm(started["group_id"], [0, 1], use_voice_design=False)

    assert calls == []
    store.close()


def test_voice_design_opt_in_schedules_best_effort_work_after_group_commit(tmp_path):
    access, store, _, _, _ = _access(tmp_path)
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    started = service.start("复刻命运石之门的 LAB MEM，并形成群聊")
    ready = service.research(started["group_id"])
    calls = []
    service._start_voice_design = lambda items: calls.append(list(items))

    result = service.confirm(started["group_id"], [0, 1], use_voice_design=True)

    assert result["status"] == "ACTIVE"
    assert len(calls) == 1
    # Kurisu already exists in the fixture, so only the newly-created Okabe
    # gets optional VoiceDesign work.
    assert [character_id for character_id, _draft in calls[0]] == ["okabe"]
    store.close()


def test_voice_design_scheduling_failure_does_not_rollback_group(tmp_path):
    access, store, _, _, _ = _access(tmp_path)
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    started = service.start("复刻命运石之门的 LAB MEM，并形成群聊")
    service.research(started["group_id"])

    def fail_schedule(_items):
        raise RuntimeError("thread unavailable")

    service._start_voice_design = fail_schedule
    result = service.confirm(started["group_id"], [0, 1], use_voice_design=True)

    assert result["status"] == "ACTIVE"
    assert result["group"]["status"] == "ACTIVE"
    assert result["group"]["member_ids"] == ["kurisu", "okabe"]
    store.close()


def test_voice_design_instruction_uses_traits_not_exact_age(tmp_path):
    access, store, _, _, _ = _access(tmp_path)
    service = EnsembleBuilderService(access, EnsembleRepository(store))
    draft = member_research_to_persona(
        EnsembleMemberResearch(
            name="Kurisu",
            age="18岁（大学一年级）",
            identity="研究者",
            description="理性、敏锐，对研究和事实有很强的专业判断，也会明确表达自己的边界。",
            speech_style="清晰直接，节奏自然。",
            personality=["理性", "敏锐"],
        )
    )

    instruct = service._voice_design_instruction(draft)

    assert "理性" in instruct
    assert "清晰直接" in instruct
    assert "18" not in instruct
    assert "不要依赖精确年龄数字" in instruct
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
        "/members/",
        "data-ensemble-member",
        "data-ensemble-voice-design",
        "use_voice_design",
        "确认并开始群聊",
        "groups?.enter",
    ]:
        assert token in script
    for token in [
        ".ensemble-builder",
        ".ensemble-member-card",
        ".ensemble-member-failed",
        ".ensemble-voice-option",
        ".ensemble-capacity",
        ".character-overflow-entry",
        ".character-overflow-card",
    ]:
        assert token in css
