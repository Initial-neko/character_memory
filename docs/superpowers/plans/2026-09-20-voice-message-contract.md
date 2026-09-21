# Voice Message Contract & Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a voice message a first-class, persistable chat message: audio bytes can be stored and read back, `VOICE_MESSAGE` is a legal visible action that keeps its text, and a message's voice status can transition from `pending` to `ready`/`failed` in SQLite and be re-delivered over SSE.

**Architecture:** Three layers. (1) `MediaStorage` gains WAV/MP3 support so audio bytes are stored and served alongside images through the existing `media_assets` table and `/v1/media/{id}` route. (2) `VOICE_MESSAGE` joins `ActionType` as a text-carrying visible action; its audio state lives in the existing free-form `metadata` JSON column, with the canonical key names shared from one module. (3) A new `update_event_metadata` store method supplies the UPDATE path that today does not exist anywhere in the codebase, and a small service drives the state transitions and re-delivers the event.

**Tech Stack:** Python 3.12, pydantic v2, SQLite via raw `sqlite3` (no ORM), FastAPI + Starlette (`TestClient` for HTTP tests), pytest 9.1.1.

## Global Constraints

Every task's requirements implicitly include this section. These are hard facts verified against the codebase — violating one produces a silent failure, not an error.

- **Synthesis itself is OUT OF SCOPE.** This plan never calls a TTS provider. `pending` messages are moved to `ready`/`failed` by explicitly calling the service. Do not write code that calls `/v1/tts` or any provider.
- **Audio formats are exactly WAV and MP3.** Verified against provider routing: `media_server.py:233` is `fallback_media_type = "audio/mpeg" if selected == "edge" else "audio/wav"`. GSV/Kokoro/Sherpa emit WAV; Edge emits MP3. Do NOT add OGG/M4A/WebM — no provider produces them.
- **All test commands run through uv:** `uv run pytest <path> -v`. Never invoke bare `python` or `pytest`. Reason: this worktree's session inherits `VIRTUAL_ENV=C:\Users\cute\projects\character_memory\.venv` (the MAIN repo), which uv corrects by its path check. A bare interpreter may silently use the wrong environment.
- **Every event row must be written through `append_event`.** `sqlite.py:416-423` is the only writer that populates `event_time_epoch`, and every read filters `WHERE event_time_epoch IS NOT NULL`. A row inserted any other way is invisible to all readers.
- **The expressive-action set exists in five places, not two.** Task 2 collapses four of them into one constant and narrows the fifth in place:
  - `runtime/person_runtime.py:20-28` and `application/group_conversation_service.py:17-25` — the two runtime gates, byte-identical to each other.
  - `application/proactive_service.py:11-19` (`_EXPRESSIVE`) and `eval/runner.py:10-18` (`_VISIBLE_ACTIONS`) — also byte-identical to those two.

  All four become `domain.models.EXPRESSIVE_ACTIONS`, imported rather than copied.
  - `life/ticker.py:30` — an inline 5-element subset that deliberately excludes `STICKER` and `IMAGE`. It does **not** join the shared set. It keeps its own narrower constant and gains `VOICE_MESSAGE` in place.

  Every one of the five silently misjudges a `VOICE_MESSAGE` until it is updated: the two runtime gates drop the message entirely, `proactive_service` records the intent as `SUPPRESSED` (a voice reply logged as giving up), `eval/runner` scores a voice-only turn as silence, and `life/ticker` records `SUPPRESSED`. **Before Task 2 lands, all five must change together.**
- **`message_actions` in `domain/models.py:105-111` gates whether `message` survives.** A type absent from that set falls through to the `else` at `models.py:152-157`, which sets `self.message = None`. `VOICE_MESSAGE` must be added there or its text is silently erased.
- **The metadata key names live in exactly one place:** `src/character_memory/voice_message_fields.py` (created in Task 4). Reading a literal string instead of importing the constant reintroduces the drift this module exists to prevent. **Four payload builders emit this same message shape, plus one writer:**
  - `history_web.py` `message_payload` — the direct history route `/v1/chat/history-page` (what the browser actually calls).
  - `group_web.py` `event_payload` — the group history route.
  - `api.py:301-317` `message_payload` — the legacy `/v1/chat/history`, **and** `character_summary` → `/v1/characters/summaries`, which is a live browser path (`web/unread.js:26` reads `latest_message`).
  - `application/chat_service.py:261-295` `ChatService.history()` — same shape; only test callers today, converged anyway so the invariant is total.
  - The writer is the state-transition service, which uses `voice_pending_fields()`, not the reader helper.

  This list was originally written as "three sites" and was wrong; a review caught the two missing builders. Any future site that emits this payload shape must import from the same module.
- **`metadata` is `dict[str, Any]` with NO schema validation** (`metadata_json TEXT NOT NULL DEFAULT '{}'`). Unknown keys persist, so a payload builder that forgets a key drops it on reload without any error.
- **No new config fields and no new schema migration.** `media_max_bytes` already exists (`config.py:60`, capped `le=32*1024*1024`) and `metadata_json` already exists. Adding either is out of scope; 8 MiB default covers ~2 minutes of 32 kHz mono WAV, far beyond a voice message.
- **Do not change `Content-Disposition` on `/v1/media/{id}`.** `api.py:572-580` passes `filename=`, which makes Starlette emit `attachment`. Making audio play inline instead of downloading is deliberately deferred (it affects existing image behavior and needs its own task). Do not "fix" it here.
- **Character identity is the persona id** (e.g. `"momo"`), carried in the `voice` field of TTS requests — never a directory path.
- **Commit messages are in English**, matching repo history.

---

### Task 1: MediaStorage stores and reads back WAV and MP3

**Files:**
- Modify: `src/character_memory/media.py`
- Test: `tests/test_media.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `MediaStorage.save_bytes(...)` and `save_data_url(...)` accept WAV and MP3 payloads and return a `MediaAsset` with `mime_type` in `{"audio/wav", "audio/mpeg"}` and `storage_name` ending `.wav` / `.mp3`; `MediaStorage.asset_path(asset)` returns a real `Path` for those assets (today it returns `None`, making stored audio unreadable).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_media.py`. Keep the existing imports and add `io` and `wave`:

