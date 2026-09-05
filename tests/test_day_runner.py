from datetime import datetime, timezone

from character_memory.domain.models import (
    ActionDecision,
    ActionType,
    DailyLifePlan,
    DiaryResult,
    LifeEventCandidate,
    PersonReaction,
)
from character_memory.life.runner import DayRunner
from character_memory.life.simulator import LifeSimulator
from character_memory.life.ticker import TimeTicker
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class FakeDayModel(PersonModel):
    def react(self, context):
        return PersonReaction(
            perception="时间过去了",
            reaction="没有特别想联系谁",
            mental_state_update="平静",
            action=ActionDecision(type=ActionType.NO_ACTION, reason="没有自然理由主动联系"),
        )

    def plan_day(self, context):
        return DailyLifePlan(events=[LifeEventCandidate(content="去了书店", importance=0.4, hour=18)])

    def write_diary(self, context):
        return DiaryResult(diary="今天去了书店。", mental_state_update="平静")


def test_day_runner_persists_world_time_and_runs_next_day(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    model = FakeDayModel()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, model, "persona")
    life = LifeSimulator(store, emb, model, "persona", runtime)
    runner = DayRunner(store, life, TimeTicker(store, runtime))
    start = datetime(2026, 9, 5, 18, tzinfo=timezone.utc)
    store.set_world_time("rin", start)

    result = runner.run_next_day("rin", tick_hours=(9,))

    assert result["date"] == "2026-09-06"
    assert result["life_events"] == 1
    assert store.get_world_time("rin") == datetime(2026, 9, 7, 0, tzinfo=timezone.utc)
