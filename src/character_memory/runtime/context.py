from __future__ import annotations

from character_memory.domain.models import EventType


def compile_context(persona: str, mental_state: str, memories, event, recent_events=None) -> str:
    relationship = [m for m in memories if m.memory_type.upper() in {"USER", "SHARED"}]
    other = [m for m in memories if m not in relationship]
    relationship_text = "\n".join(f"- [{m.memory_type}] {m.content}" for m in relationship) or "- 无"
    recalled = "\n".join(f"- [{m.memory_type}] {m.content}" for m in other) or "- 无"
    recent = (
        "\n".join(f"- {e.event_time.isoformat()} {e.event_type.value}: {e.content}" for e in (recent_events or []))
        or "- 无"
    )
    allowed = {
        EventType.USER_MESSAGE: "REPLY / MINIMAL_RESPONSE / NO_REPLY / DEFER",
        EventType.TIME_TICK: "PROACTIVE_MESSAGE / NO_ACTION / DEFER",
        EventType.PROACTIVE_INTENT: "PROACTIVE_MESSAGE / NO_ACTION / DEFER",
    }.get(event.event_type, "REPLY / MINIMAL_RESPONSE / NO_REPLY / DEFER / PROACTIVE_MESSAGE / NO_ACTION")
    return f"""# Identity / Persona
{persona}

# Current Mental State
{mental_state or '暂无持续心理状态。'}

# Relationship Context
{relationship_text}

# Other Relevant Memories
{recalled}

# Recent Events
{recent}

# Current Event
时间：{event.event_time.isoformat()}
{event.event_type.value}: {event.content}

# Behavioral Contract
你是一个持续存在的人物，不是客服。每个被感知的事件都必须产生内部反应，但不要求外部表达。
本事件允许的外部 action：{allowed}。
沉默必须有可解释原因，但原因不必告诉用户。不要无条件迎合，也不要为了提高互动率而主动联系。
action.reason 是给开发者检查的一句简短理由，不是隐藏思维过程；perception/reaction 也只写简短安全摘要。
mental_state_update 必须是“事件发生后的完整、紧凑心理状态”，会替换上一轮状态，而不是不断追加日志。
只把未来确实值得想起的内容放进 memory_candidates；记忆既可以关于用户，也可以关于人物自己或共同经历。
"""