```python
import base64
from datetime import datetime, timezone
import io
import wave

import pytest

from character_memory.media import MediaStorage
```

Then append these fixtures and tests:

```python
MP3_ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
MP3_FRAME_SYNC = b"\xff\xfb\x90\x00" + b"\x00" * 64


def wav_bytes() -> bytes:
    """A real RIFF/WAVE payload, so the sniff test cannot pass on a stub."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(32000)
        handle.writeframes(b"\x00\x00" * 320)
    return buf.getvalue()


def test_media_storage_stores_wav_and_reads_it_back(tmp_path):
    storage = MediaStorage(tmp_path / "media")
    payload = wav_bytes()

    asset = storage.save_bytes(
        character_id="momo",
        original_name="clip.wav",
        payload=payload,
        created_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        source="GENERATED_VOICE",
    )

    assert asset.mime_type == "audio/wav"
    assert asset.storage_name.endswith(".wav")
    assert asset.size_bytes == len(payload)
    # The allowlist in asset_path() is what makes stored audio readable at all.
    assert storage.asset_path(asset).read_bytes() == payload


@pytest.mark.parametrize(
    "payload",
    [MP3_ID3, MP3_FRAME_SYNC],
    ids=["id3-tag", "frame-sync"],
)
def test_media_storage_stores_both_mp3_shapes(tmp_path, payload):
    storage = MediaStorage(tmp_path / "media")

    asset = storage.save_bytes(
        character_id="momo",
        original_name="clip.mp3",
        payload=payload,
        created_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        source="GENERATED_VOICE",
    )

    assert asset.mime_type == "audio/mpeg"
    assert asset.storage_name.endswith(".mp3")
    assert storage.asset_path(asset).read_bytes() == payload


def test_media_storage_sniffs_claimed_audio_mime_off_a_wav(tmp_path):
    storage = MediaStorage(tmp_path / "media")

    asset, normalized = storage.save_data_url(
        character_id="momo",
        original_name="clip.mp3",
        data_url=data_url(data=wav_bytes(), mime="audio/mpeg"),
        created_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )

    assert asset.mime_type == "audio/wav"
    assert normalized.startswith("data:audio/wav;base64,")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_media.py -v`
Expected: the three new tests FAIL with `ValueError: unsupported image format; use JPEG, PNG, GIF or WebP`.

- [ ] **Step 3: Extend the MIME map**

In `src/character_memory/media.py`, replace `_MIME_TO_EXT` (currently `media.py:13-18`):

```python
_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
}
```

This single map is doing double duty: it is the mime→extension table for writes **and** the extension allowlist inside `asset_path` (`media.py:124`). Adding audio here is what makes `asset_path` accept audio; there is no second place to update.

- [ ] **Step 4: Teach the sniffer audio magic bytes**

Replace `_sniff_mime` (currently `media.py:44-54`) with:

```python
    @staticmethod
    def _sniff_mime(data: bytes) -> str | None:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        # RIFF carries both WebP and WAV; only bytes 8:12 separate them.
        if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
        if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
            return "audio/wav"
        # Edge TTS emits MP3 either with an ID3v2 tag or as a bare MPEG frame.
        if data.startswith(b"ID3"):
            return "audio/mpeg"
        if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
            return "audio/mpeg"
        return None
```

JPEG (`\xff\xd8\xff`, second byte `0xD8`) is matched before the frame-sync test and `0xD8 & 0xE0 == 0xC0`, so it cannot be misread as MP3.

- [ ] **Step 5: Generalize the image-only wording**

Audio now flows through `save_bytes`, so the strings that say "image" are wrong. In `src/character_memory/media.py`:

- `media.py:33-38` docstring: change `"""Small local image store for chat and generated visual assets.` to `"""Small local media store for chat and generated visual assets (images and voice clips).` and `Raw image bytes never go into SQLite or Runtime Trace.` to `Raw media bytes never go into SQLite or Runtime Trace.`
- `media.py:67`: `raise ValueError("image is empty")` → `raise ValueError("media is empty")`
- `media.py:69`: `raise ValueError(f"image exceeds {self.max_bytes // (1024 * 1024)} MiB limit")` → `raise ValueError(f"media exceeds {self.max_bytes // (1024 * 1024)} MiB limit")`
- `media.py:72`: `raise ValueError("unsupported image format; use JPEG, PNG, GIF or WebP")` → `raise ValueError("unsupported media format; use JPEG, PNG, GIF, WebP, WAV or MP3")`
- `media.py:100`: `raise ValueError("image must be a base64 data URL")` → `raise ValueError("media must be a base64 data URL")`
- `media.py:104`: `raise ValueError("image base64 is invalid")` → `raise ValueError("media base64 is invalid")`
- `media.py:77` and `media.py:25`: leave `"image"` as the `original_name` default. No caller relies on it (every call site passes `original_name`), and changing a model default is unrelated churn.

- [ ] **Step 6: Update the existing assertion that pins the old wording**

In `tests/test_media.py`, `test_media_storage_rejects_non_image_and_oversized_payload` (currently lines 34-50) has `match="unsupported image format"`. Change it to `match="unsupported media format"`. The second `pytest.raises` already matches `"exceeds"` and needs no change.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_media.py -v`
Expected: PASS (5 tests).

- [ ] **Step 8: Run the broader media suites for regressions**

Run: `uv run pytest tests/test_media.py tests/test_image_api.py tests/test_image_catalog_api.py -v`
Expected: PASS. `test_image_api.py` exercises `/v1/media/{id}` end-to-end and proves image behavior is unchanged.

- [ ] **Step 9: Commit**

```bash
git add src/character_memory/media.py tests/test_media.py
git commit -m "Store WAV and MP3 alongside images in MediaStorage"
```

---

### Task 2: VOICE_MESSAGE is a legal, text-carrying visible action

**Files:**
- Modify: `src/character_memory/domain/models.py`
- Modify: `src/character_memory/runtime/person_runtime.py`
- Modify: `src/character_memory/application/group_conversation_service.py`
- Test: `tests/test_voice_message_contract.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `ActionType.VOICE_MESSAGE` with value `"VOICE_MESSAGE"`.
  - `domain.models.EXPRESSIVE_ACTIONS: frozenset[ActionType]` — the one set every visible-action gate consults. `person_runtime`, `group_conversation_service`, `proactive_service` and `eval.runner` each expose it as the *same object*, imported rather than copied.
  - `life.ticker._INTENT_EXECUTED_ACTIONS: frozenset[ActionType]` — a deliberately narrower set (no `STICKER`, no `IMAGE`) that now includes `VOICE_MESSAGE`.
  - `ActionDecision` accepts `VOICE_MESSAGE` with a non-empty `message` and nulls the sticker/image fields.

