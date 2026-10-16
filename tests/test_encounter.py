from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
import threading
import time

import pytest

from character_memory.encounter import EncounterScheduler, EncounterService
from character_memory.encounter_store import EncounterRepository
from character_memory.storage.sqlite import SQLiteStore


def draft():
    return {
        "name": "Mika",
        "age": 23,
        "identity": "夜间电器维修店的店员",
        "tagline": "会把坏掉的小东西拆开看看",
        "description": "Mika 习惯在夜里修理旧电子设备，观察细，熟悉以后会突然讲很多冷门零件。",
        "personality": ["安静", "好奇", "有自己的判断"],
        "conversation": "自然短句。",
        "expression": "很少刷表情。",
        "questions": "真的好奇才问。",
        "silence": "没话时可以沉默。",
        "initiative": "遇到旧设备会主动分享。",
        "disagreement": "不同意会直接说理由。",
        "care": "记住具体小事。",
        "boundaries": ["不迎合", "关系慢慢形成"],
    }


@pytest.mark.parametrize('source', ['GENERATED', 'WEB'])
@pytest.mark.parametrize('preference', [None, '不限制性别，偏好安静的原创成年人物'])
def test_new_candidate_preference_reaches_both_generation_paths_without_extra_calls(tmp_path, monkeypatch, source, preference):
    from character_memory.config import Settings
    from character_memory.persona_builder import PersonaDraft
    from character_memory.encounter import EncounterWebSeed
    settings = Settings()
    if preference is not None:
        settings.encounter_persona_preference = preference
    else:
        assert '成年女性' in settings.encounter_persona_preference
        assert '18–25' in settings.encounter_persona_preference
        assert '可爱、活泼、有活力' in settings.encounter_persona_preference
    expected = settings.encounter_persona_preference
    descriptions, seed_prompts = [], []
    def generate(description, **kwargs):
        descriptions.append(description)
        return PersonaDraft.model_validate(draft())
    def seed(prompt, *args):
        seed_prompts.append(prompt)
        return EncounterWebSeed(inspiration_description='一位喜欢修理老设备、生活节奏独特且有自己边界的原创人物。', suggested_name='Mika', tags=['修理'])
    observation = SimpleNamespace(title='修理日常', url='https://example.org/repair', source_domain='example.org',
        snippet='修理旧电器', content='一篇谈论维修爱好的公开文章。')
    access = SimpleNamespace(settings=settings, require_bundle=lambda: SimpleNamespace(model=SimpleNamespace(structured_for_session=seed)),
        world_observer=SimpleNamespace(observe=lambda *args, **kwargs: {'observations':[observation]}))
    store = SQLiteStore(str(tmp_path / 'preference.db'))
    service = EncounterService(access, EncounterRepository(store))
    monkeypatch.setattr(service, '_persona_builder', lambda: SimpleNamespace(generate=generate))
    now = datetime.now(timezone.utc)
    if source == 'WEB':
        service._web_draft(now)
        assert len(seed_prompts) == 1 and expected in seed_prompts[0]
    else:
        service._generated_draft(now)
        assert seed_prompts == []
    assert len(descriptions) == 1 and expected in descriptions[0]
    store.close()


