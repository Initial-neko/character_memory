from __future__ import annotations

from datetime import datetime
import logging
import threading
import time
from typing import Callable
from uuid import uuid4

from character_memory.domain.models import ActionType, EXPRESSIVE_ACTIONS, Event, EventType, Memory
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.runtime.context import compile_context
from character_memory.voice_message_fields import voice_pending_fields


logger = logging.getLogger("character_memory.application.group")


class SupersededGroupReaction(RuntimeError):
    """Raised when newer shared user facts arrive before a member can commit."""


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def resolve_group_mentions(
    member_ids: list[str],
    message: str,
    profiles: dict[str, dict] | None = None,
    explicit_mentions: list[str] | None = None,
) -> list[str]:
    """Resolve visible @tokens to stable Character IDs.

    Explicit IDs from API clients are authoritative and validated. Text parsing
    is a fallback so manually typing ``@Name`` still produces structured Event
    metadata. ``*`` represents @所有人.
    """
    members = list(dict.fromkeys(str(value).strip() for value in member_ids if str(value).strip()))
    member_set = set(members)
    result: list[str] = []

    for raw in explicit_mentions or []:
        value = str(raw).strip()
        if not value:
            continue
        if value != "*" and value not in member_set:
            raise ValueError(f"Unknown mentioned character: {value}")
        if value not in result:
            result.append(value)
    if "*" in result:
        return ["*"]

    text = str(message or "")
    occurrences: list[tuple[int, int, str]] = []
    all_index = text.find("@所有人")
    if all_index >= 0:
        occurrences.append((all_index, -len("@所有人"), "*"))

    profile_map = profiles or {}
    terminal = set(" \t\r\n，。！？,.!?;；:：()（）[]【】{}<>《》\"'、")
    for character_id in members:
        profile = profile_map.get(character_id) or {}
        name = str(profile.get("name") or character_id).strip()
        tokens = list(dict.fromkeys([f"@{name}", f"@{character_id}"]))
        for token in tokens:
            start = 0
            while True:
                index = text.find(token, start)
                if index < 0:
                    break
                end = index + len(token)
                if end >= len(text) or text[end] in terminal:
                    occurrences.append((index, -len(token), character_id))
                start = index + len(token)

    for _, _, character_id in sorted(occurrences):
        if character_id == "*":
            return ["*"]
        if character_id not in result:
            result.append(character_id)
    return result


def build_group_user_event(
    conversation_id: str,
    message: str,
    *,
    at: datetime,
    image: dict | None = None,
    sticker: dict | None = None,
    mentions: list[str] | None = None,
) -> GroupEvent:
    content = message.strip()
    if image is not None and sticker is not None:
        raise ValueError("send a sticker or image in one group user turn, not both")
    if not content and image is None and sticker is None:
        raise ValueError("group message, sticker or image must not be empty")

    turn_id = f"turn-{uuid4().hex[:12]}"
    metadata = {"display_text": content, "mentions": list(mentions or [])}
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

    return GroupEvent(
        conversation_id=conversation_id,
        turn_id=turn_id,
        actor_type="USER",
        actor_id="user",
        event_type="USER_MESSAGE",
        event_time=at,
        content=runtime_content,
        metadata=metadata,
    )


class GroupConversationService:
    """Shared group-conversation orchestrator.

    User facts can be persisted independently from reaction generation. Visible
    character actions are committed sequentially so later members can observe
    earlier member messages. A commit guard lets an asynchronous scheduler discard
    any not-yet-committed reaction when a newer user fact arrives.
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

    def _latest_user_event_id(self, conversation_id: str) -> int | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT id FROM conversation_events WHERE conversation_id=? AND actor_type='USER' "
                "AND event_time_epoch IS NOT NULL ORDER BY event_time_epoch DESC,id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return int(row["id"]) if row else None

    def _user_turn_index(self, conversation_id: str, source_event_id: int) -> int:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM conversation_events WHERE conversation_id=? AND actor_type='USER' AND id<=?",
                (conversation_id, int(source_event_id)),
            ).fetchone()
        return int(row["n"] if row else 0)

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

    def persist_user_event(
        self,
        conversation_id: str,
        message: str,
        *,
        at: datetime | None = None,
        image: dict | None = None,
        sticker: dict | None = None,
        mentions: list[str] | None = None,
    ) -> GroupEvent:
        group = self.repo.get_group(conversation_id)
        if group is None:
            raise KeyError(f"unknown group: {conversation_id}")
        resolved_mentions = resolve_group_mentions(group.member_ids, message, self.profile_by_id, mentions)
        event = build_group_user_event(
            conversation_id,
            message,
            at=at or self.clock.now(),
            image=image,
            sticker=sticker,
            mentions=resolved_mentions,
        )
        stored = self.repo.append_event(event)
        logger.info("group.persist conversation=%s turn=%s event_id=%s mentions=%s", conversation_id, stored.turn_id, stored.id, resolved_mentions)
        return stored

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
                    metadata={"group_event_id": item.id, "conversation_id": group_id, "mentions": item.metadata.get("mentions", [])},
                )
            )
        return result

    def _group_contract(self, group, character_id: str, mentioned_ids: list[str] | None = None) -> str:
        names = "、".join(self._name(member_id) for member_id in group.member_ids)
        mentions = list(mentioned_ids or [])
        explicitly_mentioned = "*" in mentions or character_id in mentions
        if explicitly_mentioned:
            mention_guidance = "\nUser 在当前连续表达中明确 @ 了你。这是很强的注意力和回复倾向信号；优先认真理解并自然回应，但如果人物状态或语境确实适合沉默，actions=[] 仍然合法。"
        elif mentions:
            target_names = "、".join(self._name(value) for value in mentions if value in group.member_ids)
            mention_guidance = f"\nUser 当前主要 @ 了 {target_names or '其他群成员'}。你没有被直接点名，不要为了抢话而机械插入；但如果你有自然的情绪反应、不同意见、必要补充，或想回应他们刚说的话，仍然可以正常参与。"
        else:
            mention_guidance = ""
        return f"""