- [ ] **Step 1: Write the failing test**

Create `tests/test_voice_message_contract.py`:

```python
import pytest

from character_memory.domain.models import ActionDecision, ActionType, EXPRESSIVE_ACTIONS


def test_voice_message_is_an_action_type():
    assert ActionType.VOICE_MESSAGE.value == "VOICE_MESSAGE"


def test_voice_message_keeps_its_text_and_drops_visual_fields():
    decision = ActionDecision(
        type="VOICE_MESSAGE",
        message="  晚上好呀  ",
        sticker_id="sticker-1",
        image_id="image-1",
    )

    assert decision.type == ActionType.VOICE_MESSAGE
    # Stripped, not erased: the text is what the bubble shows when expanded.
    assert decision.message == "晚上好呀"
    assert decision.sticker_id is None
    assert decision.image_id is None


def test_voice_message_requires_a_non_empty_message():
    with pytest.raises(ValueError, match="VOICE_MESSAGE requires a non-empty message"):
        ActionDecision(type="VOICE_MESSAGE", message="   ")


def test_voice_message_is_expressive():
    assert ActionType.VOICE_MESSAGE in EXPRESSIVE_ACTIONS


def test_every_action_gate_consults_the_shared_set():
    """Identity, not equality: two equal sets would drift apart on the next edit,
    and the drift is silent -- the action just stops producing a message. Four
    modules used to hold their own byte-identical copy of this set."""
    from character_memory.application import group_conversation_service, proactive_service
    from character_memory.eval import runner as eval_runner
    from character_memory.runtime import person_runtime

    for module in (person_runtime, group_conversation_service, proactive_service, eval_runner):
        assert module.EXPRESSIVE_ACTIONS is EXPRESSIVE_ACTIONS, module.__name__


def test_the_intent_ticker_counts_a_voice_reply_as_executed():
    """life/ticker.py keeps a narrower set on purpose -- it excludes STICKER and
    IMAGE, and this task must not widen that. But a voice reply does execute the
    intent, so VOICE_MESSAGE belongs there or the intent is logged as dropped."""
    from character_memory.life import ticker

    assert ActionType.VOICE_MESSAGE in ticker._INTENT_EXECUTED_ACTIONS
    assert ActionType.STICKER not in ticker._INTENT_EXECUTED_ACTIONS
    assert ActionType.IMAGE not in ticker._INTENT_EXECUTED_ACTIONS
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_voice_message_contract.py -v`
Expected: FAIL — `ImportError: cannot import name 'EXPRESSIVE_ACTIONS'`.

- [ ] **Step 3: Add the enum member**

In `src/character_memory/domain/models.py`, inside `ActionType` (currently `models.py:22-37`), add after `IMAGE = "IMAGE"` (line 34):

```python
    # A spoken chat message: the text is still the message body (shown when the
    # bubble is expanded), with generated audio attached via event metadata.
    VOICE_MESSAGE = "VOICE_MESSAGE"
```

- [ ] **Step 4: Define the shared expressive-action set**

In `src/character_memory/domain/models.py`, immediately after the `ActionType` class:

```python
# Actions that produce a visible outward message. Both chat modes consult this
# one set. It used to be duplicated verbatim in runtime/person_runtime.py and
# application/group_conversation_service.py, where editing one copy silently
# dropped the action in the other chat mode.
EXPRESSIVE_ACTIONS = frozenset({
    ActionType.REPLY,
    ActionType.MINIMAL_RESPONSE,
    ActionType.PROACTIVE_MESSAGE,
    ActionType.MESSAGE,
    ActionType.VOICE_MESSAGE,
    ActionType.EMOJI,
    ActionType.STICKER,
    ActionType.IMAGE,
})
```

- [ ] **Step 5: Add VOICE_MESSAGE to the message-actions gate**

In `src/character_memory/domain/models.py`, the `message_actions` set inside `validate_message_contract` (currently `models.py:105-111`). Add `ActionType.VOICE_MESSAGE`:

```python
        message_actions = {
            ActionType.REPLY,
            ActionType.MINIMAL_RESPONSE,
            ActionType.PROACTIVE_MESSAGE,
            ActionType.MESSAGE,
            ActionType.VOICE_MESSAGE,
            ActionType.EMOJI,
        }
```

Without this the model validator falls through to `models.py:152-157` and sets `self.message = None`, silently discarding the text.

This set deliberately stays a local literal rather than reusing `EXPRESSIVE_ACTIONS`. The two answer different questions: `message_actions` lists the types that must carry text, `EXPRESSIVE_ACTIONS` lists the types that may produce an event at all (`STICKER` and `IMAGE` are in one and not the other). Merging them would let a sticker into the text gate.

- [ ] **Step 6: Point the direct runtime at the shared set**

In `src/character_memory/runtime/person_runtime.py`, delete the local definition at `person_runtime.py:20-28` entirely, add `EXPRESSIVE_ACTIONS` to the existing `from character_memory.domain.models import ...` line, and update the single use site at `person_runtime.py:358`:

```python
                    if action.type not in EXPRESSIVE_ACTIONS:
                        continue
```

- [ ] **Step 7: Point the group runtime at the shared set**

Same in `src/character_memory/application/group_conversation_service.py`: delete the local definition at `group_conversation_service.py:17-25`, add `EXPRESSIVE_ACTIONS` to its `from character_memory.domain.models import ...` line, and update the single use site at `group_conversation_service.py:401`:

```python
                    if action.type not in EXPRESSIVE_ACTIONS:
                        continue
```

