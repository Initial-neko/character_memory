from __future__ import annotations

from datetime import datetime

from character_memory.domain.models import Event, EventType, Memory
from character_memory.llm.usage import llm_usage_scope


class LifeSimulator:
    def __init__(self, store, embeddings, model, persona: str, runtime=None):
        self.store = store
        self.embeddings = embeddings
        self.model = model
        self.persona = persona
        self.runtime = runtime

    def _base(self, character_id: str, now: datetime):
        state = self.store.get_mental_state(character_id, at=now)
        recent = self.store.list_events(character_id, limit=20, before=now)
        recent_memories = [m for m in self.store.list_memories(character_id) if m.event_time <= now][-12:]
        history = "\n".join(f"- {e.event_time.isoformat()} {e.event_type.value}: {e.content}" for e in recent) or "- 无"
        memories = "\n".join(f"- [{m.memory_type}] {m.content}" for m in recent_memories) or "- 无"
        return (
            f"# Persona\n{self.persona}\n\n# Mental State\n{state or '暂无'}"
            f"\n\n# Recent Memories\n{memories}\n\n# Recent Life\n{history}\n\n# Date\n{now.date().isoformat()}"
        )

    def simulate_day(self, character_id: str, day: datetime):
        with llm_usage_scope(
            feature="LIFE",
            purpose="LIFE_PLAN",
            character_id=character_id,
            conversation_id=f"life:{character_id}:{day.date().isoformat()}",
            override=True,
        ):
            plan = self.model.plan_day(
                self._base(character_id, day)
                + "\n\n规划这一天 0-3 个符合人物自身生活的事件。不要让所有生活都围绕用户。动态可选；没有自然内容就不要发。"
            )
        created = []
        for candidate in plan.events:
            t = day.replace(hour=candidate.hour if candidate.hour is not None else 18, minute=0, second=0, microsecond=0)
            event = self.store.append_event(
                Event(
                    character_id=character_id,
                    event_type=EventType.LIFE_EVENT,
                    event_time=t,
                    content=candidate.content,
                    metadata={"importance": candidate.importance},
                )
            )
            created.append(event)
            self.store.add_memory(
                Memory(
                    character_id=character_id,
                    content=candidate.content,
                    memory_type="LIFE",
                    event_time=t,
                    importance=candidate.importance,
                    source_event_id=event.id,
                    embedding=self.embeddings.embed(candidate.content),
                )
            )
        if plan.social_post:
            self.store.append_event(
                Event(
                    character_id=character_id,
                    event_type=EventType.SOCIAL_POST,
                    event_time=day.replace(hour=20, minute=0, second=0, microsecond=0),
                    content=plan.social_post,
                    metadata={"image_prompt": plan.image_prompt},
                )
            )
        return created

    def end_day(self, character_id: str, day: datetime):
        end = day.replace(hour=23, minute=59, second=59, microsecond=999999)
        events = self.store.list_events(character_id, limit=80, before=end)
        today = [e for e in events if e.event_time.date() == day.date()]
        lines = "\n".join(f"- {e.event_time.strftime('%H:%M')} {e.event_type.value}: {e.content}" for e in today) or "- 今天没有记录到明显事件"
        with llm_usage_scope(
            feature="LIFE",
            purpose="LIFE_DIARY",
            character_id=character_id,
            conversation_id=f"life:{character_id}:{day.date().isoformat()}",
            override=True,
        ):
            result = self.model.write_diary(
                self._base(character_id, end)
                + f"\n\n# Today's Events\n{lines}"
                + "\n\n写当天第一人称日记，并给出下一天可持续的 mental_state_update。日记是主观记录，不是事实源，不得改写原始事件。"
            )
        t = day.replace(hour=23, minute=30, second=0, microsecond=0)
        event = self.store.append_event(Event(character_id=character_id, event_type=EventType.DIARY, event_time=t, content=result.diary))
        self.store.set_mental_state(character_id, result.mental_state_update, t, event.id)
        self.store.add_memory(
            Memory(
                character_id=character_id,
                content=result.diary,
                memory_type="DIARY",
                event_time=t,
                importance=0.55,
                source_event_id=event.id,
                embedding=self.embeddings.embed(result.diary),
            )
        )
        for candidate in result.memory_candidates:
            self.store.add_memory(
                Memory(
                    character_id=character_id,
                    content=candidate.content,
                    memory_type=candidate.memory_type,
                    event_time=t,
                    importance=candidate.importance,
                    source_event_id=event.id,
                    embedding=self.embeddings.embed(candidate.content),
                )
            )
        return result
