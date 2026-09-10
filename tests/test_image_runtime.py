import base64
from datetime import datetime, timezone

from character_memory.domain.models import ActionDecision, ActionType, DailyLifePlan, DiaryResult, Event, EventType, PersonReaction
from character_memory.images import load_image_catalog
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


class ImageFakeModel(PersonModel):
    def __init__(self, image_id):
        self.image_id = image_id

    def react(self, context):
        return PersonReaction(actions=[ActionDecision(type=ActionType.IMAGE, image_id=self.image_id)])

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def _catalog(tmp_path):
    persona_dir = tmp_path / "personas" / "momo"
    image_dir = persona_dir / "images"
    image_dir.mkdir(parents=True)
    persona = persona_dir / "persona.yaml"
    persona.write_text("id: momo\nname: Momo\n", encoding="utf-8")
    (image_dir / "tea.png").write_bytes(PNG_1X1)
    (image_dir / "manifest.yaml").write_text(
        "images:\n"
        "  - id: tea_photo\n"
        "    file: tea.png\n"
        "    label: 下午茶照片\n"
        "    tags: [分享, 下午茶, 日常]\n"
        "    description: 一张人物可以自然分享的下午茶照片\n",
        encoding="utf-8",
    )
    return persona, load_image_catalog(persona)


def test_valid_character_image_action_is_persisted(tmp_path):
    _, catalog = _catalog(tmp_path)
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, ImageFakeModel("tea_photo"), "persona", image_catalog=catalog)

    result = runtime.handle(Event(character_id="momo", event_type=EventType.USER_MESSAGE, event_time=datetime(2026, 9, 10, tzinfo=timezone.utc), content="在干嘛"))

    assert result.reaction.actions[0].type == ActionType.IMAGE
    messages = store.list_events("momo", event_type=EventType.CHARACTER_MESSAGE.value)
    assert messages[-1].content == "[图片：下午茶照片]"
    assert messages[-1].metadata["action"] == "IMAGE"
    assert messages[-1].metadata["image_id"] == "tea_photo"
    trace = store.get_runtime_trace(result.event.id)
    assert trace["image_decisions"] == [{"image_id": "tea_photo", "decision": "ALLOW", "label": "下午茶照片"}]
    store.close()


def test_unknown_character_image_action_is_dropped(tmp_path):
    _, catalog = _catalog(tmp_path)
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, ImageFakeModel("invented_photo"), "persona", image_catalog=catalog)

    result = runtime.handle(Event(character_id="momo", event_type=EventType.USER_MESSAGE, event_time=datetime(2026, 9, 10, tzinfo=timezone.utc), content="发张照片"))

    assert result.reaction.actions == []
    assert store.list_events("momo", event_type=EventType.CHARACTER_MESSAGE.value) == []
    trace = store.get_runtime_trace(result.event.id)
    assert trace["image_decisions"] == [{"image_id": "invented_photo", "decision": "DROP_UNKNOWN_IMAGE"}]
    store.close()