- [ ] **Step 8: Point the proactive service at the shared set**

In `src/character_memory/application/proactive_service.py`, delete `_EXPRESSIVE` at `proactive_service.py:11-19`, add `EXPRESSIVE_ACTIONS` to its `from character_memory.domain.models import ...` line (`proactive_service.py:6`), and update the one use site at `proactive_service.py:81`:

```python
                    if action_types & EXPRESSIVE_ACTIONS or legacy in EXPRESSIVE_ACTIONS:
```

Skip this and a voice reply to a proactive intent is written to the ledger as `SUPPRESSED` — the character appears to have given up on an intent it actually answered.

- [ ] **Step 9: Point the eval runner at the shared set**

In `src/character_memory/eval/runner.py`, delete `_VISIBLE_ACTIONS` at `runner.py:10-18`, add `EXPRESSIVE_ACTIONS` to its `from character_memory.domain.models import ...` line (`runner.py:7`), and update the one use site at `runner.py:59`:

```python
            visible_actions = [action for action in actions if action.type in EXPRESSIVE_ACTIONS]
```

Skip this and a voice-only turn is scored as the model choosing silence — a false negative that would make the eval suite punish the feature.

- [ ] **Step 10: Give the intent ticker its own narrower set, including VOICE_MESSAGE**

`src/character_memory/life/ticker.py:30` holds an inline 5-element literal inside an `if`. That set deliberately excludes `STICKER` and `IMAGE`, so it must NOT become `EXPRESSIVE_ACTIONS` — doing so would silently change how stickers and images are scored for intent execution, which is out of scope here. Lift it to a module-level constant with `VOICE_MESSAGE` added and nothing else changed.

Immediately after the imports near the top of `src/character_memory/life/ticker.py`:

```python
# Which actions count as having executed a proactive intent. Deliberately
# narrower than domain.models.EXPRESSIVE_ACTIONS: a sticker or an image alone
# does not discharge an intent. Kept local rather than derived so the two
# questions can move independently.
_INTENT_EXECUTED_ACTIONS = frozenset({
    ActionType.PROACTIVE_MESSAGE,
    ActionType.REPLY,
    ActionType.MINIMAL_RESPONSE,
    ActionType.MESSAGE,
    ActionType.EMOJI,
    ActionType.VOICE_MESSAGE,
})
```

Then replace the inline literal at `ticker.py:30`:

```python
            if action in _INTENT_EXECUTED_ACTIONS:
```

Skip this and a character that answers an intent with a voice message is recorded as having dropped it.

- [ ] **Step 11: Run the test to verify it passes**

Run: `uv run pytest tests/test_voice_message_contract.py -v`
Expected: PASS (6 tests).

- [ ] **Step 12: Run the contract-adjacent suites for regressions**

Run: `uv run pytest tests/test_p0_7_contract.py tests/test_runtime.py tests/test_p0_17a_runtime_core.py tests/test_p0_11_group_conversation.py tests/test_p0_9_proactive.py -v`
Expected: PASS. A `NameError: name '_EXPRESSIVE_ACTIONS' is not defined` (or `_EXPRESSIVE` / `_VISIBLE_ACTIONS`) here means a use site was missed in Steps 6-9.

Note the suite name for the proactive path may differ; if `tests/test_p0_9_proactive.py` does not exist, list `tests/` for the proactive test file and run that instead — the point is to exercise `proactive_service` and `life/ticker`.

- [ ] **Step 13: Commit**

```bash
git add src/character_memory/domain/models.py src/character_memory/runtime/person_runtime.py src/character_memory/application/group_conversation_service.py src/character_memory/application/proactive_service.py src/character_memory/eval/runner.py src/character_memory/life/ticker.py tests/test_voice_message_contract.py
git commit -m "Add VOICE_MESSAGE as a text-carrying visible action"
```

---

### Task 3: An update path for event metadata

**Files:**
- Modify: `src/character_memory/storage/sqlite.py`
- Modify: `src/character_memory/group_store.py`
- Test: `tests/test_voice_message_persistence.py` (create)

**Interfaces:**
- Consumes: nothing from Tasks 1-2.
- Produces:
  - `SQLiteStore.get_event(event_id: int) -> Event | None` — returns `None` for a missing row or one whose `event_time_epoch` is NULL.
  - `SQLiteStore.update_event_metadata(event_id: int, metadata: dict[str, Any]) -> bool` — replaces the whole metadata document, returns `True` when a row matched.
  - `GroupRepository.update_event_metadata(event_id: int, metadata: dict[str, Any]) -> bool` — same, over `conversation_events`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_voice_message_persistence.py`:

```python
from datetime import datetime, timezone

from character_memory.domain.models import Event, EventType
from character_memory.storage.sqlite import SQLiteStore


def _store(tmp_path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "voice.db")


def _voice_event(character_id="momo") -> Event:
    return Event(
        character_id=character_id,
        event_type=EventType.CHARACTER_MESSAGE,
        event_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
        content="晚上好呀",
        metadata={"action": "VOICE_MESSAGE", "action_index": 0, "voice_status": "pending"},
    )


def test_get_event_round_trips_a_persisted_event(tmp_path):
    store = _store(tmp_path)
    saved = store.append_event(_voice_event())

    reloaded = store.get_event(saved.id)

    assert reloaded.id == saved.id
    assert reloaded.content == "晚上好呀"
    assert reloaded.metadata["voice_status"] == "pending"


def test_get_event_returns_none_for_a_missing_row(tmp_path):
    store = _store(tmp_path)

    assert store.get_event(999999) is None


def test_update_event_metadata_replaces_the_document(tmp_path):
    store = _store(tmp_path)
    saved = store.append_event(_voice_event())

    updated = store.update_event_metadata(
        saved.id,
        {
            "action": "VOICE_MESSAGE",
            "action_index": 0,
            "voice_status": "ready",
            "voice_media_id": "abc123",
            "voice_duration_ms": 1840,
        },
    )

    assert updated is True
    reloaded = store.get_event(saved.id)
    assert reloaded.metadata["voice_status"] == "ready"
    assert reloaded.metadata["voice_media_id"] == "abc123"
    assert reloaded.metadata["voice_duration_ms"] == 1840


