from character_memory.domain.models import EventType


def generation_event_denial(event, *, autonomous_enabled=False):
    """One event boundary shared by advertisement, sanitization and execution."""
    if event is None:
        return "MISSING_EVENT"
    metadata = event.metadata
    group = metadata.get("source_conversation_event_id") is not None or metadata.get("group_event_id") is not None
    channel = str(metadata.get("channel") or "").upper()
    if event.event_type == EventType.USER_MESSAGE:
        # Existing user-triggered Direct and Group image expression stays supported.
        return "" if channel in {"", "DIRECT", "GROUP"} else "CHANNEL_DENIED"
    if event.event_type not in {EventType.TIME_TICK, EventType.PROACTIVE_INTENT} or channel != "DIRECT" or group:
        return "CHANNEL_OR_EVENT_DENIED"
    return "" if autonomous_enabled else "AUTONOMOUS_DISABLED"
