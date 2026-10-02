from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import character_memory.ensemble_builder as ensemble_module
from character_memory.character_onboarding import save_creation_metadata
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
        if not skip_capacity_check:
            check_capacity(
                1,
                confirm_over_soft_limit=confirm_over_soft_limit,
            )
        character_id = draft.name.lower()
        persona_path = tmp_path / "personas" / character_id / "persona.yaml"
        persona_path.parent.mkdir(parents=True, exist_ok=True)
        persona_path.write_text(
            f"id: {character_id}\nname: {draft.name}\n",
            encoding="utf-8",
        )
        save_creation_metadata(
            persona_path,
            creation=creation,
            initialization={"avatar": {"status": "ready"}},
        )
        profile = {
            "id": character_id,
            "name": draft.name,
            "identity": draft.identity,
            "persona_path": str(persona_path),
        }
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
    # The description is the query. It used to carry a trailing
    # "角色 成员 人物 资料 wiki", which steered the ranking at a source family
    # this network cannot retrieve instead of letting the engine choose sources.
    assert observer.calls[0]["query"] == "复刻命运石之门的 LAB MEM，并形成群聊"
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
    for character_id in created:
        creation = json.loads(
            (tmp_path / "personas" / character_id / "creation.json").read_text(
                encoding="utf-8"
            )
        )
        assert creation["group_id"] == result["group_id"]
    store.close()


def test_confirmation_rechecks_stale_existing_character_matches(tmp_path):
    access, store, _, profiles, created = _access(tmp_path)
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    started = service.start("复刻命运石之门的 LAB MEM，并形成群聊")
    service.research(started["group_id"])

    # A READY build may outlive a character it matched during research.
    profiles[:] = [item for item in profiles if item["id"] != "kurisu"]

    result = service.confirm(started["group_id"], [0, 1])

    current_ids = {item["id"] for item in profiles}
    assert set(result["group"]["member_ids"]) <= current_ids
    assert created == ["kurisu", "okabe"]
    store.close()


def test_confirmation_rechecks_capacity_at_each_character_write(tmp_path):
    access, store, _, profiles, created = _access(tmp_path)
    for index in range(17):
        profiles.append(
            {"id": f"extra-{index}", "name": f"Extra {index}", "identity": "测试"}
        )
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    started = service.start("复刻命运石之门的 LAB MEM，并形成群聊")
    service.research(started["group_id"])

    original_create = access.create_character_from_draft
    inserted = False

    def create_with_concurrent_character(*args, **kwargs):
        nonlocal inserted
        profile = original_create(*args, **kwargs)
        if not inserted:
            inserted = True
            profiles.append(
                {"id": "concurrent", "name": "Concurrent", "identity": "并发创建"}
            )
        return profile

    access.create_character_from_draft = create_with_concurrent_character

    with pytest.raises(ValueError, match="hard limit"):
        service.confirm(
            started["group_id"],
            [0, 1, 2],
            confirm_over_soft_limit=True,
        )

    assert GroupRepository(store).list_groups() == []
    assert "okabe" not in {item["id"] for item in profiles}
    assert "mayuri" not in {item["id"] for item in profiles}
    assert created == []
    store.close()


def test_duplicate_concurrent_confirmation_commits_once_and_keeps_build_alias(tmp_path):
    access, store, _, _, created = _access(tmp_path)
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    started = service.start("复刻命运石之门的 LAB MEM，并形成群聊")
    service.research(started["group_id"])

    original_create = access.create_character_from_draft
    first_create_started = threading.Event()
    release_first_create = threading.Event()

    def slow_first_create(*args, **kwargs):
        if not first_create_started.is_set():
            first_create_started.set()
            assert release_first_create.wait(timeout=5)
        return original_create(*args, **kwargs)

    access.create_character_from_draft = slow_first_create
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.confirm, started["group_id"], [0, 1])
        assert first_create_started.wait(timeout=5)
        duplicate = executor.submit(service.confirm, started["group_id"], [0, 1])
        release_first_create.set()
        first_result = first.result(timeout=5)
        duplicate_result = duplicate.result(timeout=5)

    assert first_result["group_id"] == duplicate_result["group_id"]
    assert first_result["status"] == duplicate_result["status"] == "ACTIVE"
    assert created == ["okabe"]
    assert len(GroupRepository(store).list_groups()) == 1
    assert repository.get(started["group_id"])["group_id"] == first_result["group_id"]
    assert service._confirm_locks == {}
    assert service._confirm_lock_users == {}
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