def test_update_event_metadata_reports_a_missing_row(tmp_path):
    store = _store(tmp_path)

    assert store.update_event_metadata(999999, {"voice_status": "ready"}) is False


def test_update_event_metadata_leaves_the_row_readable(tmp_path):
    """event_time_epoch is filtered on every read; an UPDATE must not clear it."""
    store = _store(tmp_path)
    saved = store.append_event(_voice_event())

    store.update_event_metadata(saved.id, {"action": "VOICE_MESSAGE", "voice_status": "failed"})

    row = store.conn.execute("SELECT event_time_epoch FROM events WHERE id=?", (saved.id,)).fetchone()
    assert row["event_time_epoch"] is not None
    assert store.get_event(saved.id) is not None


def test_group_update_event_metadata_replaces_the_document(tmp_path):
    """GroupRepository reaches through self.store rather than owning a
    connection of its own, so this is the one place that proxy access could
    silently be written wrong. It also has no caller yet, which is exactly why
    it needs a real test rather than a one-off script."""
    from character_memory.group_store import GroupEvent, GroupRepository

    store = _store(tmp_path)
    repo = GroupRepository(store)
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    group = repo.create_group("测试群", ["momo", "rin"], now)
    saved = repo.append_event(
        GroupEvent(
            conversation_id=group.id,
            turn_id="turn-1",
            actor_type="CHARACTER",
            actor_id="momo",
            event_type="CHARACTER_MESSAGE",
            event_time=now,
            content="晚上好呀",
            metadata={"action": "VOICE_MESSAGE", "voice_status": "pending"},
        )
    )

    assert repo.update_event_metadata(saved.id, {"action": "VOICE_MESSAGE", "voice_status": "ready"}) is True
    assert repo.update_event_metadata(999999, {"voice_status": "ready"}) is False

    # list_events filters on event_time_epoch IS NOT NULL, so reading the row
    # back proves the UPDATE did not clear that column.
    events = repo.list_events(group.id)
    assert events[-1].metadata["voice_status"] == "ready"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_voice_message_persistence.py -v`
Expected: FAIL with `AttributeError: 'SQLiteStore' object has no attribute 'get_event'`.

- [ ] **Step 3: Implement both store methods**

In `src/character_memory/storage/sqlite.py`, add next to `append_event` (currently `sqlite.py:416-423`). Match the surrounding style: the existing `get_media_asset` (`sqlite.py:431-439`) is the model for the NULL-epoch guard and its warning log.

`sqlite.py` imports no typing names today — its stdlib block is `contextlib`, `json`, `logging`, `sqlite3`, `struct`, `threading`, `time`, `datetime`, `pathlib`. Add `from typing import Any` beside those for the `dict[str, Any]` annotation below. (`group_store.py` already has it, so no change is needed there.) `json` and the module-level `logger` are both already present in `sqlite.py`.

```python
    def get_event(self, event_id: int) -> Event | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                return None
            if row["event_time_epoch"] is None:
                logger.warning("storage.event skipped_invalid_time event_id=%s value=%r", event_id, row["event_time"])
                return None
            return self._event_from_row(row)

    def update_event_metadata(self, event_id: int, metadata: dict[str, Any]) -> bool:
        """Replace an event's metadata document.

        The whole document is replaced, not merged: callers read the current
        metadata, merge their keys, and pass the result. event_time_epoch is
        deliberately untouched, because every read filters on it being non-NULL.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE events SET metadata_json=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), event_id),
            )
            self._maybe_commit()
            return cur.rowcount > 0
```

- [ ] **Step 4: Implement the group-store equivalent**

Read `src/character_memory/group_store.py` first to confirm this, then add to `GroupRepository` (it owns the `conversation_events` table). **`GroupRepository` holds no connection of its own** — `__init__` only stores the store (`group_store.py:53-55`) and every method reaches through as `self.store.conn`, `self.store._lock`, `self.store._maybe_commit()` (see `append_event`, `group_store.py:285-307`). Writing `self.conn` here is an `AttributeError`:

```python
    def update_event_metadata(self, event_id: int, metadata: dict[str, Any]) -> bool:
        """Replace a conversation event's metadata document; see SQLiteStore."""
        with self.store._lock:
            cur = self.store.conn.execute(
                "UPDATE conversation_events SET metadata_json=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), event_id),
            )
            self.store._maybe_commit()
            return cur.rowcount > 0
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_voice_message_persistence.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Run the storage suites for regressions**

Run: `uv run pytest tests/test_p0_14_migrations.py tests/test_chat_service.py tests/test_p0_11_group_conversation.py -v`
Expected: PASS. `test_p0_14_migrations.py` asserts the migration ledger, proving no migration was added.

- [ ] **Step 7: Commit**

```bash
git add src/character_memory/storage/sqlite.py src/character_memory/group_store.py tests/test_voice_message_persistence.py
git commit -m "Add event read and metadata update paths to both stores"
```

---

### Task 4: A written voice message survives a history reload

**Files:**
- Create: `src/character_memory/voice_message_fields.py`
- Modify: `src/character_memory/history_web.py`
- Modify: `src/character_memory/group_web.py`
- Modify: `src/character_memory/api.py` (the legacy `message_payload` — see the reader-count note below)
- Modify: `src/character_memory/application/chat_service.py` (`ChatService.history()` — same payload shape, test-only callers today)
- Modify: `src/character_memory/runtime/person_runtime.py`
- Modify: `src/character_memory/application/group_conversation_service.py`
- Test: `tests/test_voice_message_persistence.py` (extend)

**Interfaces:**
- Consumes: `ActionType.VOICE_MESSAGE` (Task 2); `get_event` (Task 3).
- Produces:
  - `voice_message_fields.voice_pending_fields() -> dict[str, Any]` — the four metadata keys written at persist time.
  - `voice_message_fields.voice_fields(metadata: dict) -> dict[str, Any]` — the same four keys read out for a payload, `None` for non-voice messages.
  - Constants `VOICE_STATUS`, `VOICE_MEDIA_ID`, `VOICE_DURATION_MS`, `VOICE_ERROR`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_voice_message_persistence.py`. Add these imports at the top of the file:

```python
from pathlib import Path

