import json
import threading

import pytest
import yaml

from character_memory.persona_builder import PersonaBuilder, PersonaDraft, dump_persona_yaml, ensure_safe_persona_text, normalize_character_id


class BuilderFakeModel:
    attempts = 2

    def __init__(self):
        self._call_lock = threading.RLock()
        self.calls = 0

    @staticmethod
    def _json(text):
        return json.loads(text)

    def _request(self, messages, *, conversation_id=None, json_object=False):
        self.calls += 1
        assert conversation_id == "persona-builder"
        assert json_object is True
        return json.dumps(
            {
                "name": "Nova",
                "age": 24,
                "identity": "独立游戏音效设计师，喜欢收集城市里奇怪的声音",
                "tagline": "脑洞很大，但不会什么都顺着别人",
                "description": "Nova 很容易被奇怪的小事吸引。她会认真记住共同经历里的细节，也会直接表达不同意见。",
                "personality": ["好奇心强", "有自己的审美", "熟悉以后会开怪玩笑"],
                "conversation": "偏自然短句，兴奋时会突然连着说两三句。",
                "expression": "会偶尔使用感叹号和很轻的 emoji，不机械刷表情。",
                "questions": "真的好奇才追问，一次通常只抓一个点。",
                "silence": "对话自然结束或没想说的话时可以不回复。",
                "initiative": "遇到和共同经历有关的声音、游戏或小事时可能主动想起对方。",
                "disagreement": "不同意会直接说理由，不为了维持气氛假装赞同。",
                "care": "更喜欢记住具体事情并后来问结果，而不是泛泛安慰。",
                "boundaries": ["不无条件迎合", "关系通过共同经历慢慢形成"],
            },
            ensure_ascii=False,
        )


def test_persona_builder_generates_valid_draft():
    model = BuilderFakeModel()
    draft = PersonaBuilder(model).generate("想认识一个脑洞很大的声音设计师", name="Nova", age=24)
    assert draft.name == "Nova"
    assert draft.age == 24
    assert "共同经历" in draft.description
    assert model.calls == 1


def test_persona_yaml_keeps_existing_persona_shape():
    draft = PersonaDraft(
        name="Nova",
        age=24,
        identity="独立游戏音效设计师",
        tagline="会收集奇怪声音",
        description="一个有自己判断、也愿意记住共同经历的人。",
        personality=["好奇", "独立"],
        conversation="自然口语。",
        expression="偶尔使用 emoji。",
        questions="真的好奇才问。",
        silence="没话时可以沉默。",
        initiative="遇到共同话题后续时可能主动提起。",
        disagreement="会表达不同意见。",
        care="记住具体事情。",
        boundaries=["不迎合", "不强制亲密"],
    )
    data = yaml.safe_load(dump_persona_yaml(draft, "nova"))
    assert data["id"] == "nova"
    assert data["name"] == "Nova"
    assert data["behavior"]["silence"] == "没话时可以沉默。"
    assert data["boundaries"] == ["不迎合", "不强制亲密"]


def test_character_id_is_filesystem_safe():
    assert normalize_character_id("Nova") == "nova"
    assert normalize_character_id("Nova Sound!") == "nova-sound"
    assert normalize_character_id("星野") .startswith("character-")


def test_persona_builder_rejects_explicit_sexualized_request():
    with pytest.raises(ValueError, match="不支持"):
        ensure_safe_persona_text("做一个 NSFW 色情角色")
