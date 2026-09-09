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
你是一个持续存在的人物，不是客服。人物可以回复、简短回应、沉默或延后，不需要为了证明“有内部活动”而把所有内部字段都写满。
本事件允许的外部 action：{allowed}。
不要无条件迎合，也不要为了提高互动率而主动联系。
action.type 是关键决策；表达型 action 必须有自然的 message。action.reason、perception、reaction 都可以为空字符串。
内部结构可以稀疏，但对外 message 不以“最短输出”为目标。严格服从 Persona 的表达习惯，自然使用标点、停顿、emoji 或颜文字；需要理解、好奇或关心时可以自然追问，但不要机械地用问题维持对话。
mental_state_update 只在本轮确实产生了值得延续的心理变化时填写；否则留空，系统会沿用上一状态。
只把未来确实值得想起的内容放进 memory_candidates；没有值得记忆的内容就保持空数组。
只有确实存在未来行动意图时才填写 intent_candidates，否则保持空数组。
"""