from character_memory.voice_message_fields import (
    VOICE_DURATION_MS,
    VOICE_ERROR,
    VOICE_MEDIA_ID,
    VOICE_STATUS,
    voice_fields,
    voice_pending_fields,
)
```

Then:

```python
ROOT = Path(__file__).resolve().parents[1]


def test_voice_pending_fields_are_the_four_canonical_keys():
    assert voice_pending_fields() == {
        VOICE_STATUS: "pending",
        VOICE_MEDIA_ID: None,
        VOICE_DURATION_MS: None,
        VOICE_ERROR: None,
    }


def test_voice_fields_reads_a_ready_message():
    assert voice_fields(
        {
            "action": "VOICE_MESSAGE",
            VOICE_STATUS: "ready",
            VOICE_MEDIA_ID: "abc123",
            VOICE_DURATION_MS: 1840,
        }
    ) == {
        VOICE_STATUS: "ready",
        VOICE_MEDIA_ID: "abc123",
        VOICE_DURATION_MS: 1840,
        VOICE_ERROR: None,
    }


def test_voice_fields_defaults_to_none_for_a_plain_text_message():
    assert voice_fields({"action": "MESSAGE"}) == {
        VOICE_STATUS: None,
        VOICE_MEDIA_ID: None,
        VOICE_DURATION_MS: None,
        VOICE_ERROR: None,
    }


