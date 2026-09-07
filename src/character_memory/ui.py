from __future__ import annotations

from datetime import datetime
import json
import logging
import os
import time

from character_memory.app import build_embedding, build_model
from character_memory.config import load_persona, load_settings
from character_memory.domain.models import Event, EventType
from character_memory.logging_utils import configure_logging
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


configure_logging()
logger = logging.getLogger("character_memory.ui")


def main():
    import streamlit as st

    st.set_page_config(page_title="Character Memory", page_icon="💬", layout="centered")
    config_path = os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml")
    settings = load_settings(config_path)

    if not st.session_state.get("_cm_session_logged"):
        logger.info(
            "ui.session open config=%s db=%s model=%s embedding=%s/%s",
            config_path,
            settings.db_path,
            settings.chat_model,
            settings.embedding_provider,
            settings.embedding_model,
        )
        st.session_state["_cm_session_logged"] = True

    st.markdown(
        """
        <style>
        .block-container {padding-top:.8rem;padding-bottom:5rem;max-width:900px;}
        .cm-date {text-align:center;color:#8a919b;font-size:.76rem;margin:1rem 0 .7rem}
        .cm-header {position:sticky;top:0;z-index:8;background:rgba(255,255,255,.94);backdrop-filter:blur(8px);padding:.25rem 0 .45rem;}
        [data-testid="stChatMessage"] {padding-top:.28rem;padding-bottom:.28rem;}
        [data-testid="stChatInput"] {max-width:900px;margin:auto;}
        button[kind="secondary"] {border-color:#e8eaed;}
        </style>
        """,
        unsafe_allow_html=True,
    )

    @st.cache_resource(show_spinner=False)
    def get_heavy_runtime(path: str):
        cached_settings = load_settings(path)
        total_started = time.perf_counter()

        logger.info(
            "ui.runtime_load embedding start provider=%s model=%s",
            cached_settings.embedding_provider,
            cached_settings.embedding_model,
        )
        stage_started = time.perf_counter()
        embeddings = build_embedding(cached_settings)
        logger.info(
            "ui.runtime_load embedding ready duration_ms=%d",
            int((time.perf_counter() - stage_started) * 1000),
        )

        logger.info("ui.runtime_load provider start model=%s", cached_settings.chat_model)
        stage_started = time.perf_counter()
        model = build_model(cached_settings)
        logger.info(
            "ui.runtime_load provider ready model=%s session=%s duration_ms=%d",
            cached_settings.chat_model,
            getattr(model, "session_id", "-"),
            int((time.perf_counter() - stage_started) * 1000),
        )

        logger.info("ui.runtime_load persona start path=%s", cached_settings.persona_path)
        persona = load_persona(cached_settings.persona_path)
        logger.info(
            "ui.runtime_load done total_ms=%d persona_chars=%d",
            int((time.perf_counter() - total_started) * 1000),
            len(persona),
        )
        return embeddings, model, persona

    try:
        with st.spinner(f"加载 Embedding：{settings.embedding_model} ..."):
            embeddings, model, persona = get_heavy_runtime(config_path)
    except Exception as exc:
        logger.exception("ui.runtime_load failed error=%s", exc)
        st.error(f"运行时初始化失败：{exc}")
        st.info("请确认 config.yaml、OPENCODE_GO_API_KEY 和本地 embedding 依赖/模型已经准备好。")
        return

    store = SQLiteStore(settings.db_path)
    character_id = st.session_state.get("character_id", "rin")

    # Web chat is production-like: runtime time follows wall clock on every rerun.
    real_now = datetime.now().astimezone()
    store.set_world_time(character_id, real_now)

    recall = VectorRecall(store, embeddings, limit=settings.recall_limit)
    runtime = PersonRuntime(store, recall, embeddings, model, persona)

    # Only recent events are loaded for the normal chat surface. Heavy Memory/Intent
    # tables are fetched lazily inside dialogs.
    events = store.list_events(character_id, 180)
    chat_events = [
        event
        for event in events
        if event.event_type in {EventType.USER_MESSAGE, EventType.CHARACTER_MESSAGE}
    ]
    trace_events = [
        event
        for event in events
        if event.event_type == EventType.ACTION and event.metadata.get("trace")
    ]
    trace_by_source = {
        event.metadata["trace"].get("source_event_id"): event.metadata["trace"]
        for event in trace_events
        if event.metadata["trace"].get("source_event_id") is not None
    }

    if not st.session_state.get("_cm_ready_logged"):
        logger.info(
            "ui.ready character=%s recent_events=%d chat_messages=%d traces=%d provider_session=%s",
            character_id,
            len(events),
            len(chat_events),
            len(trace_events),
            getattr(model, "session_id", "-"),
        )
        st.session_state["_cm_ready_logged"] = True

    @st.dialog("Runtime")
    def show_runtime():
        logger.info("ui.runtime_dialog open character=%s", character_id)
        now = datetime.now().astimezone()
        st.caption("WebUI 使用现实时间；每次交互都会重新读取当前系统时间。")
        st.metric("现在", now.strftime("%Y-%m-%d %H:%M:%S %z"))
        st.markdown("##### Provider")
        st.code(
            f"chat_model = {settings.chat_model}\n"
            f"base_url = {settings.base_url}\n"
            f"session = {getattr(model, 'session_id', '-')}\n"
            f"embedding = {settings.embedding_provider} / {settings.embedding_model}\n"
            f"db = {settings.db_path}",
            language="text",
        )
        st.markdown("##### Persona")
        st.code(runtime.persona, language="text")
        st.markdown("##### Mental State")
        st.write(store.get_mental_state(character_id) or "暂无")

        intents = [dict(row) for row in store.list_intents(character_id, 80)]
        memories = store.list_memories(character_id, include_inactive=True)[-80:]
        with st.expander(f"Memory（最近 {len(memories)} 条）"):
            st.dataframe(
                [
                    {
                        "id": memory.id,
                        "time": memory.event_time,
                        "type": memory.memory_type,
                        "importance": memory.importance,
                        "active": memory.active,
                        "content": memory.content,
                        "source_event_id": memory.source_event_id,
                    }
                    for memory in reversed(memories)
                ],
                use_container_width=True,
                hide_index=True,
            )
        with st.expander(f"Intent（最近 {len(intents)} 条）"):
            if intents:
                st.dataframe(intents, use_container_width=True, hide_index=True)
            else:
                st.caption("暂无 Intent。")

    @st.dialog("本轮详情")
    def show_trace(trace):
        source = trace.get("event", {})
        logger.info(
            "ui.trace_dialog open source_event_id=%s event_type=%s",
            trace.get("source_event_id"),
            source.get("event_type", ""),
        )
        source_content = source.get("content", "")
        st.caption(
            f"{source.get('event_type', '')} · {source.get('event_time', '')}"
            + (f" · {source_content}" if source_content else "")
        )

        decision_tab, context_tab, memory_tab, state_tab, raw_tab = st.tabs(
            ["决策", "模型输入", "Memory", "State / Intent", "Raw"]
        )

        with decision_tab:
            action = trace.get("action", {})
            c1, c2, c3 = st.columns(3)
            c1.metric("Action", action.get("type", ""))
            c2.metric("Recall", len(trace.get("recalled_memories", [])))
            c3.metric("Memory Write", len(trace.get("created_memory_ids", [])))

            st.markdown("##### 最终对外表达")
            if action.get("message"):
                st.success(action["message"])
            else:
                st.info(f"没有发送消息 · {action.get('type', '')}")

            st.markdown("##### Perception")
            st.write(trace.get("perception", "") or "—")
            st.markdown("##### Reaction")
            st.write(trace.get("reaction", "") or "—")
            st.markdown("##### Action Reason")
            st.write(action.get("reason", "") or "—")

            before, after = st.columns(2)
            with before:
                st.markdown("##### Mental State · Before")
                st.write(trace.get("mental_state_before", "") or "暂无")
            with after:
                st.markdown("##### Mental State · After")
                st.write(trace.get("mental_state_after", "") or "暂无")

        with context_tab:
            st.caption("这一轮真正发送给模型的 messages，以及 Runtime 编译出的完整 Context。")
            model_messages = trace.get("model_messages", [])
            if model_messages:
                for index, model_message in enumerate(model_messages, start=1):
                    role_name = model_message.get("role", "?")
                    with st.expander(f"{index}. {role_name}", expanded=index <= 2):
                        st.code(model_message.get("content", ""), language="text")
            else:
                st.caption("该模型实现没有 request message trace。")
            with st.expander("Compiled Context", expanded=True):
                st.code(trace.get("context", ""), language="text")

        with memory_tab:
            recalled = trace.get("recalled_memories", [])
            st.markdown("##### 本轮 Recall")
            if recalled:
                st.dataframe(
                    [
                        {
                            "id": item.get("id"),
                            "time": item.get("event_time"),
                            "type": item.get("memory_type"),
                            "importance": item.get("importance"),
                            "content": item.get("content"),
                            "source_event_id": item.get("source_event_id"),
                        }
                        for item in recalled
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.caption("本轮没有 Recall 到 Memory。")

            st.markdown("##### Memory Candidates")
            candidates = trace.get("memory_candidates", [])
            if candidates:
                st.json(candidates)
            else:
                st.caption("本轮没有 Memory Candidate。")
            st.caption(f"实际写入 Memory IDs：{trace.get('created_memory_ids', [])}")

        with state_tab:
            st.markdown("##### Mental State")
            st.write(store.get_mental_state(character_id) or "暂无")
            st.markdown("##### Intent Candidates")
            candidates = trace.get("intent_candidates", [])
            if candidates:
                st.json(candidates)
            else:
                st.caption("本轮没有 Intent Candidate。")
            st.caption(f"实际写入 Intent IDs：{trace.get('created_intent_ids', [])}")

        with raw_tab:
            raw = trace.get("raw_model_response", "")
            if raw:
                st.code(raw, language="json")
            else:
                st.caption("没有保存 Raw Model Response。")
            with st.expander("完整 Structured Runtime Trace"):
                st.json(trace)

    # Header: chat first, runtime stays out of the way until requested.
    st.markdown("<div class='cm-header'>", unsafe_allow_html=True)
    h1, h2, h3 = st.columns([5, 2, 1.25])
    with h1:
        st.subheader(character_id)
        st.caption("Persistent AI Person")
    with h2:
        st.caption("现在")
        st.markdown(f"**{real_now.strftime('%m-%d %H:%M')}**")
    with h3:
        if st.button("Runtime", use_container_width=True):
            show_runtime()
    st.markdown("</div>", unsafe_allow_html=True)

    if not chat_events:
        st.info("还没有聊天记录。直接在下面输入第一句话。")

    last_date = None
    for event in chat_events:
        current_date = event.event_time.date()
        if current_date != last_date:
            st.markdown(
                f"<div class='cm-date'>── {current_date.isoformat()} ──</div>",
                unsafe_allow_html=True,
            )
            last_date = current_date

        role = "user" if event.event_type == EventType.USER_MESSAGE else "assistant"
        with st.chat_message(role):
            st.write(event.content)

            suffix = ""
            if event.event_type == EventType.CHARACTER_MESSAGE:
                action_name = event.metadata.get("action", "")
                if action_name == "PROACTIVE_MESSAGE":
                    suffix = " · 主动消息"

            source_event_id = (
                event.id if event.event_type == EventType.USER_MESSAGE else event.metadata.get("source_event_id")
            )
            meta_left, meta_right = st.columns([8, 1.15])
            meta_left.caption(event.event_time.strftime("%H:%M:%S") + suffix)
            trace = trace_by_source.get(source_event_id)
            if trace is not None and meta_right.button("···", key=f"trace_{event.id}", help="查看本轮详情"):
                show_trace(trace)

    message = st.chat_input(f"给 {character_id} 发消息")
    if message and message.strip():
        now = datetime.now().astimezone()
        clean_message = message.strip()
        logger.info(
            "ui.chat submit character=%s at=%s message_chars=%d",
            character_id,
            now.isoformat(),
            len(clean_message),
        )
        store.set_world_time(character_id, now)

        with st.chat_message("user"):
            st.write(clean_message)
            st.caption(now.strftime("%H:%M:%S"))

        with st.chat_message("assistant"):
            typing = st.empty()
            typing.markdown(f"*{character_id} 正在输入中…*")

        try:
            result = runtime.handle(
                Event(
                    character_id=character_id,
                    event_type=EventType.USER_MESSAGE,
                    event_time=now,
                    content=clean_message,
                )
            )
            logger.info(
                "ui.chat result character=%s event_id=%s action=%s reply_chars=%d recalled=%d memory_writes=%d intent_writes=%d",
                character_id,
                result.event.id,
                result.reaction.action.type.value,
                len(result.reaction.action.message or ""),
                len(result.recalled_memories),
                len(result.created_memory_ids),
                len(result.created_intent_ids),
            )
            if result.reaction.action.message:
                typing.markdown(result.reaction.action.message)
            else:
                typing.caption(f"未发送消息 · {result.reaction.action.type.value}")
        except Exception as exc:
            logger.exception("ui.chat failed character=%s error=%s", character_id, exc)
            typing.error(f"生成失败：{exc}")
            store.close()
            return

        store.close()
        st.rerun()

    store.close()


if __name__ == "__main__":
    main()
