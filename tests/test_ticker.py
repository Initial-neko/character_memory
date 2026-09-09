from datetime import datetime, timedelta, timezone

from character_memory.domain.models import ActionDecision, ActionType, DailyLifePlan, DiaryResult, PersonReaction
from character_memory.life.ticker import TimeTicker
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class IntentModel(PersonModel):
    def react(self, context):
        return PersonReaction(
            perception="想起了之前的事",
            reaction="现在适合问一下",
            mental_state_update="好奇结果",
            action=ActionDecision(type=ActionType.PROACTIVE_MESSAGE, reason="存在未完成话题", message="汇报结束了吗？"),
        )

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="平静。", mental_state_update="平静")


class NewMessageIntentModel(IntentModel):
    def react(self, context):
        return PersonReaction(
            perception="想起了之前的事",
            reaction="想问一下结果",
            actions=[ActionDecision(type=ActionType.MESSAGE, message="所以，汇报怎么样了？")],
        )


def _add_due_intent(store, now):
    return store.add_intent(
        "rin",
        "问用户下午汇报怎么样",
        "PROACTIVE_MESSAGE",
        now - timedelta(hours=2),
        now - timedelta(hours=1),
        now + timedelta(hours=6),
        "之前提到过汇报",
    )


def test_due_intent_executes_through_same_runtime(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, IntentModel(), "persona")
    now = datetime(2026, 9, 5, 18, tzinfo=timezone.utc)
    intent_id = _add_due_intent(store, now)

    results = TimeTicker(store, runtime).tick("rin", now)

    assert len(results) == 1
    assert results[0].reaction.action.type == ActionType.PROACTIVE_MESSAGE
    row = next(r for r in store.list_intents("rin") if r["id"] == intent_id)
    assert row["status"] == "EXECUTED"


def test_due_intent_new_message_action_is_also_executed(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, NewMessageIntentModel(), "persona")
    now = datetime(2026, 9, 5, 18, tzinfo=timezone.utc)
    intent_id = _add_due_intent(store, now)

    results = TimeTicker(store, runtime).tick("rin", now)

    assert results[0].reaction.action.type == ActionType.MESSAGE
    row = next(r for r in store.list_intents("rin") if r["id"] == intent_id)
    assert row["status"] == "EXECUTED"
