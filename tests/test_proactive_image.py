from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from character_memory.application.proactive_service import ProactiveService
from character_memory.domain.models import ActionDecision, ActionType, PersonReaction
from character_memory.storage.sqlite import SQLiteStore


class ImageChatStub:
    def dispatch_proactive_intent(self, **kwargs):
        return SimpleNamespace(
            event=SimpleNamespace(id=99),
            reaction=PersonReaction(actions=[ActionDecision(type=ActionType.IMAGE, image_id="daily_photo")]),
        )


def test_proactive_image_action_marks_intent_executed(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    intent_id = store.add_intent(
        "momo",
        "晚一点把今天拍的照片发给用户",
        ActionType.PROACTIVE_MESSAGE.value,
        now - timedelta(hours=2),
        now - timedelta(hours=1),
        now + timedelta(hours=3),
    )

    outcomes = ProactiveService(store, ImageChatStub()).dispatch_due(["momo"], now)

    assert outcomes[0]["intent_id"] == intent_id
    assert outcomes[0]["status"] == "EXECUTED"
    assert outcomes[0]["actions"] == ["IMAGE"]
    assert dict(store.list_intents("momo", limit=1)[0])["status"] == "EXECUTED"
    store.close()
