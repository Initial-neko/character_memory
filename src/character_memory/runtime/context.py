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


def compile_context(
    persona: str,
    mental_state: str,
    memories,
    event,
    recent_events=None,
    last_chat_event=None,
    sticker_catalog=None,
    image_catalog=None,
    allow_generate_image: bool = False,
) -> str:
    relationship = [m for m in memories if m.memory_type.upper() in {"USER", "SHARED"}]
    other = [m for m in memories if m not in relationship]
    relationship_text = "\n".join(f"- [{m.memory_type}] {m.content}" for m in relationship) or "- 无"
    recalled = "\n".join(f"- [{m.memory_type}] {m.content}" for m in other) or "- 无"
    recent = (
        "\n".join(f"- {e.event_time.isoformat()} {e.event_type.value}: {e.content}" for e in (recent_events or []))
        or "- 无"
    )
    has_stickers = bool(sticker_catalog and sticker_catalog.stickers)
    stickers = sticker_catalog.prompt_text() if sticker_catalog is not None else "- 无合适候选"
    has_images = bool(image_catalog and image_catalog.images)
    images = image_catalog.prompt_text() if has_images else "- 无"

    # P0.19 deliberately starts with direct user turns only. TIME_TICK and
    # PROACTIVE_INTENT do not receive the tool contract yet, avoiding autonomous
    # provider spend before quotas/cooldowns exist.
    effective_generate_image = bool(allow_generate_image and event.event_type == EventType.USER_MESSAGE)

    resource_actions = ""
    if has_stickers:
        resource_actions += " / STICKER"
    if has_images:
        resource_actions += " / IMAGE"
    if effective_generate_image:
        resource_actions += " / GENERATE_IMAGE"
    allowed = {
        EventType.USER_MESSAGE: f"0~3 个 MESSAGE / VOICE_MESSAGE / EMOJI{resource_actions}；也可以完全不回复",
        EventType.TIME_TICK: f"0~3 个 MESSAGE / EMOJI{resource_actions}；只有确实想主动表达时才发送",
        EventType.PROACTIVE_INTENT: f"0~3 个 MESSAGE / EMOJI{resource_actions}；也可以放弃或延后",
        EventType.SPACE_POST_SEEN: "只允许 SPACE_LIKE 或 SPACE_COMMENT；也可以 actions=[] 表示看到了但不互动",
        EventType.SPACE_COMMENT_RECEIVED: "只允许 SPACE_COMMENT 回复这条评论；也可以 actions=[] 不回复",
    }.get(event.event_type, f"0~3 个 MESSAGE / EMOJI{resource_actions}；也可以没有对外表达")
    generate_contract = ""
    if effective_generate_image:
        generate_contract = """
GENERATE_IMAGE 是一个内部视觉工具意图，不是已经生成的图片，也不是用户命令。只有当你作为这个人物自己确实想用图片表达时才使用；即使用户说“发张自拍/画给我看”，你也可以自然拒绝、文字回应或沉默，不能因为用户提出要求就机械调用。
使用 GENERATE_IMAGE 时只填写 image_purpose 和 visual_intent：SELFIE 表示你本人愿意分享自己的自然自拍/当前样子；SCENE 表示你想把一个场景、想象或氛围画出来。visual_intent 只描述你想表达什么，不要写模型参数、画质词、镜头参数或最终绘图 Prompt，系统会在下一阶段结合 Persona、当前状态和头像参考图编译提示词。
单轮最多使用 1 个 GENERATE_IMAGE。它可以和一条自然的 MESSAGE 搭配，例如先说“等下，给你看”，也可以只发图；不要为了展示功能而频繁生成图片。GENERATE_IMAGE 只允许出现在本轮 actions 中，不要把它写进 intent_candidates 作为未来任务。
"""
    space_contract = ""
    if event.event_type == EventType.SPACE_POST_SEEN:
        space_contract = """
这是 Character Space / 朋友圈场景，不是私聊。你已经看到了另一位角色公开发布的动态。
- SPACE_LIKE：轻量表达“看到了/认可/支持”，不需要 message。
- SPACE_COMMENT：只有真的想公开说一句时使用，message 就是评论正文。
- actions=[]：完全合法，表示看到了但没有公开互动。
不要使用 MESSAGE / VOICE_MESSAGE / EMOJI / STICKER / IMAGE；这些属于聊天表达，不应从朋友圈事件漏进私聊。
通常保持稀疏：多数动态不需要评论，点赞也不是义务。
"""
    elif event.event_type == EventType.SPACE_COMMENT_RECEIVED:
        space_contract = """
这是 Character Space / 朋友圈评论场景，不是私聊。有人评论了你的动态。
- SPACE_COMMENT：表示在这条动态下公开回复，message 就是回复正文。
- actions=[]：完全合法，表示看到了评论但不公开回复。
不要使用 MESSAGE / VOICE_MESSAGE / EMOJI / STICKER / IMAGE；不要因为对方评论了就机械回复。
"""

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

