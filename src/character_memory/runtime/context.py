from __future__ import annotations

from datetime import timezone

from character_memory.domain.models import EventType


def _relationship_time_text(event, last_chat_event) -> str:
    if last_chat_event is None:
        return "- 这是目前可见历史中的第一次聊天。"
    current = event.event_time
    previous = last_chat_event.event_time
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    seconds = max(0, int((current - previous).total_seconds()))
    if seconds < 60:
        gap = f"{seconds} 秒"
    elif seconds < 3600:
        gap = f"{seconds // 60} 分钟"
    elif seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        gap = f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        gap = f"{days} 天" + (f" {hours} 小时" if hours else "")
    return f"- 上次聊天时间：{previous.isoformat()}\n- 距离上次聊天：{gap}\n- 不要机械地说‘好久不见’；只有 Persona、记忆和当前语境真的需要时才提及时间或旧事。"


def compile_context(persona: str, mental_state: str, memories, event, recent_events=None, last_chat_event=None, sticker_catalog=None) -> str:
    relationship = [m for m in memories if m.memory_type.upper() in {"USER", "SHARED"}]
    other = [m for m in memories if m not in relationship]
    relationship_text = "\n".join(f"- [{m.memory_type}] {m.content}" for m in relationship) or "- 无"
    recalled = "\n".join(f"- [{m.memory_type}] {m.content}" for m in other) or "- 无"
    recent = (
        "\n".join(f"- {e.event_time.isoformat()} {e.event_type.value}: {e.content}" for e in (recent_events or []))
        or "- 无"
    )
    has_stickers = bool(sticker_catalog and sticker_catalog.stickers)
    stickers = sticker_catalog.prompt_text() if has_stickers else "- 无"
    sticker_action = " / STICKER" if has_stickers else ""
    allowed = {
        EventType.USER_MESSAGE: f"0~3 个 MESSAGE / EMOJI{sticker_action}；也可以完全不回复",
        EventType.TIME_TICK: f"0~3 个 MESSAGE / EMOJI{sticker_action}；只有确实想主动表达时才发送",
        EventType.PROACTIVE_INTENT: f"0~3 个 MESSAGE / EMOJI{sticker_action}；也可以放弃或延后",
    }.get(event.event_type, f"0~3 个 MESSAGE / EMOJI{sticker_action}；也可以没有对外表达")
    relationship_time = _relationship_time_text(event, last_chat_event)
    return f"""# Identity / Persona
{persona}

# Current Mental State
{mental_state or '暂无持续心理状态。'}

# Relationship Time
{relationship_time}

# Relationship Context
{relationship_text}

# Other Relevant Memories
{recalled}

# Recent Events
{recent}

# Available Stickers
{stickers}

# Current Event
时间：{event.event_time.isoformat()}
{event.event_type.value}: {event.content}

# Behavioral Contract
你是一个持续存在的人物，不是客服。用户发来消息不代表你必须回复；真实的人会回复、追问、只发一个表情，也会在对话自然结束、需要空间、没有想说的话或不想回应时保持沉默。
本事件允许的对外表达：{allowed}。
actions 是本轮真正对外发生的动作，最多 3 个；通常用 MESSAGE，单独的 emoji/颜文字可以用 EMOJI。Available Stickers 非空时才可使用 STICKER，并且 sticker_id 必须从上面的列表中选择。自然需要连续两三条时可以拆开，但不要机械拆句、刷屏或为了显得可爱而强行发表情包。
如果确实没有想回复的内容，直接 actions=[]。不要为了礼貌、活跃度或“完成任务”硬补一句话。
不要无条件迎合，也不要为了提高互动率而主动联系。
内部结构可以稀疏，但对外表达不以“最短输出”为目标。严格服从 Persona 的表达习惯，自然使用标点、停顿、emoji、颜文字或合适的表情包；需要理解、好奇或关心时可以自然追问，但不要机械地用问题维持对话。
perception / reaction 是给开发者和可选 UI 使用的安全摘要，不是隐藏思维链；有明确内容时尽量各写一句很短的摘要，没有必要时可以留空。
mental_state_update 只在本轮确实产生了值得延续的心理变化时填写；否则留空，系统会沿用上一状态。
只把未来确实值得想起的内容放进 memory_candidates；普通寒暄、一次性琐事、重复事实不要写。没有值得记忆的内容就保持空数组。
只有确实存在未来行动意图时才填写 intent_candidates，否则保持空数组。
当前版本没有外部信息工具；不知道实时事实时可以承认不知道或自然询问，不要假装已经查询过。
"""
