from __future__ import annotations

import logging
import os

from character_memory.config import load_persona, load_settings
from character_memory.logging_utils import configure_logging
from character_memory.storage.sqlite import SQLiteStore


configure_logging()
logger = logging.getLogger("character_memory.inspector")


def main():
    import streamlit as st

    st.set_page_config(page_title="Character Memory Inspector", page_icon="🔎", layout="wide")
    config_path = os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml")
    settings = load_settings(config_path)
    store = SQLiteStore(settings.db_path)

    st.title("Character Memory · Developer Inspector")
    st.caption("只读调试工具。正常聊天请使用 `character-memory web` / FastAPI HTML 页面。")

    character_id = st.text_input("Character", "rin")
    logger.info("inspector.open character=%s db=%s", character_id, settings.db_path)

    top1, top2, top3 = st.columns(3)
    top1.metric("Chat Model", settings.chat_model)
    top2.metric("Embedding", settings.embedding_model)
    top3.metric("Trace Count", len(store.list_runtime_trace_sources(character_id)))

    state_tab, chat_tab, memory_tab, intent_tab, trace_tab = st.tabs(["State", "Chat", "Memory", "Intent", "Trace"])

    with state_tab:
        st.subheader("Mental State")
        st.write(store.get_mental_state(character_id) or "暂无")
        st.subheader("Persona")
        st.code(load_persona(settings.persona_path), language="yaml")
        st.subheader("Runtime")
        st.code(f"db = {settings.db_path}\nbase_url = {settings.base_url}\nchat_model = {settings.chat_model}\nembedding = {settings.embedding_provider} / {settings.embedding_model}", language="text")

    with chat_tab:
        events = store.list_chat_events(character_id, limit=300)
        st.dataframe([{"id": e.id, "time": e.event_time, "type": e.event_type.value, "content": e.content, "source_event_id": e.metadata.get("source_event_id"), "action": e.metadata.get("action")} for e in reversed(events)], use_container_width=True, hide_index=True)

    with memory_tab:
        memories = store.list_memories(character_id, include_inactive=True, limit=200, include_embedding=False)
        st.dataframe([{"id": m.id, "time": m.event_time, "type": m.memory_type, "importance": m.importance, "active": m.active, "content": m.content, "source_event_id": m.source_event_id} for m in reversed(memories)], use_container_width=True, hide_index=True)

    with intent_tab:
        intents = [dict(row) for row in store.list_intents(character_id, limit=200)]
        if intents:
            st.dataframe(intents, use_container_width=True, hide_index=True)
        else:
            st.caption("暂无 Intent。")

    with trace_tab:
        source_ids = sorted(store.list_runtime_trace_sources(character_id), reverse=True)
        if not source_ids:
            st.caption("暂无 Runtime Trace。")
        else:
            source_id = st.selectbox("Source Event ID", source_ids)
            trace = store.get_runtime_trace(int(source_id))
            if trace:
                decision, context, raw = st.tabs(["Decision", "Context", "Raw"])
                with decision:
                    action = trace.get("action", {})
                    st.write({"action": action.get("type"), "message": action.get("message"), "reason": action.get("reason"), "perception": trace.get("perception"), "reaction": trace.get("reaction"), "mental_state_before": trace.get("mental_state_before"), "mental_state_after": trace.get("mental_state_after"), "created_memory_ids": trace.get("created_memory_ids"), "created_intent_ids": trace.get("created_intent_ids")})
                    st.subheader("Recall")
                    st.json(trace.get("recalled_memories", []))
                with context:
                    for index, message in enumerate(trace.get("model_messages", []), start=1):
                        with st.expander(f"{index}. {message.get('role', '?')}", expanded=index <= 2):
                            st.code(message.get("content", ""), language="text")
                    st.subheader("Compiled Context")
                    st.code(trace.get("context", ""), language="text")
                with raw:
                    st.code(trace.get("raw_model_response", ""), language="json")
                    st.json(trace)

    store.close()


if __name__ == "__main__":
    main()
