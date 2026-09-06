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

    st.set_page_config(page_title="Character Memory Research Console", page_icon="🧠", layout="wide")
    config_path = os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml")
    settings = load_settings(config_path)

    st.markdown(
        """
        <style>
        .block-container {padding-top: 1.2rem; padding-bottom: 2rem;}
        .cm-date {text-align:center;color:#8a919b;font-size:.8rem;margin:1rem 0}
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

    st.title("Character Memory · Research Console")
    st.caption("直接聊天，同时检查时间、Recall、上下文、人物反应、Mental State、Action、Memory Write 与 Intent。")

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
    st.sidebar.caption("这里展示开发者安全摘要，不展示模型隐藏 chain-of-thought。")

    events = store.list_events(character_id, 400)
    memories = store.list_memories(character_id, include_inactive=True)[-300:]
    intents = [dict(row) for row in store.list_intents(character_id, 200)]

    chat_tab, trace_tab, timeline_tab, memory_tab, state_tab = st.tabs(
        ["💬 对话", "🔬 本轮调试", "🕒 Timeline", "🧠 Memory", "📌 State / Intent"]
    )

    with chat_tab:
        st.subheader(character_id)
        st.caption(f"当前人物时间：{days.current_time(character_id).strftime('%Y-%m-%d %H:%M')}")

        chat_events = [
            event
            for event in events
            if event.event_type in {EventType.USER_MESSAGE, EventType.CHARACTER_MESSAGE}
        ]
        last_date = None
        if not chat_events:
            st.info("还没有聊天记录。直接在下面输入第一句话。")

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
                    elif action_name:
                        suffix = f" · {action_name}"
                st.caption(event.event_time.strftime("%H:%M:%S") + suffix)

        message = st.chat_input("给角色发送消息")
        if message:
            now = days.current_time(character_id)
            with st.spinner("人物正在感知、Recall、反应并决定是否表达 ..."):
                result = runtime.handle(
                    Event(
                        character_id=character_id,
                        event_type=EventType.USER_MESSAGE,
                        event_time=now,
                        content=message,
                    )
                )
                store.set_world_time(character_id, now + timedelta(minutes=1))
                st.session_state["selected_trace_source"] = result.event.id
            store.close()
            st.rerun()

    trace_events = [
        event
        for event in events
        if event.event_type == EventType.ACTION and event.metadata.get("trace")
    ]

    with trace_tab:
        st.subheader("Runtime Trace")
        st.caption("选择任意一次 Runtime 决策，查看当时实际获得的上下文和最终对外行为。")

        if not trace_events:
            st.info("还没有可查看的 Runtime Trace。发送一条消息或推进时间后这里会出现。")
        else:
            def trace_label(action_event):
                trace = action_event.metadata["trace"]
                source = trace.get("event", {})
                content = str(source.get("content", "")).replace("\n", " ")
                if len(content) > 32:
                    content = content[:32] + "…"
                return (
                    f"{action_event.event_time.strftime('%m-%d %H:%M:%S')} · "
                    f"{source.get('event_type', '?')} · {content} · {action_event.content}"
                )

            source_preference = st.session_state.get("selected_trace_source")
            default_index = len(trace_events) - 1
            if source_preference is not None:
                for idx, item in enumerate(trace_events):
                    if item.metadata["trace"].get("source_event_id") == source_preference:
                        default_index = idx
                        break

            selected = st.selectbox(
                "选择一轮",
                trace_events,
                index=default_index,
                format_func=trace_label,
            )
            trace = selected.metadata["trace"]
            action = trace.get("action", {})

            a1, a2, a3, a4 = st.columns(4)
            a1.metric("Action", action.get("type", ""))
            a2.metric("Model Attempt", trace.get("model_attempt", 0) or "n/a")
            a3.metric("Recall", len(trace.get("recalled_memories", [])))
            a4.metric("Memory Writes", len(trace.get("created_memory_ids", [])))

            st.markdown("#### 用户真正收到什么")
            if action.get("message"):
                st.success(action["message"])
            else:
                st.info(f"没有对外发送消息。Action = {action.get('type', '')}")

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("#### Perception")
                st.write(trace.get("perception", "") or "—")
                st.markdown("#### Reaction")
                st.write(trace.get("reaction", "") or "—")
            with c2:
                st.markdown("#### Action Reason")
                st.write(action.get("reason", "") or "—")
                st.markdown("#### Source Event")
                st.json(trace.get("event", {}), expanded=False)

            st.markdown("#### Mental State")
            before, after = st.columns(2)
            with before:
                st.caption("Before")
                st.write(trace.get("mental_state_before", "") or "暂无")
            with after:
                st.caption("After")
                st.write(trace.get("mental_state_after", "") or "暂无")

            st.markdown("#### Recall 到的 Memory")
            recalled = trace.get("recalled_memories", [])
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

            st.markdown("#### 实际发送给模型的 Messages")
            model_messages = trace.get("model_messages", [])
            if model_messages:
                for index, model_message in enumerate(model_messages, start=1):
                    role = model_message.get("role", "?")
                    with st.expander(
                        f"{index}. {role}",
                        expanded=role in {"system", "user"} and len(model_messages) <= 2,
                    ):
                        st.code(model_message.get("content", ""), language="text")
            else:
                st.caption("该模型实现没有提供 request message trace。")

            with st.expander("Compiled Context（Person Runtime 生成）", expanded=False):
                st.code(trace.get("context", ""), language="text")

            structured = {
                "perception": trace.get("perception"),
                "reaction": trace.get("reaction"),
                "mental_state_before": trace.get("mental_state_before"),
                "mental_state_after": trace.get("mental_state_after"),
                "action": trace.get("action"),
                "memory_candidates": trace.get("memory_candidates", []),
                "created_memory_ids": trace.get("created_memory_ids", []),
                "intent_candidates": trace.get("intent_candidates", []),
                "created_intent_ids": trace.get("created_intent_ids", []),
            }
            with st.expander("Structured Runtime Output", expanded=True):
                st.json(structured)

            raw = trace.get("raw_model_response", "")
            if raw:
                with st.expander("Raw Model Response", expanded=False):
                    st.code(raw, language="json")

    with timeline_tab:
        st.subheader("完整 Event Timeline")
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

    with memory_tab:
        st.subheader("Language Memory")
        st.caption("Embedding 本身不直接展示；这里看 Memory 文本、类型、重要度、来源和是否 active。")
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

    with state_tab:
        st.subheader("当前人物状态")
        st.markdown("#### Persona")
        st.code(runtime.persona, language="text")
        st.markdown("#### Mental State")
        st.write(store.get_mental_state(character_id) or "暂无")
        st.markdown("#### Pending / Historical Intents")
        if intents:
            st.dataframe(intents, use_container_width=True, hide_index=True)
        else:
            st.caption("暂无 Intent。")

    store.close()


if __name__ == "__main__":
    main()
