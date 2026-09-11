from character_memory.avatar_intent import AvatarIntentPlanner, AvatarSearchIntent


class FakeStructuredModel:
    def __init__(self):
        self.prompt = ""
        self.images = None
        self.schema = None
        self.session_id = ""

    def structured_with_images_for_session(self, prompt, image_data_urls, schema, session_id):
        self.prompt = prompt
        self.images = image_data_urls
        self.schema = schema
        self.session_id = session_id
        return schema(
            visual_intent="有点害羞，但仍然保持聪明和自信的气质",
            queries=[
                "牧濑红莉栖 害羞 微笑 动漫头像 正脸",
                "Kurisu Makise shy smile anime portrait",
            ],
            preferred_mood="轻微害羞、自信",
            preferred_style="清晰正脸或近景，背景简单",
        )


def test_avatar_intent_planner_uses_live_context_but_returns_short_search_queries():
    model = FakeStructuredModel()
    planner = AvatarIntentPlanner(model)

    intent = planner.plan(
        "kurisu",
        persona="name: 牧濑红莉栖\nidentity: 天才脑科学研究员\npersonality: 傲娇、聪明",
        mental_state={"mood": "被逗得有些害羞", "energy": 0.7},
        recent_dialogue=["用户: 你今天看起来心情不错", "角色: 才、才没有因为你高兴呢。"],
        user_hint="希望看起来温暖一点",
    )

    assert intent.visual_intent.startswith("有点害羞")
    assert len(intent.queries) == 2
    assert model.images == []
    assert model.schema is AvatarSearchIntent
    assert model.session_id == "avatar-intent:kurisu"
    assert "被逗得有些害羞" in model.prompt
    assert "希望看起来温暖一点" in model.prompt
    assert "搜索词绝不能包含用户姓名" in model.prompt
    assert all(len(query) <= 180 for query in intent.queries)


def test_avatar_search_intent_deduplicates_and_trims_queries():
    intent = AvatarSearchIntent(
        visual_intent="经典头像",
        queries=["  Mika   avatar  ", "mika avatar", "第二条", "第三条"],
    )

    assert intent.queries == ["Mika avatar", "第二条", "第三条"]
