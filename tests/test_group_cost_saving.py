from types import SimpleNamespace

import pytest

from test_group_turn_granularity import build_service, MEMBERS


def test_saving_mode_restores_original_limit_and_preserves_named_members(tmp_path):
    from character_memory.application.group_conversation_service import effective_group_speaker_limit
    settings=SimpleNamespace(group_max_speakers_per_turn=5, cost_saving_enabled=False,
                             cost_saving_group_max_speakers_per_turn=3)
    service,model=build_service(tmp_path,MEMBERS,reactions=20)
    group=service.create_group('节省测试',MEMBERS)
    try:
        assert effective_group_speaker_limit(settings)==5
        settings.cost_saving_enabled=True
        assert effective_group_speaker_limit(settings)==3
        first=service.send(group.id,'普通聊天',max_speakers=effective_group_speaker_limit(settings))
        assert first['speaker_order']==['a','b','c']
        assert len(model.contexts)==3
        named=service.send(group.id,'@所有人',mentions=['*'],max_speakers=effective_group_speaker_limit(settings))
        assert set(named['speaker_order'])==set(MEMBERS)
        assert named['deferred_speaker_ids']==[]
        assert len(model.contexts)==9
        settings.cost_saving_enabled=False
        assert effective_group_speaker_limit(settings)==5
        restored=service.send(group.id,'恢复普通聊天',max_speakers=effective_group_speaker_limit(settings))
        assert restored['speaker_order']==['c','d','e','f','a']
        assert len(model.contexts)==14
        assert settings.group_max_speakers_per_turn==5
        settings.group_max_speakers_per_turn=2
        settings.cost_saving_enabled=True
        assert effective_group_speaker_limit(settings)==2
    finally:
        service.store.close()


def test_saving_settings_are_optional_validated_and_restart_required():
    from character_memory.config import Settings
    from character_memory.settings_store import resolved_schema
    settings=Settings()
    assert settings.cost_saving_enabled is False
    assert settings.cost_saving_group_max_speakers_per_turn==3
    for invalid in (0,13):
        with pytest.raises(ValueError): Settings(cost_saving_group_max_speakers_per_turn=invalid)
    fields={field['name']:field for section in resolved_schema() for field in section['fields']}
    for key in ('cost_saving_enabled','cost_saving_group_max_speakers_per_turn'):
        assert fields[key]['help'] and fields[key]['restart_required']


@pytest.mark.parametrize('saving,mentions,expected', [
    (False, [], 5), (True, [], 3), (True, ['*'], 6),
])
def test_sync_http_consumes_same_limit_as_async_without_extra_models(tmp_path,saving,mentions,expected):
    import threading
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from character_memory.group_web import attach_group_routes
    service,model=build_service(tmp_path,MEMBERS,reactions=10)
    group=service.create_group('HTTP群聊',MEMBERS)
    profiles=[{'id':member,'name':member.upper(),'persona_path':str(tmp_path/'persona.yaml')} for member in MEMBERS]
    lock=threading.RLock()
    bundle=SimpleNamespace(store=service.store,runtimes=service.runtimes,clock=service.clock,
        chat=SimpleNamespace(_lock_for=lambda _id:lock),settings=SimpleNamespace(group_max_speakers_per_turn=5,
        cost_saving_enabled=saving,cost_saving_group_max_speakers_per_turn=3))
    app=FastAPI()
    app.state.character_memory=SimpleNamespace(store=lambda:service.store,get_bundle=lambda:bundle,
        character_profiles=lambda:profiles,global_sticker_catalog=lambda:None,
        refresh_runtime_sticker_catalog=lambda _:None)
    attach_group_routes(app)
    try:
        with TestClient(app) as client:
            response=client.post(f'/v1/groups/{group.id}/chat',json={'message':'测试','mentions':mentions})
            assert response.status_code==200,response.text
            result=response.json()
            assert len(result['speaker_order'])==expected
            assert len(model.contexts)==expected
            assert result['max_speakers']==(3 if saving else 5)
            assert len(result['deferred_speaker_ids'])==len(MEMBERS)-expected
    finally:
        service.store.close()