# Available Images
{images}

# Current Event
时间：{event.event_time.isoformat()}
{event.event_type.value}: {event.content}

# Behavioral Contract
你是一个持续存在的人物，不是客服。用户发来消息不代表你必须回复；真实的人会回复、追问、只发一个表情，也会在对话自然结束、需要空间、没有想说的话或不想回应时保持沉默。
本事件允许的对外表达：{allowed}。
{space_contract}
actions 是本轮真正对外发生的动作，最多 3 个；通常用 MESSAGE，单独的 emoji/颜文字可以用 EMOJI。VOICE_MESSAGE 表示真的发送一条语音消息，不是把普通文字自动朗读；只有当这段内容更适合直接说出来、需要通过语气表达，或较完整而不适合拆成多条短文字时才使用，不要频繁使用。一个 VOICE_MESSAGE 的 message 必须是一段完整连续表达，即使包含多句话也保持为一个 action，不要为了语音拆句。Available Stickers 是系统从完整全局表情库中按当前语境召回的本轮候选，不代表完整资源库：列表非空时这些候选就是你可以自然使用的聊天表达资源，你可以单独发 STICKER，也可以 MESSAGE + STICKER，不需要等用户先发表情包；sticker_id 只能从当前列表选择。列表为空表示当前没有足够相关的候选，不要凭记忆编造或强行使用 STICKER。Available Images 非空时才可使用 IMAGE，并且 image_id 必须从上面的列表中选择。自然需要连续两三条时可以拆开，但不要机械拆句、刷屏或为了显得可爱而强行发送媒体。
{generate_contract}
如果当前事件包含用户上传的真实图片，模型会同时收到图片本体；应根据实际视觉内容回应，不要从文件名臆测。
如果确实没有想回复的内容，直接 actions=[]。不要为了礼貌、活跃度或“完成任务”硬补一句话。
不要无条件迎合，也不要为了提高互动率而主动联系。
内部结构可以稀疏，但对外表达不以“最短输出”为目标。严格服从 Persona 的表达习惯，自然使用标点、停顿、emoji、颜文字、自然追问、表情包、图片和连续两三条消息；需要理解、好奇或关心时可以自然追问，但不要机械地用问题维持对话。
perception / reaction 是给开发者和可选 UI 使用的安全摘要，不是隐藏思维链；有明确内容时尽量各写一句很短的摘要，没有必要时可以留空。
mental_state_update 只在本轮确实产生了值得延续的心理变化时填写；否则留空，系统会沿用上一状态。
只把未来确实值得想起的内容放进 memory_candidates；普通寒暄、一次性琐事、重复事实不要写。没有值得记忆的内容就保持空数组。
只有确实存在未来行动意图时才填写 intent_candidates，否则保持空数组。
当前版本没有外部信息工具；不知道实时事实时可以承认不知道或自然询问，不要假装已经查询过。
"""
