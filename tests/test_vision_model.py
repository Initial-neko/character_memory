from character_memory.llm.client import OpenAICompatibleModel


class RecordingModel(OpenAICompatibleModel):
    def __init__(self):
        super().__init__(
            "test-key",
            model="deepseek-flash",
            vision_model="deepseek-v4-flash-vision-exp",
            base_url="https://example.invalid/v1",
            attempts=1,
        )
        self.requests = []

    def _request(self, messages, *, conversation_id=None, json_object=False, model=None):
        self.requests.append(
            {
                "messages": messages,
                "conversation_id": conversation_id,
                "json_object": json_object,
                "model": model,
            }
        )
        return '{"actions":[],"memory_candidates":[],"intent_candidates":[]}'


def test_image_turn_routes_to_vision_model_and_redacts_trace_base64():
    model = RecordingModel()
    try:
        data_url = "data:image/png;base64,aGVsbG8="
        result = model.react_with_images_for_session("看看这张图", [data_url], "conversation")

        assert result.actions == []
        request = model.requests[-1]
        assert request["model"] == "deepseek-v4-flash-vision-exp"
        assert request["json_object"] is True
        content = request["messages"][1]["content"]
        assert content[0] == {"type": "text", "text": "看看这张图"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"] == data_url
        assert model.last_model == "deepseek-v4-flash-vision-exp"
        assert model.last_request_messages[1]["content"][1]["image_url"]["url"] == "data:image/png;base64,<base64 omitted>"
        assert "aGVsbG8=" not in str(model.last_request_messages)
    finally:
        model.close()


def test_text_turn_stays_on_v41_flash():
    model = RecordingModel()
    try:
        model.react_for_session("普通文字", "conversation")
        assert model.requests[-1]["model"] == "deepseek-flash"
        assert isinstance(model.requests[-1]["messages"][1]["content"], str)
        assert model.last_model == "deepseek-flash"
    finally:
        model.close()
