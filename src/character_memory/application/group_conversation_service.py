from __future__ import annotations

from datetime import datetime
import logging
import threading
import time
from uuid import uuid4

from character_memory.domain.models import ActionType, Event, EventType, Memory
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.runtime.context import compile_context


logger = logging.getLogger("character_memory.application.group")

_EXPRESSIVE_ACTIONS = {
    ActionType.REPLY,
    ActionType.MINIMAL_RESPONSE,
    ActionType.PROACTIVE_MESSAGE,
    ActionType.MESSAGE,
    ActionType.EMOJI,
    ActionType.STICKER,
    ActionType.IMAGE,
}


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class GroupConversationService:
    """Shared group-conversation orchestrator.

    One user fact is persisted once in `conversation_events`. Each character then
    observes that same shared history, independently reacts or stays silent, and
    writes only private derived cognition (Mental State / Memory) to its own
    character state. Visible character actions are appended back to the shared
    conversation, never copied into the character-local chat Event Log.

    `turn_lock` must be application-shared for Web requests. It protects the
    complete user-input -> all-member-reactions turn so a later user message
    cannot leak into an earlier member's context.
    """

    def __init__(
        self,
        store,
        runtimes: dict,
        clock,
        *,
        chat_service=None,
        profiles: list[dict] | None = None,
        turn_lock=None,
    ):
        self.store = store
        self.runtimes = runtimes
        self.clock = clock
        self.chat_service = chat_service
        self.repo = GroupRepository(store)
        self.profile_by_id = {item["id"]: item for item in (profiles or [])}
        self.turn_lock = turn_lock or threading.RLock()

    def _name(self, actor_id: str) -> str:
        if actor_id == "user":
            return "User"
        profile = self.profile_by_id.get(actor_id) or {}
        return str(profile.get("name") or actor_id)

    def create_group(self, name: str, member_ids: list[str], *, at: datetime | None = None):
        unique = []
        for value in member_ids:
            character_id = str(value).strip()
            if character_id and character_id not in unique:
                unique.append(character_id)
        if len(unique) < 2 or len(unique) > 4:
            raise ValueError("group must contain 2 to 4 distinct characters")
        unknown = [character_id for character_id in unique if character_id not in self.runtimes]
        if unknown:
            raise KeyError(f"unknown group characters: {', '.join(unknown)}")
        return self.repo.create_group(name, unique, at or self.clock.now())

    def list_groups(self):
        return self.repo.list_groups()

    def history(self, conversation_id: str, limit: int = 50):
        group = self.repo.get_group(conversation_id)
        if group is None:
            raise KeyError(f"unknown group: {conversation_id}")
        return group, self.repo.list_events(conversation_id, limit=limit)

    def _recent_as_events(self, group_id: str, *, limit: int = 14) -> list[Event]:
        result = []
        for item in self.repo.list_events(group_id, limit=limit):
            actor = self._name(item.actor_id)
            event_type = EventType.USER_MESSAGE if item.actor_type == "USER" else EventType.CHARACTER_MESSAGE
            content = item.metadata.get("display_text", item.content)
            if item.metadata.get("action") == ActionType.STICKER.value:
                label = item.metadata.get("sticker_label") or item.metadata.get("sticker_id") or "表情包"
                meaning = item.metadata.get("sticker_meaning") or ""
                content = f"[表情包：{label}{f'；含义：{meaning}' if meaning else ''}]"
            elif item.metadata.get("action") == ActionType.IMAGE.value:
                content = f"[图片：{item.metadata.get('image_label') or item.metadata.get('image_id') or '图片'}]"
            elif item.metadata.get("media_id"):
                prefix = str(item.metadata.get("display_text") or "").strip()
                content = f"{prefix}\n[用户发送了一张真实图片]".strip()
            result.append(
                Event(
                    character_id=item.actor_id,
                    event_type=event_type,
                    event_time=item.event_time,
                    content=f"{actor}: {content}",
                    metadata={"group_event_id": item.id, "conversation_id": group_id},
                )
            )
        return result

    def _group_contract(self, group, character_id: str) -> str:
        names = "、".join(self._name(member_id) for member_id in group.member_ids)
        return f"""

# Group Conversation Contract
你现在位于群聊「{group.name}」。群成员：User、{names}。
你是 {self._name(character_id)}，只代表自己说话，不代替其他成员总结或回答。
群里出现消息不代表你必须发言；如果别人已经表达了与你相同的意思、当前话题与你关系不大、你没有自然补充，actions=[] 是正常且优先允许的选择。
不要机械重复别人刚说的话，不要为了保持群活跃度而插话，也不要因为你“能回答”就一定回答。
你可以自然回应 User，也可以回应其他 Character 刚刚说的话；后说话时要把本轮已经出现的群消息当成真实发生的共同经历。
Available Stickers 是系统针对当前群语境召回的候选表情；只能从当前候选中选择 STICKER，也可以完全不用表情或保持沉默。
如果你拥有 Available Images，也可以自然使用，但不要刷媒体。
当前群聊不创建未来主动 Intent：intent_candidates 必须保持 []。
来自你私人单聊的 Memory 只用于理解背景；除非 User 已在这个群里主动公开，否则不要把私人信息透露给其他群成员。
"""

    def _member_lock(self, character_id: str):
        if self.chat_service is not None and hasattr(self.chat_service, "_lock_for"):
            return self.chat_service._lock_for(character_id)
        return threading.RLock()

    def _react_member(
        self,
        *,
        group,
        source_event: GroupEvent,
        character_id: str,
        image_data_url: str | None,
    ) -> dict:
        runtime = self.runtimes[character_id]
        started = time.perf_counter()
        with self._member_lock(character_id):
            now = source_event.event_time
            self.store.set_world_time(character_id, now)
            state_before = self.store.get_mental_state(character_id, at=now)
            recall_query = str(source_event.metadata.get("display_text") or source_event.content or "").strip()
            if source_event.metadata.get("media_id"):
                recall_query = f"{recall_query} 群聊图片".strip()
            if source_event.metadata.get("action") == ActionType.STICKER.value:
                recall_query = f"{source_event.metadata.get('sticker_label') or '表情包'} {source_event.metadata.get('sticker_meaning') or ''}".strip()
            memories = runtime.recall.recall(character_id, recall_query, now=now)
            recent = self._recent_as_events(group.id)
            sticker_query = "\n".join(
                item.content.strip()
                for item in recent[-4:]
                if (item.content or "").strip()
            ) or recall_query
            sticker_retrieval = runtime.sticker_retriever.retrieve(runtime.sticker_catalog, sticker_query)
            prompt_stickers = sticker_retrieval.catalog if sticker_retrieval is not None else None
            allowed_sticker_ids = {match.sticker_id for match in sticker_retrieval.matches} if sticker_retrieval is not None else set()
            synthetic = Event(
                character_id=character_id,
                event_type=EventType.USER_MESSAGE,
                event_time=now,
                content=f"群聊里 User 刚刚发起了这一轮：{source_event.content}",
                metadata={
                    "conversation_id": group.id,
                    "group_turn_id": source_event.turn_id,
                    "source_conversation_event_id": source_event.id,
                },
            )
            context = compile_context(
                runtime.persona,
                state_before,
                memories,
                synthetic,
                recent,
                last_chat_event=None,
                sticker_catalog=prompt_stickers,
                image_catalog=runtime.image_catalog,
            ) + self._group_contract(group, character_id)
            session_id = f"group:{group.id}:{character_id}"
            model_started = time.perf_counter()
            if image_data_url:
                model_call = runtime.model.react_call_with_images_for_session(context, [image_data_url], session_id)
            else:
                model_call = runtime.model.react_call_for_session(context, session_id)
            reaction = model_call.value
            reaction, sticker_decisions, image_decisions = runtime._sanitize_resource_actions(
                reaction,
                allowed_sticker_ids=allowed_sticker_ids,
            )
            reaction = reaction.model_copy(update={"intent_candidates": []})
            model_ms = _ms(model_started)

            state_after = (reaction.mental_state_update or "").strip() or state_before
            accepted_memories, memory_decisions = runtime._prepare_memory_writes(
                character_id,
                now,
                reaction.memory_candidates,
            )
            created_memory_ids: list[int] = []
            emitted_event_ids: list[int] = []

            with self.store.transaction():
                if state_after:
                    self.store.set_mental_state(character_id, state_after, now, None)
                for candidate, embedding in accepted_memories:
                    saved = self.store.add_memory(
                        Memory(
                            character_id=character_id,
                            content=candidate.content.strip(),
                            memory_type=candidate.memory_type,
                            event_time=now,
                            importance=candidate.importance,
                            source_event_id=None,
                            metadata={
                                "origin": "GROUP",
                                "conversation_id": group.id,
                                "source_conversation_event_id": source_event.id,
                                "turn_id": source_event.turn_id,
                            },
                            embedding=embedding,
                        )
                    )
                    if saved.id is not None:
                        created_memory_ids.append(saved.id)

                for index, action in enumerate(reaction.actions):
                    if action.type not in _EXPRESSIVE_ACTIONS:
                        continue
                    metadata = {
                        "action": action.type.value,
                        "action_index": index,
                        "source_conversation_event_id": source_event.id,
                    }
                    if action.type == ActionType.STICKER:
                        sticker = runtime.sticker_catalog.get(action.sticker_id) if runtime.sticker_catalog is not None else None
                        if sticker is None:
                            continue
                        content = f"[表情包：{sticker.label}]"
                        metadata.update({
                            "sticker_id": sticker.id,
                            "sticker_label": sticker.label,
                            "sticker_meaning": "、".join(sticker.tags) or sticker.description,
                        })
                    elif action.type == ActionType.IMAGE:
                        image = runtime.image_catalog.get(action.image_id) if runtime.image_catalog is not None else None
                        if image is None:
                            continue
                        content = f"[图片：{image.label}]"
                        metadata.update({"image_id": image.id, "image_label": image.label})
                    else:
                        if not (action.message or "").strip():
                            continue
                        content = (action.message or "").strip()
                    saved_event = self.repo.append_event(
                        GroupEvent(
                            conversation_id=group.id,
                            turn_id=source_event.turn_id,
                            actor_type="CHARACTER",
                            actor_id=character_id,
                            event_type="CHARACTER_MESSAGE",
                            event_time=now,
                            content=content,
                            metadata=metadata,
                        )
                    )
                    if saved_event.id is not None:
                        emitted_event_ids.append(saved_event.id)

                trace = {
                    "perception": reaction.perception,
                    "reaction": reaction.reaction,
                    "actions": [action.model_dump(mode="json") for action in reaction.actions],
                    "mental_state_before": state_before,
                    "mental_state_after": state_after,
                    "recalled_memories": [memory.model_dump(mode="json", exclude={"embedding"}) for memory in memories],
                    "memory_decisions": memory_decisions,
                    "created_memory_ids": created_memory_ids,
                    "sticker_retrieval": {
                        "query": sticker_retrieval.query,
                        "matches": [match.__dict__ for match in sticker_retrieval.matches],
                    } if sticker_retrieval is not None else {"query": "", "matches": []},
                    "sticker_decisions": sticker_decisions,
                    "image_decisions": image_decisions,
                    "model_messages": model_call.trace.request_messages,
                    "raw_model_response": model_call.trace.response_text,
                    "model_attempt": model_call.trace.attempt,
                    "model_used": model_call.trace.model or str(getattr(runtime.model, "model", "") or ""),
                    "model_ms": model_ms,
                    "total_ms": _ms(started),
                }
                self.repo.add_trace(
                    group.id,
                    source_event.turn_id,
                    character_id,
                    int(source_event.id),
                    now,
                    trace,
                )

            logger.info(
                "group.member conversation=%s turn=%s character=%s actions=%s sticker_candidates=%d memories=%d model_ms=%.1f total_ms=%.1f",
                group.id,
                source_event.turn_id,
                character_id,
                [action.type.value for action in reaction.actions] or ["SILENCE"],
                len(allowed_sticker_ids),
                len(created_memory_ids),
                model_ms,
                _ms(started),
            )
            return {
                "character_id": character_id,
                "actions": [action.model_dump(mode="json") for action in reaction.actions],
                "emitted_event_ids": emitted_event_ids,
                "created_memory_ids": created_memory_ids,
                "perception": reaction.perception,
                "reaction": reaction.reaction,
                "model_ms": model_ms,
            }

    def _send_locked(
        self,
        conversation_id: str,
        message: str,
        *,
        at: datetime | None = None,
        image: dict | None = None,
        image_data_url: str | None = None,
        sticker: dict | None = None,
    ) -> dict:
        group = self.repo.get_group(conversation_id)
        if group is None:
            raise KeyError(f"unknown group: {conversation_id}")
        content = message.strip()
        if image is not None and sticker is not None:
            raise ValueError("send a sticker or image in one group user turn, not both")
        if not content and image is None and sticker is None:
            raise ValueError("group message, sticker or image must not be empty")
        now = at or self.clock.now()
        turn_id = f"turn-{uuid4().hex[:12]}"
        metadata = {"display_text": content}
        runtime_content = content
        if image is not None:
            metadata.update(
                {
                    "media_id": image.get("id"),
                    "media_name": image.get("original_name") or "图片",
                    "media_mime_type": image.get("mime_type") or "",
                    "media_size_bytes": int(image.get("size_bytes") or 0),
                }
            )
            runtime_content = f"{content}\n[用户发送了一张真实图片]".strip()
        elif sticker is not None:
            label = str(sticker.get("label") or sticker.get("id") or "表情包")
            tags = sticker.get("tags") if isinstance(sticker.get("tags"), list) else []
            meaning = "、".join(str(value) for value in tags if str(value).strip()) or str(sticker.get("description") or "")
            metadata.update(
                {
                    "action": ActionType.STICKER.value,
                    "sticker_id": sticker.get("id"),
                    "sticker_label": label,
                    "sticker_meaning": meaning,
                }
            )
            runtime_content = f"[用户发送表情包：{label}{f'；含义：{meaning}' if meaning else ''}]"
        source_event = self.repo.append_event(
            GroupEvent(
                conversation_id=conversation_id,
                turn_id=turn_id,
                actor_type="USER",
                actor_id="user",
                event_type="USER_MESSAGE",
                event_time=now,
                content=runtime_content,
                metadata=metadata,
            )
        )

        members = list(group.member_ids)
        user_turn_count = self.repo.count_user_turns(conversation_id)
        offset = (user_turn_count - 1) % len(members)
        ordered = members[offset:] + members[:offset]
        logger.info(
            "group.turn start conversation=%s turn=%s order=%s image=%s sticker=%s",
            conversation_id,
            turn_id,
            ordered,
            bool(image_data_url),
            (sticker or {}).get("id") or "-",
        )
        decisions = []
        for character_id in ordered:
            decisions.append(
                self._react_member(
                    group=group,
                    source_event=source_event,
                    character_id=character_id,
                    image_data_url=image_data_url,
                )
            )
        logger.info(
            "group.turn done conversation=%s turn=%s responders=%d",
            conversation_id,
            turn_id,
            sum(bool(item["actions"]) for item in decisions),
        )
        return {
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "source_event_id": source_event.id,
            "speaker_order": ordered,
            "decisions": decisions,
            "events": self.repo.list_turn_events(conversation_id, turn_id),
        }

    def send(
        self,
        conversation_id: str,
        message: str,
        *,
        at: datetime | None = None,
        image: dict | None = None,
        image_data_url: str | None = None,
        sticker: dict | None = None,
    ) -> dict:
        with self.turn_lock:
            return self._send_locked(
                conversation_id,
                message,
                at=at,
                image=image,
                image_data_url=image_data_url,
                sticker=sticker,
            )