def test_researched_interaction_styles_survive_local_persona_projection():
    direct = member_research_to_persona(
        EnsembleMemberResearch(
            name="Direct",
            identity="直率的研究者",
            description="重事实、反应快，遇到逻辑问题会直接指出，也愿意承担冲突带来的后果。",
            speech_style="短句，直接指出结论和证据。",
            personality=["直接", "理性"],
            expression_style="情绪明显时也先讲事实，再补一句自己的感受。",
            question_style="只追问会改变判断的关键信息，不用寒暄式问题拖长对话。",
            silence_style="信息不足时会先观察，不会为了热闹随便接话。",
            initiative_style="发现明显漏洞或风险时会主动打断并指出。",
            disagreement_style="不同意时直接指出哪一步推理不成立，并给出自己的依据。",
            care_style="更常通过解决具体问题和提前提醒风险来表达关心。",
            boundaries=["不接受用撒娇绕过事实问题", "不会为了合群假装赞同"],
        )
    )
    gentle = member_research_to_persona(
        EnsembleMemberResearch(
            name="Gentle",
            identity="温和的长期朋友",
            description="更关注人的情绪和关系变化，但并不没有主见，会用自己的方式坚持重要事情。",
            speech_style="语速慢，先回应情绪，再讲自己的看法。",
            personality=["温和", "细致"],
            expression_style="先说自己注意到的细节，再自然表达情绪，不用夸张语气。",
            question_style="察觉对方情绪变化时会轻轻确认一次，得到回应后不连续追问。",
            silence_style="气氛紧张时愿意陪着沉默，不急着填满空白。",
            initiative_style="看到熟悉的人持续低落时会主动提起共同经历或邀请一起做点小事。",
            disagreement_style="不同意时先承认对方感受，再平静说明自己不会跟着做的原因。",
            care_style="通过陪伴、记住偏好和准备小事来表达关心。",
            boundaries=["不会替别人做重大决定", "不把温和等同于无条件答应"],
        )
    )

    assert direct.questions != gentle.questions
    assert direct.silence != gentle.silence
    assert direct.initiative != gentle.initiative
    assert direct.disagreement != gentle.disagreement
    assert direct.care != gentle.care
    assert direct.boundaries != gentle.boundaries
    assert "推理" in direct.disagreement
    assert "陪着沉默" in gentle.silence


def test_persona_projection_clamps_interaction_styles_and_pads_boundaries():
    long_style = "风格" * 200
    draft = member_research_to_persona(
        EnsembleMemberResearch(
            name="BoundaryCase",
            identity="边界测试人物",
            description="这是一个用于验证长交互风格和边界数量兼容性的角色描述，长度足够通过研究模型校验。",
            expression_style=long_style,
            question_style=long_style,
            silence_style=long_style,
            initiative_style=long_style,
            disagreement_style=long_style,
            care_style=long_style,
            boundaries=["保留这一条自定义边界"],
        )
    )

    for field in ("expression", "questions", "silence", "initiative", "disagreement", "care"):
        assert len(getattr(draft, field)) == 320
    assert draft.boundaries[0] == "保留这一条自定义边界"
    assert len(draft.boundaries) >= 2


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


def test_routes_recover_interrupted_build_and_expose_it_for_resume(tmp_path):
    access, store, _, _, _ = _access(tmp_path)
    repository = EnsembleRepository(store)
    started = EnsembleBuilderService(access, repository).start(
        "复刻命运石之门的 LAB MEM，并形成群聊",
        now=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
    )
    assert started["status"] == "BUILDING"

    # Route attachment represents the next process startup: no in-memory task
    # can still own a BUILDING record from the previous process.
    app = FastAPI()
    app.state.character_memory = access
    attach_ensemble_routes(app)

    with TestClient(app) as client:
        response = client.get("/v1/ensembles")

    assert response.status_code == 200
    build = response.json()["build"]
    assert build["group_id"] == started["group_id"]
    assert build["status"] == "FAILED"
    assert "服务重启" in build["error"]
    store.close()


def test_latest_build_does_not_resurface_an_older_failed_attempt(tmp_path):
    access, store, _, _, _ = _access(tmp_path)
    repository = EnsembleRepository(store)
    service = EnsembleBuilderService(access, repository)
    failed = service.start(
        "第一次失败的群像资料整理",
        now=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
    )
    repository.set_status(
        failed["group_id"],
        "FAILED",
        datetime(2026, 9, 22, 12, 1, tzinfo=timezone.utc),
        error="provider unavailable",
    )
    cancelled = service.start(
        "第二次随后被用户取消的群像资料整理",
        now=datetime(2026, 9, 22, 12, 2, tzinfo=timezone.utc),
    )
    service.cancel(
        cancelled["group_id"],
        now=datetime(2026, 9, 22, 12, 3, tzinfo=timezone.utc),
    )

    assert repository.latest_resumable() is None
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
        'CM.api("/v1/ensembles")',
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