def test_candidate_portrait_is_cached_concurrent_and_restart_safe(tmp_path):
    from character_memory.avatars import AvatarStore
    from character_memory.visual_generation import ImageGenerationResult
    from concurrent.futures import ThreadPoolExecutor

    store = SQLiteStore(str(tmp_path / 'portrait.db'))
    repo = EncounterRepository(store)
    candidate = repo.create_candidate(source_type='GENERATED', draft=draft(),
        encounter_hook='修理铺的客人', opening_message='你好', now=datetime.now(timezone.utc))
    calls = []
    def generate(request):
        calls.append(request)
        time.sleep(.02)
        return ImageGenerationResult(provider='test', model='test', payload=b'portrait', mime_type='image/webp')
    avatars = AvatarStore(tmp_path / 'avatars')
    access = SimpleNamespace(settings=SimpleNamespace(encounter_portraits_enabled=True, image_generation_provider='test'),
        services=SimpleNamespace(avatar_store=avatars, image_generation_providers={'test': SimpleNamespace(
            configured=lambda: True, available=lambda: True, generate=generate)}))
    service = EncounterService(access, repo)
    assert service.portrait_url(candidate['id']) == ''
    assert calls == []  # Reading/listing never purchases an image.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(service.ensure_portrait, [candidate['id']] * 2))
    assert results[0] == results[1]
    assert len(calls) == 1
    assert calls[0].aspect_ratio == '3:4'
    assert 'Mika' in calls[0].prompt
    restarted = EncounterService(access, EncounterRepository(store))
    assert restarted.ensure_portrait(candidate['id']) == results[0]
    assert len(calls) == 1
    assert repo.pending_count() == 1
    assert avatars.asset_path(f"encounter-{candidate['id']}") is None
    assert service.portraits.asset_path(f"encounter-{candidate['id']}").read_bytes() == b'portrait'
    assert repo.get_candidate(candidate['id'])['status'] == 'NEW'
    avatars.close()
    store.close()


def test_candidate_portrait_failure_does_not_create_asset_or_change_lifecycle(tmp_path):
    from character_memory.avatars import AvatarStore
    store = SQLiteStore(str(tmp_path / 'portrait.db'))
    repo = EncounterRepository(store)
    candidate = repo.create_candidate(source_type='GENERATED', draft=draft(), encounter_hook='客人',
        opening_message='你好', now=datetime.now(timezone.utc))
    avatars = AvatarStore(tmp_path / 'avatars')
    def fail(request):
        raise RuntimeError('provider unavailable')
    access = SimpleNamespace(settings=SimpleNamespace(encounter_portraits_enabled=True, image_generation_provider='test'),
        services=SimpleNamespace(avatar_store=avatars, image_generation_providers={'test': SimpleNamespace(
            configured=lambda: True, available=lambda: True, generate=fail)}))
    service = EncounterService(access, repo)
    with pytest.raises(RuntimeError, match='provider unavailable'):
        service.ensure_portrait(candidate['id'])
    assert service.portrait_url(candidate['id']) == ''
    assert repo.get_candidate(candidate['id'])['status'] == 'NEW'
    access.settings.encounter_portraits_enabled = False
    with pytest.raises(ValueError, match='关闭'):
        service.ensure_portrait(candidate['id'])
    with pytest.raises(KeyError):
        service.ensure_portrait(999)
    avatars.close()
    store.close()


def test_encounter_candidate_pool_and_trial_messages_are_durable(tmp_path):
    store = SQLiteStore(str(tmp_path / "x.db"))
    repo = EncounterRepository(store)
    now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)

    candidate = repo.create_candidate(
        source_type="WEB",
        draft=draft(),
        encounter_hook="在一篇旧电器维修记录里得到的灵感。",
        opening_message="这个接口你还要用吗？我刚拆下来。",
        source_query="repair cafe vintage electronics hobby story",
        source_urls=["https://example.org/repair"],
        source_domains=["example.org"],
        now=now,
    )
    assert candidate["status"] == "NEW"
    assert candidate["source_type"] == "WEB"
    assert repo.pending_count() == 1

    repo.append_message(candidate["id"], "USER", "你修的是什么？", now)
    repo.append_message(candidate["id"], "CHARACTER", "一台很老的随身听。", now)
    assert [item["role"] for item in repo.list_messages(candidate["id"])] == ["USER", "CHARACTER"]

    repo.set_status(candidate["id"], "CHATTING", now)
    assert repo.get_candidate(candidate["id"])["status"] == "CHATTING"
    store.close()