# Group Conversation Contract
你现在位于群聊「{group.name}」。群成员：User、{names}。
你是 {self._name(character_id)}，只代表自己说话，不代替其他成员总结或回答。
群里出现消息不代表你必须回复；如果别人已经表达了与你相同的意思、当前话题与你关系不大、你没有自然补充，actions=[] 是正常且优先允许的选择。
不要机械重复别人刚说的话，不要为了保持群活跃度而插话，也不要因为你“能回答”就一定回答。
你可以自然回应 User，也可以回应其他 Character 刚刚说的话；后说话时要把本轮已经出现的群消息当成真实发生的共同经历。
用户可能连续发送多条消息。Recent Events 才是当前共享事实；不要假设每条用户消息都必须得到一条单独回复。{mention_guidance}
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
        image_data_urls: list[str] | None = None,
        commit_guard: Callable[[], bool] | None = None,
        mentioned_ids: list[str] | None = None,
    ) -> dict:
        runtime = self.runtimes[character_id]
        started = time.perf_counter()
        with self._member_lock(character_id):
            now = source_event.event_time
            self.store.set_world_time(character_id, now)
            recall_query = str(source_event.metadata.get("display_text") or source_event.content or "").strip()
            if source_event.metadata.get("media_id"):
                recall_query = f"{recall_query} 群聊图片".strip()
            if source_event.metadata.get("action") == ActionType.STICKER.value:
                recall_query = f"{source_event.metadata.get('sticker_label') or '表情包'} {source_event.metadata.get('sticker_meaning') or ''}".strip()
            recent = self._recent_as_events(group.id)
            person_context = runtime.context_builder.build(
                character_id,
                query=recall_query,
                at=now,
                recent_events=recent,
                recent_limit=14,
            )
            state_before = person_context.mental_state
            memories = person_context.memories
            recent = person_context.recent_events
            sticker_query = "\n".join(item.content.strip() for item in recent[-4:] if (item.content or "").strip()) or recall_query
            sticker_retrieval = runtime.sticker_retriever.retrieve(runtime.sticker_catalog, sticker_query)
            prompt_stickers = sticker_retrieval.catalog if sticker_retrieval is not None else None
            allowed_sticker_ids = {match.sticker_id for match in sticker_retrieval.matches} if sticker_retrieval is not None else set()
            synthetic = Event(
                character_id=character_id,
                event_type=EventType.USER_MESSAGE,
                event_time=now,
                content=f"群聊当前最新用户事实：{source_event.content}",
                metadata={
                    "conversation_id": group.id,
                    "group_turn_id": source_event.turn_id,
                    "source_conversation_event_id": source_event.id,
                    "mentions": list(mentioned_ids or []),
                    "explicitly_mentioned": "*" in (mentioned_ids or []) or character_id in (mentioned_ids or []),
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
            ) + self._group_contract(group, character_id, mentioned_ids)
            session_id = f"group:{group.id}:{character_id}"
            model_started = time.perf_counter()
            if image_data_urls:
                model_call = runtime.model.react_call_with_images_for_session(context, image_data_urls, session_id)
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
            accepted_memories, memory_decisions = runtime._prepare_memory_writes(character_id, now, reaction.memory_candidates)
            created_memory_ids: list[int] = []
            emitted_events: list[GroupEvent] = []

            with self.store.transaction():
                if commit_guard is not None and not commit_guard():
                    raise SupersededGroupReaction(
                        f"group reaction for source event {source_event.id} was superseded"
                    )
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
                    if action.type not in EXPRESSIVE_ACTIONS:
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
                    elif action.type == ActionType.VOICE_MESSAGE:
                        if not (action.message or "").strip():
                            continue
                        content = (action.message or "").strip()
                        metadata.update(voice_pending_fields())
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
                    emitted_events.append(saved_event)

                trace = {
                    "perception": reaction.perception,
                    "reaction": reaction.reaction,
                    "actions": [action.model_dump(mode="json") for action in reaction.actions],
                    "mental_state_before": state_before,
                    "mental_state_after": state_after,
                    "recalled_memories": [memory.model_dump(mode="json", exclude={"embedding"}) for memory in memories],
                    "memory_decisions": memory_decisions,
                    "created_memory_ids": created_memory_ids,
                    "mentions": list(mentioned_ids or []),
                    "explicitly_mentioned": "*" in (mentioned_ids or []) or character_id in (mentioned_ids or []),
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
                self.repo.add_trace(group.id, source_event.turn_id, character_id, int(source_event.id), now, trace)

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
                "emitted_event_ids": [event.id for event in emitted_events],
                "emitted_events": [event.model_dump(mode="json") for event in emitted_events],
                "created_memory_ids": created_memory_ids,
                "perception": reaction.perception,
                "reaction": reaction.reaction,
                "model_ms": model_ms,
                "explicitly_mentioned": "*" in (mentioned_ids or []) or character_id in (mentioned_ids or []),
            }

    def react_from_event(
        self,
        source_event: GroupEvent,
        *,
        image_data_urls: list[str] | None = None,
        commit_guard: Callable[[], bool] | None = None,
        on_member: Callable[[dict], None] | None = None,
        mention_order: list[str] | None = None,
    ) -> dict:
        group = self.repo.get_group(source_event.conversation_id)
        if group is None:
            raise KeyError(f"unknown group: {source_event.conversation_id}")
        members = list(group.member_ids)
        user_turn_index = self._user_turn_index(group.id, int(source_event.id))
        offset = max(user_turn_index - 1, 0) % len(members)
        base_order = members[offset:] + members[:offset]
        mentions = list(mention_order if mention_order is not None else (source_event.metadata.get("mentions") or []))
        if "*" in mentions:
            explicit_mentions = list(members)
            ordered = base_order
        else:
            explicit_mentions = [value for value in mentions if value in members]
            explicit_mentions = list(dict.fromkeys(explicit_mentions))
            ordered = explicit_mentions + [value for value in base_order if value not in explicit_mentions]
        logger.info(
            "group.reaction start conversation=%s source_event=%s order=%s mentions=%s",
            group.id,
            source_event.id,
            ordered,
            mentions,
        )
        decisions = []
        for character_id in ordered:
            if commit_guard is not None and not commit_guard():
                raise SupersededGroupReaction(
                    f"group reaction for source event {source_event.id} was superseded before {character_id}"
                )
            try:
                decision = self._react_member(
                    group=group,
                    source_event=source_event,
                    character_id=character_id,
                    image_data_urls=image_data_urls,
                    commit_guard=commit_guard,
                    mentioned_ids=explicit_mentions,
                )
            except SupersededGroupReaction:
                # Supersession is a conversation-level ordering signal, not a
                # member-local failure. It must still abort this stale turn.
                raise
            except Exception as exc:
                # Each character is an independent participant. A malformed model
                # result or provider hiccup should look like that member missing a
                # turn, not like the whole room disappearing. Errors stay visible
                # in logs/result metadata and an all-member failure is escalated
                # below so systemic runtime/storage outages are not hidden.
                logger.exception(
                    "group.member failed conversation=%s turn=%s character=%s error=%s",
                    group.id,
                    source_event.turn_id,
                    character_id,
                    exc,
                )
                decision = {
                    "character_id": character_id,
                    "actions": [],
                    "emitted_event_ids": [],
                    "emitted_events": [],
                    "created_memory_ids": [],
                    "perception": "",
                    "reaction": "",
                    "model_ms": 0.0,
                    "explicitly_mentioned": "*" in explicit_mentions or character_id in explicit_mentions,
                    "error": str(exc),
                }
            decisions.append(decision)
            if on_member is not None:
                on_member(decision)

        failures = [item for item in decisions if item.get("error")]
        if decisions and len(failures) == len(decisions):
            summary = "; ".join(f"{item['character_id']}: {item['error']}" for item in failures)
            raise RuntimeError(f"all group members failed for source event {source_event.id}: {summary}")

        logger.info("group.reaction done conversation=%s source_event=%s responders=%d failures=%d", group.id, source_event.id, sum(bool(item["actions"]) for item in decisions), len(failures))
        return {
            "conversation_id": group.id,
            "turn_id": source_event.turn_id,
            "source_event_id": source_event.id,
            "speaker_order": ordered,
            "mentions": mentions,
            "decisions": decisions,
            "events": self.repo.list_turn_events(group.id, source_event.turn_id),
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
        mentions: list[str] | None = None,
    ) -> dict:
        source_event = self.persist_user_event(
            conversation_id,
            message,
            at=at,
            image=image,
            sticker=sticker,
            mentions=mentions,
        )
        return self.react_from_event(
            source_event,
            image_data_urls=[image_data_url] if image_data_url else None,
        )

    def send(
        self,
        conversation_id: str,
        message: str,
        *,
        at: datetime | None = None,
        image: dict | None = None,
        image_data_url: str | None = None,
        sticker: dict | None = None,
        mentions: list[str] | None = None,
    ) -> dict:
        with self.turn_lock:
            return self._send_locked(
                conversation_id,
                message,
                at=at,
                image=image,
                image_data_url=image_data_url,
                sticker=sticker,
                mentions=mentions,
            )