def test_both_history_payloads_read_the_shared_keys():
    """Two payload builders read these keys. A literal string in either one is
    the drift this module exists to prevent, so assert they call the shared
    reader rather than trusting review to catch it."""
    for relative in (
        "src/character_memory/history_web.py",
        "src/character_memory/group_web.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "voice_fields(" in source, relative


def test_both_runtimes_write_the_pending_state_from_the_shared_helper():
    """Same reasoning for the write side: the two action loops are verbatim
    copies, and a hardcoded key in one of them drifts on the next rename."""
    for relative in (
        "src/character_memory/runtime/person_runtime.py",
        "src/character_memory/application/group_conversation_service.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "voice_pending_fields(" in source, relative
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_voice_message_persistence.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'character_memory.voice_message_fields'`.

- [ ] **Step 3: Create the shared key module**

Create `src/character_memory/voice_message_fields.py`:

```python
"""Canonical metadata keys for a persisted VOICE_MESSAGE.

Every payload builder that emits a message and the service that writes a voice
message share this one definition, so a rename cannot drift between them -- a
drifted key fails silently, dropping the audio reference on reload instead of
raising.
"""

from __future__ import annotations

from typing import Any

VOICE_STATUS = "voice_status"
VOICE_MEDIA_ID = "voice_media_id"
VOICE_DURATION_MS = "voice_duration_ms"
VOICE_ERROR = "voice_error"


def voice_pending_fields() -> dict[str, Any]:
    """The metadata written when a voice message is first persisted.

    The message is written before any synthesis is attempted, so a synthesis
    failure still leaves the text in the transcript.
    """
    return {
        VOICE_STATUS: "pending",
        VOICE_MEDIA_ID: None,
        VOICE_DURATION_MS: None,
        VOICE_ERROR: None,
    }


def voice_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    """The four keys exposed on a history payload; None for non-voice messages."""
    return {
        VOICE_STATUS: metadata.get(VOICE_STATUS),
        VOICE_MEDIA_ID: metadata.get(VOICE_MEDIA_ID),
        VOICE_DURATION_MS: metadata.get(VOICE_DURATION_MS),
        VOICE_ERROR: metadata.get(VOICE_ERROR),
    }
```

- [ ] **Step 4: Run the module tests**

Run: `uv run pytest tests/test_voice_message_persistence.py -v -k "voice_pending_fields or voice_fields"` 
Expected: 3 pass, 2 fail. The two source-contract tests fail — neither a payload builder nor an action loop references the new helper yet.

- [ ] **Step 5: Spread the keys into the direct history payload**

In `src/character_memory/history_web.py`, add the import at the top:

```python
from character_memory.voice_message_fields import voice_fields
```

Then in the payload dict returned by `message_payload` (currently `history_web.py:91-108`), add one spread line just before `"source_event_type"`:

```python
            "media_id": media_id,
            # Deliberately a distinct key from "media_id" above, which drives
            # image rendering.
            **voice_fields(event.metadata),
            "source_event_type": source_event_type,
```

The first line shown is the **existing** `"media_id": media_id,` line — keep it as it is. Only the two comment lines and the spread are new; the spread adds `voice_status`, `voice_media_id`, `voice_duration_ms`, `voice_error`.

- [ ] **Step 6: Spread the same keys into the group history payload**

In `src/character_memory/group_web.py`, add the import at the top (this module already imports from `character_memory.images`, so a `character_memory`-level import matches the file's style):

```python
from character_memory.voice_message_fields import voice_fields
```

Then in the dict returned by `event_payload` (currently `group_web.py:183-202`), add the same one-line spread just before `"source_conversation_event_id"`:

```python
            "image": image,
            **voice_fields(event.metadata),
            "source_conversation_event_id": event.metadata.get("source_conversation_event_id"),
```

- [ ] **Step 7: Run the payload tests**

Run: `uv run pytest tests/test_voice_message_persistence.py -v`
Expected: 9 pass, 1 fail. Only `test_both_runtimes_write_the_pending_state_from_the_shared_helper` still fails; Steps 8-9 have not run yet.

- [ ] **Step 8: Make the direct runtime write the pending state**

In `src/character_memory/runtime/person_runtime.py`, the action loop at `person_runtime.py:357-391` handles `STICKER` and `IMAGE` in dedicated branches and everything else in the `else`. Add the import at the top:

```python
from character_memory.voice_message_fields import voice_pending_fields
```

Then add a `VOICE_MESSAGE` branch before the `else`:

```python
        elif action.type == ActionType.VOICE_MESSAGE:
            if not (action.message or "").strip():
                continue
            content = (action.message or "").strip()
            metadata.update(voice_pending_fields())
```

The `metadata` dict it updates is the one built at `person_runtime.py:359-365`; it already carries `action`, `action_index`, `source_event_id`, `source_event_type` and `conversation_id`, and is passed to `append_event` below.

- [ ] **Step 9: Make the group runtime write it too**

In `src/character_memory/application/group_conversation_service.py`, add the same import at the top, then add the identical branch to the loop at `group_conversation_service.py:400-440`, before its `else`:

```python
        elif action.type == ActionType.VOICE_MESSAGE:
            if not (action.message or "").strip():
                continue
            content = (action.message or "").strip()
            metadata.update(voice_pending_fields())
```

- [ ] **Step 10: Write the end-to-end persistence test**

Append to `tests/test_voice_message_persistence.py`:

```python
def test_a_persisted_voice_message_reloads_with_its_audio_reference(tmp_path):
    """The whole point of this task: write pending, flip to ready, and confirm a
    history read still carries the asset reference. Without the payload spread in
    Steps 5-6 these keys vanish on reload and the bubble loses its audio."""
    store = _store(tmp_path)
    saved = store.append_event(
        Event(
            character_id="momo",
            event_type=EventType.CHARACTER_MESSAGE,
            event_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
            content="晚上好呀",
            metadata={"action": "VOICE_MESSAGE", "action_index": 0, **voice_pending_fields()},
        )
    )

    assert store.get_event(saved.id).metadata[VOICE_STATUS] == "pending"

    metadata = store.get_event(saved.id).metadata
    metadata.update(
        {
            VOICE_STATUS: "ready",
            VOICE_MEDIA_ID: "abc123",
            VOICE_DURATION_MS: 1840,
        }
    )
    store.update_event_metadata(saved.id, metadata)

    payload = voice_fields(store.get_event(saved.id).metadata)
    assert payload[VOICE_STATUS] == "ready"
    assert payload[VOICE_MEDIA_ID] == "abc123"
    assert payload[VOICE_DURATION_MS] == 1840
    assert payload[VOICE_ERROR] is None
```

- [ ] **Step 11: Run the full voice-message set**

Run: `uv run pytest tests/test_voice_message_persistence.py tests/test_voice_message_contract.py -v`
Expected: PASS (16 tests — 11 persistence, 5 contract).

- [ ] **Step 12: Run the history suites for regressions**

Run: `uv run pytest tests/test_chat_service.py tests/test_p0_11_group_conversation.py tests/test_api_smoke.py -v`
Expected: PASS. These exercise the two payload builders through HTTP.

- [ ] **Step 13: Commit**

```bash
git add src/character_memory/voice_message_fields.py src/character_memory/history_web.py src/character_memory/group_web.py src/character_memory/runtime/person_runtime.py src/character_memory/application/group_conversation_service.py tests/test_voice_message_persistence.py
git commit -m "Persist and reload voice message state in both chat modes"
```

---

### Task 5: Move a voice message between states and re-deliver it

**Files:**
- Create: `src/character_memory/application/voice_message_service.py`
- Test: `tests/test_voice_message_service.py` (create)

**Interfaces:**
- Consumes: `get_event` and `update_event_metadata` (Task 3); the key constants (Task 4).
- Produces:
  - `mark_voice_message_ready(store, hub, event_id, *, character_id, conversation_id, media_id, duration_ms) -> bool`
  - `mark_voice_message_failed(store, hub, event_id, *, character_id, conversation_id, error) -> bool`
  - Both return `False` when the event id does not exist, and both re-publish `character_event` with the **same event id**, which is what makes the browser's id-keyed merge update the bubble in place.

- [ ] **Step 1: Write the failing test**

Create `tests/test_voice_message_service.py`:

```python
from datetime import datetime, timezone

from character_memory.application.voice_message_service import (
    mark_voice_message_failed,
    mark_voice_message_ready,
)
from character_memory.domain.models import Event, EventType
from character_memory.storage.sqlite import SQLiteStore
from character_memory.voice_message_fields import (
    VOICE_DURATION_MS,
    VOICE_ERROR,
    VOICE_MEDIA_ID,
    VOICE_STATUS,
    voice_pending_fields,
)


class _FakeHub:
    def __init__(self):
        self.published = []

    def publish(self, channel_key, event_type, data):
        self.published.append((channel_key, event_type, data))
        return len(self.published)


def _pending(tmp_path):
    store = SQLiteStore(tmp_path / "voice.db")
    saved = store.append_event(
        Event(
            character_id="momo",
            event_type=EventType.CHARACTER_MESSAGE,
            event_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
            content="晚上好呀",
            metadata={"action": "VOICE_MESSAGE", **voice_pending_fields()},
        )
    )
    return store, saved


def test_mark_ready_sets_the_asset_and_republishes_the_same_id(tmp_path):
    store, saved = _pending(tmp_path)
    hub = _FakeHub()

    assert (
        mark_voice_message_ready(
            store,
            hub,
            saved.id,
            character_id="momo",
            conversation_id="momo:default",
            media_id="abc123",
            duration_ms=1840,
        )
        is True
    )

    metadata = store.get_event(saved.id).metadata
    assert metadata[VOICE_STATUS] == "ready"
    assert metadata[VOICE_MEDIA_ID] == "abc123"
    assert metadata[VOICE_DURATION_MS] == 1840

    channel_key, event_type, data = hub.published[0]
    assert event_type == "character_event"
    # The same id is what makes the client merge update in place.
    assert data["id"] == saved.id
    assert data["metadata"][VOICE_STATUS] == "ready"
    assert channel_key.endswith("momo:default")


def test_mark_failed_records_the_reason_and_keeps_the_text(tmp_path):
    store, saved = _pending(tmp_path)
    hub = _FakeHub()

    assert (
        mark_voice_message_failed(
            store,
            hub,
            saved.id,
            character_id="momo",
            conversation_id="momo:default",
            error="provider unavailable",
        )
        is True
    )

    reloaded = store.get_event(saved.id)
    assert reloaded.content == "晚上好呀"
    assert reloaded.metadata[VOICE_STATUS] == "failed"
    assert reloaded.metadata[VOICE_ERROR] == "provider unavailable"
    assert reloaded.metadata[VOICE_MEDIA_ID] is None


def test_unknown_event_id_is_reported_not_raised(tmp_path):
    store, _ = _pending(tmp_path)
    hub = _FakeHub()

    assert (
        mark_voice_message_ready(
            store,
            hub,
            999999,
            character_id="momo",
            conversation_id="momo:default",
            media_id="abc123",
            duration_ms=None,
        )
        is False
    )
    assert hub.published == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_voice_message_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'character_memory.application.voice_message_service'`.

- [ ] **Step 3: Implement the service**

Create `src/character_memory/application/voice_message_service.py`:

```python
"""State transitions for a persisted VOICE_MESSAGE.

Synthesis is not performed here. The caller synthesizes, stores the audio as a
media asset, then reports the outcome through these two functions. Each one
updates the event's metadata and re-publishes the event under its ORIGINAL id,
which is what lets the browser merge the update into the existing bubble.
"""

from __future__ import annotations

import logging

from character_memory.application.async_conversation import direct_channel
from character_memory.voice_message_fields import (
    VOICE_DURATION_MS,
    VOICE_ERROR,
    VOICE_MEDIA_ID,
    VOICE_STATUS,
)

logger = logging.getLogger("character_memory.application.voice_message")


def _publish(hub, event, character_id: str, conversation_id: str) -> None:
    if hub is None:
        return
    try:
        hub.publish(
            direct_channel(character_id, conversation_id),
            "character_event",
            {
                "id": event.id,
                "character_id": event.character_id,
                "event_type": event.event_type.value,
                "event_time": event.event_time.isoformat(),
                "content": event.content,
                "metadata": event.metadata,
            },
        )
    except Exception:
        logger.exception("voice_message.publish_failed event_id=%s", event.id)


def _apply(store, hub, event_id: int, *, character_id: str, conversation_id: str, patch: dict) -> bool:
    event = store.get_event(event_id)
    if event is None:
        logger.warning("voice_message.missing_event event_id=%s", event_id)
        return False

    metadata = dict(event.metadata)
    metadata.update(patch)
    if not store.update_event_metadata(event_id, metadata):
        return False

    _publish(hub, event.model_copy(update={"metadata": metadata}), character_id, conversation_id)
    return True


def mark_voice_message_ready(
    store,
    hub,
    event_id: int,
    *,
    character_id: str,
    conversation_id: str,
    media_id: str,
    duration_ms: int | None,
) -> bool:
    return _apply(
        store,
        hub,
        event_id,
        character_id=character_id,
        conversation_id=conversation_id,
        patch={
            VOICE_STATUS: "ready",
            VOICE_MEDIA_ID: media_id,
            VOICE_DURATION_MS: duration_ms,
            VOICE_ERROR: None,
        },
    )


def mark_voice_message_failed(
    store,
    hub,
    event_id: int,
    *,
    character_id: str,
    conversation_id: str,
    error: str,
) -> bool:
    return _apply(
        store,
        hub,
        event_id,
        character_id=character_id,
        conversation_id=conversation_id,
        patch={
            VOICE_STATUS: "failed",
            VOICE_MEDIA_ID: None,
            VOICE_DURATION_MS: None,
            VOICE_ERROR: str(error)[:400],
        },
    )
```

- [ ] **Step 4: Verify the channel key format the test asserts**

Run: `uv run pytest tests/test_voice_message_service.py::test_mark_ready_sets_the_asset_and_republishes_the_same_id -v`

The test only asserts `channel_key.endswith("momo:default")`, which is deliberately loose. If it still fails, read `direct_channel` in `src/character_memory/application/async_conversation.py` (channel keys are documented at `async_conversation.py:21-26`) and correct the test's expectation — the production call is correct, the expectation is the guess.

- [ ] **Step 5: Run the service tests**

Run: `uv run pytest tests/test_voice_message_service.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the whole voice-message set**

Run: `uv run pytest tests/test_voice_message_service.py tests/test_voice_message_persistence.py tests/test_voice_message_contract.py tests/test_media.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite to confirm a clean baseline is preserved**

Run: `uv run pytest -q`
Expected: PASS, `0 failed`. The pre-existing baseline at this commit is 471 passed, 5 skipped; this plan adds tests and must not break any existing one.

- [ ] **Step 8: Commit**

```bash
git add src/character_memory/application/voice_message_service.py tests/test_voice_message_service.py
git commit -m "Add voice message state transitions with in-place re-delivery"
```

---

## Deliberately out of scope

These are real requirements from the wider voice-message feature that this plan does not implement. They are listed so the final review does not treat them as gaps, and so the next plan starts from a known edge.

1. **Synthesis.** Nothing here calls a TTS provider. `/v1/tts` is bytes-only and persists nothing (`media_server.py:180-191`), so the next plan must call it, capture `X-Media-Audio-Ms` for the duration, store the bytes via `MediaStorage`, then call `mark_voice_message_ready`. That work is blocked on the GSV template chain landing: `gsv_tts_experiment.py:185` still resolves voices through `discover_voice_profiles`, not the template registry.
2. **GSV silently degrades an unknown voice** to `default_voice` instead of failing (`gsv_tts_experiment.py:210-229`), while kokoro/edge raise and return 400. The synthesis plan must compare the returned `X-Media-Voice` against the requested character id, or a wrong-voice clip will be stored and marked `ready`.
3. **Browser playback.** `/v1/media/{id}` sends `Content-Disposition: attachment` (via `filename=` at `api.py:572-580`) and supports no HTTP `Range`, so a browser will download the clip rather than play it and cannot seek. Fixing this touches image behavior and belongs in the player task.
4. **Group-chat SSE re-delivery.** Task 5 re-publishes on the direct channel only. The group equivalent (`group_character_event` on `group:{conversation_id}`) is not implemented; group persistence and history reload are (Task 4).
5. **Recovery of orphaned `pending` messages.** A process that dies mid-synthesis leaves a message stuck at `pending` forever. A startup sweep that flips stale `pending` rows to `failed` is not implemented here.
6. **Frontend.** No player, no `voice.js` changes, no expand-text UI, no single-playback-at-a-time rule, no pause-on-record.
