from __future__ import annotations

import os

from character_memory.config import load_settings
from character_memory.storage.sqlite import SQLiteStore


def main():
    import streamlit as st

    config_path = os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml")
    settings = load_settings(config_path)
    st.set_page_config(page_title="Character Memory Inspector", layout="wide")
    st.title("Character Memory Inspector")
    character_id = st.sidebar.text_input("Character", "rin")
    st.sidebar.caption(f"DB: {settings.db_path}")

    store = SQLiteStore(settings.db_path)
    try:
        world_time = store.get_world_time(character_id)
        mental = store.get_mental_state(character_id)
        events = store.list_events(character_id, 100)
        memories = store.list_memories(character_id)[-100:]
        intents = [dict(r) for r in store.list_intents(character_id, 100)]

        c1, c2 = st.columns(2)
        c1.metric("World time", str(world_time or "not initialized"))
        c2.metric("Active memories", len(memories))
        st.subheader("Mental State")
        st.text(mental or "暂无")

        left, right = st.columns(2)
        with left:
            st.subheader("Timeline")
            st.dataframe(
                [
                    {
                        "time": e.event_time,
                        "type": e.event_type.value,
                        "content": e.content,
                        "metadata": e.metadata,
                    }
                    for e in reversed(events)
                ],
                use_container_width=True,
            )
        with right:
            st.subheader("Memories")
            st.dataframe(
                [
                    {
                        "time": m.event_time,
                        "type": m.memory_type,
                        "importance": m.importance,
                        "content": m.content,
                        "source_event_id": m.source_event_id,
                    }
                    for m in reversed(memories)
                ],
                use_container_width=True,
            )
        st.subheader("Intents")
        st.dataframe(intents, use_container_width=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
