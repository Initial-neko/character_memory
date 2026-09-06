from __future__ import annotations

from datetime import datetime, timedelta
import json
import os

from character_memory.app import build_embedding, build_model
from character_memory.config import load_persona, load_settings
from character_memory.domain.models import Event, EventType
from character_memory.life.runner import DayRunner
from character_memory.life.simulator import LifeSimulator
from character_memory.life.ticker import TimeTicker
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


def main():
    import streamlit as st

    st.set_page_config(page_title="Character Memory", page_icon="💬", layout="centered")
    config_path = os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml")
    settings = load_settings(config_path)

    st.markdown(
        """
        <style>
        .block-container {padding-top: 1rem; padding-bottom: 1.5rem; max-width: 920px;}
        .cm-date {text-align:center;color:#8a919b;font-size:.78rem;margin:1rem 0 .65rem}
        .cm-subtle {color:#8a919b;font-size:.8rem}
        [data-testid="stForm"] {border:0;padding:0;margin-top:.35rem;}
        [data-testid="stChatMessage"] {padding-top:.35rem;padding-bottom:.35rem;}
        div[data-testid="stVerticalBlockBorderWrapper"] {border-color:#eef0f3;}
        </style>
        """,
        unsafe_allow_html=True,
    )

    @st.cache_resource(show_spinner=False)
    def get_heavy_runtime(path: str):
        cached_settings = load_settings(path)
        return (
            build_embedding(cached_settings),
            build_model(cached_settings),
            load_persona(cached_settings.persona_path),
        )

    try:
        with st.spinner(f"加载 Embedding：{settings.embedding_model} ..."):
            embeddings, model, persona = get_heavy_runtime(config_path)
    except Exception as exc:
        st.error(f"运行时初始化失败：{exc}")
        st.info("请确认 config.yaml、OPENCODE_GO_API_KEY 和本地 embedding 依赖/模型已经准备好。")
        return

    store = SQLiteStore(settings.db_path)
    recall = VectorRecall(store, embeddings, limit=settings.recall_limit)
    runtime = PersonRuntime(store, recall, embeddings, model, persona)
    life = LifeSimulator(store, embeddings, model, persona, runtime)
    ticker = TimeTicker(store, runtime)
    days = DayRunner(store, life, ticker)

    character_id = st.sidebar.text_input("Character", "rin")
    world_time = days.current_time(character_id)

    st.sidebar.subheader("Runtime")
    st.sidebar.caption(f"Config: {config_path}")
    st.sidebar.caption(f"DB: {settings.db_path}")
    st.sidebar.caption(f"Chat: {settings.chat_model}")
    st.sidebar.caption(f"Embedding: {settings.embedding_model}")
    st.sidebar.metric("World Time", world_time.strftime("%Y-%m-%d %H:%M:%S %z"))

    if st.sidebar.button("同步到现实时间", use_container_width=True):
        store.set_world_time(character_id, datetime.now().astimezone())
        store.close()
        st.rerun()

    t1, t2 = st.sidebar.columns(2)
    if t1.button("+1 小时", use_container_width=True):
        now = days.current_time(character_id)
        with st.spinner("推进时间并处理 due intents ..."):
            ticker.tick(character_id, now)
            store.set_world_time(character_id, now + timedelta(hours=1))
        store.close()
        st.rerun()

    if t2.button("+1 天", use_container_width=True):
        with st.spinner("模拟下一天的生活、时间事件和日记 ..."):
            days.run_next_day(character_id)
        store.close()
        st.rerun()

    sim_days = st.sidebar.number_input("批量模拟天数", min_value=1, max_value=365, value=7, step=1)
    if st.sidebar.button("运行时间模拟", use_container_width=True):
        with st.spinner(f"正在模拟 {sim_days} 天；这会产生真实模型调用 ..."):
            days.simulate(character_id, int(sim_days))
        store.close()
        st.rerun()

    st.sidebar.divider()
    st.sidebar.caption("Perception / Reaction / Mental State / Action Reason 是开发者安全摘要，不是模型隐藏 chain-of-thought。")

    events = store.list_events(character_id, 500)
    memories = store.list_memories(character_id, include_inactive=True)[-300:]
    intents = [dict(row) for row in store.list_intents(character_id, 200)]
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

    head_left, head_right = st.columns([5, 2])
    with head_left:
        st.subheader(character_id)
        st.caption("Persistent AI Person · 直接聊天")
    with head_right:
        st.caption("当前人物时间")
        st.markdown(f"**{days.current_time(character_id).strftime('%m-%d %H:%M')}**")

    st.divider()

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

            meta_cols = st.columns([6, 1.35])
            meta_cols[0].caption(event.event_time.strftime("%H:%M:%S") + suffix)

            source_event_id = (
                event.id
                if event.event_type == EventType.USER_MESSAGE
                else event.metadata.get("source_event_id")
            )
            if source_event_id in trace_by_source:
                if meta_cols[1].button("查看详情", key=f"trace_{event.id}", use_container_width=True):
                    st.session_state["selected_trace_source"] = source_event_id
                    st.rerun()

    with st.form("chat_form", clear_on_submit=True):
        input_col, send_col = st.columns([7, 1.15])
        message = input_col.text_input(
            "消息",
            placeholder=f"给 {character_id} 发消息…",
            label_visibility="collapsed",
        )
        submitted = send_col.form_submit_button("发送", use_container_width=True)

    if submitted and message.strip():
        now = days.current_time(character_id)
        pending_user = st.chat_message("user")
        with pending_user:
            st.write(message.strip())
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
                    content=message.strip(),
                )
            )
            store.set_world_time(character_id, now + timedelta(minutes=1))
            st.session_state["selected_trace_source"] = result.event.id
            if result.reaction.action.message:
                typing.markdown(result.reaction.action.message)
            else:
                typing.caption(f"未发送消息 · {result.reaction.action.type.value}")
        except Exception as exc:
            typing.error(f"生成失败：{exc}")
            store.close()
            return

        store.close()
        st.rerun()

    selected_source = st.session_state.get("selected_trace_source")
    selected_trace = trace_by_source.get(selected_source)

    if selected_trace is not None:
        st.divider()
        detail_head, detail_close = st.columns([6, 1])
        detail_head.subheader("本轮详情")
        if detail_close.button("关闭", use_container_width=True):
            st.session_state.pop("selected_trace_source", None)
            store.close()
            st.rerun()

        source = selected_trace.get("event", {})
        source_content = source.get("content", "")
        st.caption(
            f"{source.get('event_type', '')} · {source.get('event_time', '')}"
            + (f" · {source_content}" if source_content else "")
        )

        decision_tab, context_tab, memory_tab, state_tab, raw_tab = st.tabs(
            ["决策", "模型输入", "Memory", "State / Intent", "Raw"]
        )

        with decision_tab:
            action = selected_trace.get("action", {})
            c1, c2, c3 = st.columns(3)
            c1.metric("Action", action.get("type", ""))
            c2.metric("Recall", len(selected_trace.get("recalled_memories", [])))
            c3.metric("Memory Write", len(selected_trace.get("created_memory_ids", [])))

            st.markdown("##### 最终对外表达")
            if action.get("message"):
                st.success(action["message"])
            else:
                st.info(f"没有发送消息 · {action.get('type', '')}")

            p1, p2 = st.columns(2)
            with p1:
                st.markdown("##### Perception")
                st.write(selected_trace.get("perception", "") or "—")
                st.markdown("##### Reaction")
                st.write(selected_trace.get("reaction", "") or "—")
            with p2:
                st.markdown("##### Action Reason")
                st.write(action.get("reason", "") or "—")
                st.markdown("##### Model Attempt")
                st.write(selected_trace.get("model_attempt", 0) or "—")

            before, after = st.columns(2)
            with before:
                st.markdown("##### Mental State · Before")
                st.write(selected_trace.get("mental_state_before", "") or "暂无")
            with after:
                st.markdown("##### Mental State · After")
                st.write(selected_trace.get("mental_state_after", "") or "暂无")

        with context_tab:
            st.caption("下面展示这一轮实际发送给模型的 messages，以及 Runtime 编译出的 Context。")
            model_messages = selected_trace.get("model_messages", [])
            if model_messages:
                for index, model_message in enumerate(model_messages, start=1):
                    role_name = model_message.get("role", "?")
                    with st.expander(f"{index}. {role_name}", expanded=index <= 2):
                        st.code(model_message.get("content", ""), language="text")
            else:
                st.caption("该模型实现没有提供 request message trace。")

            with st.expander("Compiled Context", expanded=False):
                st.code(selected_trace.get("context", ""), language="text")

        with memory_tab:
            recalled = selected_trace.get("recalled_memories", [])
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
            candidates = selected_trace.get("memory_candidates", [])
            if candidates:
                st.json(candidates)
            else:
                st.caption("本轮没有 Memory Candidate。")
            st.caption(f"实际写入 Memory IDs：{selected_trace.get('created_memory_ids', [])}")

        with state_tab:
            st.markdown("##### 当前 Persona")
            st.code(runtime.persona, language="text")
            st.markdown("##### 当前 Mental State")
            st.write(store.get_mental_state(character_id) or "暂无")
            st.markdown("##### 本轮 Intent Candidates")
            intent_candidates = selected_trace.get("intent_candidates", [])
            if intent_candidates:
                st.json(intent_candidates)
            else:
                st.caption("本轮没有 Intent Candidate。")
            st.caption(f"实际写入 Intent IDs：{selected_trace.get('created_intent_ids', [])}")
            with st.expander("全部 Pending / Historical Intents", expanded=False):
                if intents:
                    st.dataframe(intents, use_container_width=True, hide_index=True)
                else:
                    st.caption("暂无 Intent。")

        with raw_tab:
            raw = selected_trace.get("raw_model_response", "")
            if raw:
                st.code(raw, language="json")
            else:
                st.caption("没有保存 Raw Model Response。")
            with st.expander("完整 Structured Runtime Trace", expanded=False):
                st.json(selected_trace)
            with st.expander("完整 Event Timeline", expanded=False):
                st.dataframe(
                    [
                        {
                            "id": event.id,
                            "time": event.event_time,
                            "type": event.event_type.value,
                            "content": event.content,
                            "metadata": json.dumps(event.metadata, ensure_ascii=False),
                        }
                        for event in reversed(events)
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
            with st.expander("全部 Memory", expanded=False):
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

    store.close()


if __name__ == "__main__":
    main()