def test_full_chat_list_does_not_close_or_delete_encounter(tmp_path):
    store = SQLiteStore(str(tmp_path / "x.db"))
    repo = EncounterRepository(store)
    now = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)
    candidate = repo.create_candidate(
        source_type="GENERATED",
        draft=draft(),
        encounter_hook="偶然遇见。",
        opening_message="……你也是来找零件的吗？",
        now=now,
    )

    def full_creator(*_args, **_kwargs):
        raise ValueError("角色已达到容量上限：当前 20 位，本次新增 1 位，最多 20 位。")

    access = SimpleNamespace(
        create_character_from_draft=full_creator,
        store=lambda: store,
    )
    service = EncounterService(access, repo)
    with pytest.raises(ValueError, match="最多 20 位"):
        service.accept(candidate["id"], now=now)

    # The candidate remains available for trial chat/dismissal instead of being
    # destroyed merely because the formal chat list is full.
    assert repo.get_candidate(candidate["id"])["status"] == "NEW"
    store.close()


def test_scheduler_skips_creation_when_pending_pool_is_full(tmp_path):
    store = SQLiteStore(str(tmp_path / "x.db"))
    repo = EncounterRepository(store)
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    repo.create_candidate(
        source_type="GENERATED",
        draft=draft(),
        encounter_hook="已有候选。",
        opening_message="你好。",
        now=now,
    )
    settings = SimpleNamespace(
        api_key="fake",
        encounter_enabled=True,
        encounter_interval_minutes=30,
        encounter_poll_seconds=60,
        encounter_web_probability=0.5,
        encounter_max_pending=1,
    )
    scheduler = EncounterScheduler(SimpleNamespace(settings=settings), repo)
    scheduler.force_due(now=now)
    result = scheduler.run_once(now=now)
    assert result[0]["status"] == "SKIPPED_PENDING"
    assert repo.pending_count() == 1
    store.close()


def _chat_candidate(repo, now):
    return repo.create_candidate(
        source_type="GENERATED",
        draft=draft(),
        encounter_hook="偶然遇见。",
        opening_message="……你也是来找零件的吗？",
        now=now,
    )


class _RecordingModel:
    """Returns one canned line per call and counts the calls."""

    def __init__(self, message="……嗯，我还在。"):
        self.message = message
        self.calls = 0
        self.sessions = []

    def structured_for_session(self, prompt, schema, session_id):
        self.calls += 1
        self.sessions.append(session_id)
        return SimpleNamespace(message=self.message)


class _DismissDuringReplyModel(_RecordingModel):
    def __init__(self, repo, candidate_id, now):
        super().__init__("这句已经来迟了。")
        self.repo = repo
        self.candidate_id = candidate_id
        self.now = now

    def structured_for_session(self, prompt, schema, session_id):
        self.repo.set_status(self.candidate_id, "DISMISSED", self.now)
        return super().structured_for_session(prompt, schema, session_id)


def test_a_message_is_stored_before_the_character_answers(tmp_path):
    """The send no longer waits on the model, so the two halves are separable."""

    store = SQLiteStore(str(tmp_path / "x.db"))
    repo = EncounterRepository(store)
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    candidate = _chat_candidate(repo, now)

    model = _RecordingModel()
    access = SimpleNamespace(require_bundle=lambda: SimpleNamespace(model=model))
    service = EncounterService(access, repo)

    posted = service.post_message(candidate["id"], "你还在吗？", now=now)
    assert model.calls == 0
    assert [item["role"] for item in posted["messages"]] == ["USER"]
    # The panel is rendered open from the status, so the status has to move with
    # the message rather than with the reply.
    assert repo.get_candidate(candidate["id"])["status"] == "CHATTING"

    answered = service.reply(candidate["id"], now=now)
    assert model.calls == 1
    assert model.sessions == [f"encounter-chat:{candidate['id']}"]
    assert [item["role"] for item in answered["messages"]] == ["USER", "CHARACTER"]
    assert answered["message"]["content"] == "……嗯，我还在。"
    store.close()


