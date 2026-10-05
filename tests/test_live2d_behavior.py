import json
from datetime import datetime, timezone

from character_memory.domain.models import Event, EventType, PersonReaction
from character_memory.live2d_behavior import (
    Live2DPresentationRequest, bind_presentation, behavior_metadata,
    presentation_scope, presentation_schema, presentation_prompt,
)


def make_model(root):
    folder = root / "rin"
    folder.mkdir()
    for name in ("nod.motion3.json", "idle.motion3.json", "happy.exp3.json"):
        (folder / name).write_text("{}")
    (folder / "rin.model3.json").write_text(json.dumps({"Version":3,"FileReferences":{
        "Motions":{"Idle":[{"File":"idle.motion3.json"}],"Nod":[{"File":"nod.motion3.json"}]},
        "Expressions":[{"Name":"开心","File":"happy.exp3.json"},{"Name":"missing","File":"absent.exp3.json"}],
    }}))
    return folder


def event(cap=None, character_id="rin"):
    return Event(character_id=character_id,event_type=EventType.USER_MESSAGE,event_time=datetime.now(timezone.utc),content="你好",metadata={"live2d":cap} if cap else {})


def test_capabilities_are_derived_from_local_resources_and_revision(tmp_path):
    folder = make_model(tmp_path)
    from character_memory.live2d_web import model_capabilities
    caps = model_capabilities(tmp_path,"rin")
    assert caps["motions"] == ["Idle","Nod"]
    assert caps["expressions"] == ["开心"]
    req=Live2DPresentationRequest(token="call-1",character_id="rin",revision=caps["revision"])
    bound=bind_presentation(tmp_path,req,["rin"])
    assert bound["token"] == "call-1"
    assert bind_presentation(tmp_path,req,["other"]) is None
    (folder / "rin.model3.json").write_text('{"Version":3,"FileReferences":{}}')
    assert bind_presentation(tmp_path,req,["rin"]) is None


def test_schema_scope_and_hint_validation_do_not_affect_plain_replies():
    caps={"character_id":"rin","token":"call-1","revision":"a"*64,"motions":["Idle","Nod"],"expressions":["开心"]}
    assert presentation_schema() is PersonReaction
    assert presentation_prompt(event()) == ""
    with presentation_scope(event(caps)):
        schema=presentation_schema()
        reply=schema.model_validate({"actions":[{"type":"MESSAGE","message":"你好","live2d":{"motion":"Nod","expression":"开心"}}]})
        metadata=behavior_metadata(event(caps),reply.actions[0])
        assert metadata["live2d"]["motion"] == "Nod"
        assert metadata["live2d"]["token"] == "call-1"
        assert behavior_metadata(event(caps,"other"),reply.actions[0]) == {}
        bad=schema.model_validate({"actions":[{"type":"MESSAGE","message":"仍然正常回复","live2d":{"motion":"MadeUp","expression":"missing"}}]})
        assert behavior_metadata(event(caps),bad.actions[0]) == {}
        malformed=schema.model_validate({"actions":[{"type":"MESSAGE","message":"继续回复","live2d":99}]})
        assert malformed.actions[0].message == "继续回复"
    assert presentation_schema() is PersonReaction


def test_formal_provider_uses_one_reply_call_with_optional_presentation():
    from character_memory.llm.client import OpenAICompatibleModel
    caps={"character_id":"rin","token":"call-1","revision":"a"*64,"motions":["Nod"],"expressions":["开心"]}
    calls=[]
    model=OpenAICompatibleModel("test-key",attempts=1)
    def request(messages, **kwargs):
        calls.append(messages)
        return json.dumps({"actions":[{"type":"MESSAGE","message":"你好","live2d":{"motion":"Nod","expression":"开心"}}]})
    model._request=request
    try:
        with presentation_scope(event(caps)):
            result=model.react_for_session("same formal turn"+presentation_prompt(event(caps)),"direct:rin")
        assert len(calls)==1
        assert "Available Live2D Resources" in calls[0][-1]["content"]
        assert behavior_metadata(event(caps),result.actions[0])["live2d"]["expression"]=="开心"
        plain=model.react("ordinary turn")
        assert not hasattr(plain.actions[0],"live2d")
        assert "Live2D" not in calls[1][-1]["content"]
    finally:
        model.close()
