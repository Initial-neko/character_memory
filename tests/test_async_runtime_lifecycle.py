from __future__ import annotations

from types import SimpleNamespace

from character_memory.application.async_conversation import (
    ConversationEventHub,
    ReactionScheduler,
    direct_channel,
)


def test_hub_reclaims_idle_channel_but_never_an_active_subscriber():
    hub = ConversationEventHub(idle_seconds=1)
    key = direct_channel("rin", "old-conversation")

    with hub.channel_lease(key) as channel:
        channel.last_activity = 10.0
        assert hub.prune_idle(now=100.0) == 0
        assert hub._channel(key) is channel

    channel.last_activity = 10.0
    assert hub.prune_idle(now=100.0) == 1
    assert hub._channel(key) is not channel
    hub.close()


def test_scheduler_reclaims_only_fully_processed_idle_state():
    hub = ConversationEventHub()
    scheduler = ReactionScheduler(
        lambda: None,
        lambda: [],
        hub,
        idle_seconds=1,
    )
    key = direct_channel("rin", "finished")
    state = scheduler._state(key)
    state.latest_event = SimpleNamespace(id=7)
    state.processed_id = 7
    state.active = False
    state.last_activity = 10.0

    assert scheduler.prune_idle(now=100.0) == 1
    assert scheduler.status_snapshot(key) == {"state": "idle", "watermark": 0}

    busy = scheduler._state(direct_channel("rin", "busy"))
    busy.latest_event = SimpleNamespace(id=8)
    busy.processed_id = 7
    busy.active = True
    busy.last_activity = 10.0
    assert scheduler.prune_idle(now=100.0) == 0

    scheduler.close()
    hub.close()