def test_a_closed_encounter_is_never_answered(tmp_path):
    """A reply that arrives after the person moved on is dropped, not forced."""

    store = SQLiteStore(str(tmp_path / "x.db"))
    repo = EncounterRepository(store)
    now = datetime(2026, 9, 22, 12, 30, tzinfo=timezone.utc)
    candidate = _chat_candidate(repo, now)

    model = _RecordingModel()
    access = SimpleNamespace(require_bundle=lambda: SimpleNamespace(model=model))
    service = EncounterService(access, repo)

    service.post_message(candidate["id"], "还在吗？", now=now)
    service.dismiss(candidate["id"], now=now)
    assert service.reply(candidate["id"], now=now) is None
    assert model.calls == 0
    store.close()


def test_reply_that_finishes_after_dismiss_is_dropped(tmp_path):
    """Closing during the model call must win over its late result."""

    store = SQLiteStore(str(tmp_path / "x.db"))
    repo = EncounterRepository(store)
    now = datetime(2026, 9, 22, 12, 45, tzinfo=timezone.utc)
    candidate = _chat_candidate(repo, now)
    model = _DismissDuringReplyModel(repo, candidate["id"], now)
    service = EncounterService(
        SimpleNamespace(require_bundle=lambda: SimpleNamespace(model=model)),
        repo,
    )

    service.post_message(candidate["id"], "还在吗？", now=now)
    assert service.reply(candidate["id"], now=now) is None
    assert repo.get_candidate(candidate["id"])["status"] == "DISMISSED"
    assert [item["role"] for item in repo.list_messages(candidate["id"])] == ["USER"]
    store.close()


def test_concurrent_accept_creates_only_one_formal_character(tmp_path):
    """Two HTTP workers accepting one card must share one lifecycle claim."""

    store = SQLiteStore(str(tmp_path / "x.db"))
    repo = EncounterRepository(store)
    now = datetime(2026, 9, 22, 12, 50, tzinfo=timezone.utc)
    candidate = _chat_candidate(repo, now)
    first_entered = threading.Event()
    release_first = threading.Event()
    created = []

    def creator(*_args, **_kwargs):
        created.append(f"formal-{len(created) + 1}")
        if len(created) == 1:
            first_entered.set()
            release_first.wait(timeout=2)
        return {"id": created[-1]}

    service = EncounterService(
        SimpleNamespace(create_character_from_draft=creator, store=lambda: store),
        repo,
    )
    results = []
    errors = []

    def accept():
        try:
            results.append(service.accept(candidate["id"], now=now))
        except Exception as exc:  # pragma: no cover - assertion reports the exception
            errors.append(exc)

    first = threading.Thread(target=accept)
    second = threading.Thread(target=accept)
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    time.sleep(0.05)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert errors == []
    assert created == ["formal-1"]
    assert {item["accepted_character_id"] for item in results} == {"formal-1"}
    store.close()


def test_scheduler_answers_queued_messages_and_survives_a_failure(tmp_path):
    """Queued replies run on the worker, and a broken one never escapes it."""

    store = SQLiteStore(str(tmp_path / "x.db"))
    repo = EncounterRepository(store)
    now = datetime(2026, 9, 22, 13, 0, tzinfo=timezone.utc)
    candidate = _chat_candidate(repo, now)

    model = _RecordingModel("我这边刚把外壳装回去。")
    access = SimpleNamespace(
        settings=SimpleNamespace(),
        require_bundle=lambda: SimpleNamespace(model=model),
    )
    scheduler = EncounterScheduler(access, repo)

    assert scheduler.enqueue_reply(candidate["id"], now=now) is True
    assert scheduler.pending_reply_count() == 1
    answered = scheduler.drain_pending_replies()
    assert scheduler.pending_reply_count() == 0
    assert [item["role"] for item in answered[0]["messages"]] == ["CHARACTER"]
    assert answered[0]["message"]["content"] == "我这边刚把外壳装回去。"

    def explode(candidate_id, *, now=None):
        raise RuntimeError("provider exploded")

    scheduler.service.reply = explode
    assert scheduler.enqueue_reply(candidate["id"], now=now) is True
    assert scheduler.drain_pending_replies() == []
    assert scheduler.pending_reply_count() == 0
    store.close()


