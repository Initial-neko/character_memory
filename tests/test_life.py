from datetime import datetime,timezone
from character_memory.domain.models import *
from character_memory.storage.sqlite import SQLiteStore
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.life.simulator import LifeSimulator
from character_memory.llm.client import PersonModel
class FakeModel(PersonModel):
    def react(self,c): raise AssertionError
    def plan_day(self,c): return DailyLifePlan(events=[LifeEventCandidate(content="去了书店",importance=.4,hour=18)],social_post="买到一本书",image_prompt="book")
    def write_diary(self,c): return DiaryResult(diary="今天去了书店。",mental_state_update="有点开心",memory_candidates=[])
def test_life_day(tmp_path):
    store=SQLiteStore(tmp_path/'x.db'); emb=DeterministicEmbedding(); life=LifeSimulator(store,emb,FakeModel(),"persona")
    day=datetime(2026,9,5,tzinfo=timezone.utc); life.simulate_day("rin",day); life.end_day("rin",day)
    types=[e.event_type for e in store.list_events("rin")]
    assert EventType.LIFE_EVENT in types and EventType.DIARY in types and EventType.SOCIAL_POST in types
    assert store.get_mental_state("rin")=="有点开心"