def test_scheduler_recovers_a_durable_user_line_after_restart(tmp_path):
    store = SQLiteStore(str(tmp_path / "x.db"))
    repo = EncounterRepository(store)
    now = datetime(2026, 9, 22, 13, 15, tzinfo=timezone.utc)
    candidate = _chat_candidate(repo, now)
    model = _RecordingModel("重启后也没有丢掉这次回答。")
    access = SimpleNamespace(
        settings=SimpleNamespace(),
        require_bundle=lambda: SimpleNamespace(model=model),
    )

    # The request process stored the user line and then stopped before its
    # process-local queue could be drained.
    EncounterService(access, repo).post_message(candidate["id"], "刚才断开了吗？", now=now)
    restarted = EncounterScheduler(access, repo)
    assert restarted.pending_reply_count() == 0
    assert restarted.recover_pending_replies(now=now) == 1
    assert restarted.recover_pending_replies(now=now) == 0

    answered = restarted.drain_pending_replies()
    assert answered[0]["message"]["content"] == "重启后也没有丢掉这次回答。"
    assert repo.candidates_waiting_for_reply() == []
    store.close()


def test_encounter_ui_and_server_wiring_exist():
    root = Path(__file__).resolve().parents[1]
    server = (root / "src" / "character_memory" / "server.py").read_text(encoding="utf-8")
    index = (root / "src" / "character_memory" / "web" / "index.html").read_text(encoding="utf-8")
    script = (root / "src" / "character_memory" / "web" / "encounter.js").read_text(encoding="utf-8")
    world = (root / 'src/character_memory/web/world_v2.js').read_text(encoding='utf-8')

    assert "attach_encounter_routes" in server
    assert '/static/encounter.js' in index
    assert '/static/encounter.css' in index
    for token in [
        "/v1/encounters?limit=50",
        "active_character_soft_limit",
    ]:
        assert token in script
    for token in ('data-encounter-chat','data-encounter-accept','data-encounter-dismiss'):
        assert token in world


def test_new_user_line_supersedes_an_inflight_temporary_reply(tmp_path):
    store=SQLiteStore(str(tmp_path/'superseded.db'))
    try:
        repo=EncounterRepository(store)
        now=datetime.now(timezone.utc)
        candidate=_chat_candidate(repo,now)
        repo.append_open_message(candidate['id'],'USER','first',now)
        def reply(*args):
            repo.append_open_message(candidate['id'],'USER','newer',datetime.now(timezone.utc))
            return SimpleNamespace(message='obsolete')
        service=EncounterService(SimpleNamespace(require_bundle=lambda:SimpleNamespace(model=SimpleNamespace(structured_for_session=reply))),repo)
        assert service.reply(candidate['id']) is None
        assert repo.list_messages(candidate['id'])[-1]['content']=='newer'
        assert repo.candidates_waiting_for_reply()==[candidate['id']]
    finally:
        store.close()


def test_encounter_lifecycle_locks_do_not_collide_across_candidates(tmp_path):
    """A slow accept on candidate 1 must not block candidate 65."""

    store = SQLiteStore(str(tmp_path / "lock.db"))
    repo = EncounterRepository(store)
    first = repo.lifecycle_lock(1)
    assert first is repo.lifecycle_lock(1)
    unrelated = repo.lifecycle_lock(65)
    assert unrelated is not first

    acquired = threading.Event()

    def claim_unrelated():
        with repo.lifecycle_lock(65):
            acquired.set()

    with first:
        worker = threading.Thread(target=claim_unrelated)
        worker.start()
        assert acquired.wait(timeout=1), "unrelated candidate shares a lifecycle lock"
    worker.join(timeout=2)
    store.close()
