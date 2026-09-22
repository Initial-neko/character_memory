from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.application.clock import FixedClock
from character_memory.application.group_conversation_service import GroupConversationService
from character_memory.domain.models import (
    ActionDecision,
    ActionType,
    DailyLifePlan,
    DiaryResult,
    IntentCandidate,
    MemoryCandidate,
    PersonReaction,
)
from character_memory.group_web import attach_group_routes
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class SequenceModel(PersonModel):
    def __init__(self, reactions):
        self.reactions = list(reactions)
        self.contexts = []
        self.sessions = []
        self.last_model = "fake-group-model"

    def react(self, context: str) -> PersonReaction:
        return self.react_for_session(context, "default")

    def react_for_session(self, context: str, session_id: str) -> PersonReaction:
        self.contexts.append(context)
        self.sessions.append(session_id)
        return self.reactions.pop(0)

    def plan_day(self, context: str) -> DailyLifePlan:
        return DailyLifePlan()

    def write_diary(self, context: str) -> DiaryResult:
        return DiaryResult(diary="", mental_state_update="")


def runtime_for(store, embeddings, model, persona):
    return PersonRuntime(
        store,
        VectorRecall(store, embeddings, limit=8),
        embeddings,
        model,
        persona,
    )


def test_group_turn_has_one_shared_fact_and_independent_character_reactions(tmp_path: Path):
    store = SQLiteStore(tmp_path / "group.db")
    now = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
    clock = FixedClock(now)
    embeddings = DeterministicEmbedding()
    model = SequenceModel(
        [
            PersonReaction(
                perception="用户在群里说自己修好了问题。",
                reaction="想简短确认一下。",
                actions=[ActionDecision(type=ActionType.MESSAGE, message="总算修好了。")],
                memory_candidates=[MemoryCandidate(content="用户在群里说自己终于修好了那个问题。", memory_type="SHARED", importance=0.7)],
                intent_candidates=[IntentCandidate(content="晚点再问一次")],
            ),
            PersonReaction(
                perception="Rin 已经回应了用户。",
                reaction="没有必要重复。",
                actions=[],
            ),
        ]
    )
    runtimes = {
        "rin": runtime_for(store, embeddings, model, "你是 Rin。"),
        "momo": runtime_for(store, embeddings, model, "你是 Momo。"),
    }
    service = GroupConversationService(
        store,
        runtimes,
        clock,
        profiles=[{"id": "rin", "name": "Rin"}, {"id": "momo", "name": "Momo"}],
    )
    group = service.create_group("测试群", ["rin", "momo"])
    result = service.send(group.id, "终于修好了")

    events = service.repo.list_events(group.id)
    assert [(item.actor_type, item.actor_id) for item in events] == [("USER", "user"), ("CHARACTER", "rin")]
    assert events[0].content == "终于修好了"
    assert events[1].content == "总算修好了。"
    assert result["speaker_order"] == ["rin", "momo"]
    assert store.list_chat_events("rin") == []
    assert store.list_chat_events("momo") == []
    assert "Rin: 总算修好了。" in model.contexts[1]
    assert model.sessions == [f"group:{group.id}:rin", f"group:{group.id}:momo"]

    rin_memories = store.list_memories("rin", include_embedding=False)
    assert len(rin_memories) == 1
    assert rin_memories[0].metadata["origin"] == "GROUP"
    assert rin_memories[0].metadata["conversation_id"] == group.id
    assert rin_memories[0].metadata["source_conversation_event_id"] == events[0].id
    assert store.list_intents("rin") == []

    traces = service.repo.list_turn_traces(group.id, result["turn_id"])
    assert [item["character_id"] for item in traces] == ["rin", "momo"]
    assert traces[1]["actions"] == []
    store.close()


def test_group_user_sticker_is_one_shared_semantic_fact_not_direct_chat(tmp_path: Path):
    store = SQLiteStore(tmp_path / "sticker-group.db")
    now = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
    embeddings = DeterministicEmbedding()
    model = SequenceModel([PersonReaction(actions=[]), PersonReaction(actions=[])])
    runtimes = {
        "rin": runtime_for(store, embeddings, model, "Rin"),
        "momo": runtime_for(store, embeddings, model, "Momo"),
    }
    service = GroupConversationService(store, runtimes, FixedClock(now), profiles=[{"id":"rin","name":"Rin"},{"id":"momo","name":"Momo"}])
    group = service.create_group("表情群", ["rin", "momo"])

    service.send(
        group.id,
        "",
        sticker={"id":"happy_01","label":"开心","tags":["开心","好耶"],"description":"庆祝时使用"},
    )

    events = service.repo.list_events(group.id)
    assert len(events) == 1
    assert events[0].actor_type == "USER"
    assert events[0].metadata["action"] == "STICKER"
    assert events[0].metadata["sticker_id"] == "happy_01"
    assert "开心" in events[0].content
    assert "好耶" in events[0].content
    assert store.list_chat_events("rin") == []
    assert store.list_chat_events("momo") == []
    assert all("用户发送表情包" in context for context in model.contexts)
    store.close()


def test_group_first_speaker_rotates_between_user_turns(tmp_path: Path):
    store = SQLiteStore(tmp_path / "rotate.db")
    now = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
    embeddings = DeterministicEmbedding()
    model = SequenceModel([PersonReaction(actions=[]), PersonReaction(actions=[]), PersonReaction(actions=[]), PersonReaction(actions=[])])
    runtimes = {
        "rin": runtime_for(store, embeddings, model, "Rin"),
        "momo": runtime_for(store, embeddings, model, "Momo"),
    }
    service = GroupConversationService(store, runtimes, FixedClock(now))
    group = service.create_group("轮转群", ["rin", "momo"])
    first = service.send(group.id, "第一轮")
    second = service.send(group.id, "第二轮")
    assert first["speaker_order"] == ["rin", "momo"]
    assert second["speaker_order"] == ["momo", "rin"]
    store.close()


def test_group_create_and_list_api_do_not_initialize_model(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                'api_key: ""',
                'embedding_provider: "deterministic"',
                f'db_path: "{(tmp_path / "group-api.db").as_posix()}"',
                f'persona_path: "{(root / "personas" / "rin" / "persona.yaml").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    app = create_api(str(config))
    attach_group_routes(app, str(config))

    with TestClient(app) as client:
        assert client.get("/health").json()["runtime_loaded"] is False
        profiles = client.get("/v1/characters").json()["characters"]
        assert len(profiles) >= 2
        response = client.post("/v1/groups", json={"name":"测试群","member_ids":[profiles[0]["id"],profiles[1]["id"]]})
        assert response.status_code == 200
        group = response.json()["group"]
        listed = client.get("/v1/groups")
        assert listed.status_code == 200
        assert listed.json()["groups"][0]["id"] == group["id"]
        assert client.get("/health").json()["runtime_loaded"] is False


def test_web_has_one_submit_owner_and_group_module_never_installs_competing_submit_handler():
    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "character_memory" / "web"
    index = (web / "index.html").read_text(encoding="utf-8")
    core_path = web / "app.js"
    core = core_path.read_text(encoding="utf-8")
    groups_path = web / "groups.js"
    groups = groups_path.read_text(encoding="utf-8")
    css = (web / "p0_11.css").read_text(encoding="utf-8")

    assert "/static/groups.js" in index
    assert "/static/p0_11.js" not in index
    assert core.count('addEventListener("submit"') == 1
    assert "CM.submitCurrentText" in core
    assert 'if (CM.isGroupConversation()) return CM.features.groups?.sendText?.(message);' in core
    assert 'addEventListener("submit"' not in groups
    assert "stopImmediatePropagation" not in core
    assert "stopImmediatePropagation" not in groups
    assert "/v1/groups/" in groups
    assert "sendSticker" in groups
    assert "group-message-sticker" in css
    # The CSS above is only worth anything if the group surface asks for the
    # group variant; the renderer behaviour itself is asserted in
    # test_group_message_rendering_keeps_its_own_media_classes.
    assert '"group"' in groups

    node = shutil.which("node")
    if node:
        for path in [core_path, web / "persona.js", web / "unread.js", web / "stickers.js", web / "images.js", groups_path, web / "intent.js"]:
            checked = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            assert checked.returncode == 0, f"{path.name}: {checked.stderr}"


# Runs the real web/message_content.js in a stubbed browser realm (node's `vm`)
# and reports the markup it produces for each surface. Behavioural on purpose:
# grepping the script for a class name says nothing about whether group rows
# actually carry it.
MESSAGE_CONTENT_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const sandbox = {
  window: {
    CM: {
      escapeHtml: value => String(value === undefined || value === null ? "" : value),
      voiceMessageHtml: () => "",
    },
  },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const CM = sandbox.window.CM;

const image = {action: "IMAGE", image: {url: "/v1/media/1", label: "图"}};
const sticker = {action: "STICKER", sticker: {id: "s1", label: "表情", url: "/v1/stickers/s1/asset"}};

process.stdout.write(JSON.stringify({
  groupImage: CM.messageContentHtml(image, {variant: "group"}),
  directImage: CM.messageContentHtml(image),
  groupSticker: CM.messageContentHtml(sticker, {variant: "group"}),
  directSticker: CM.messageContentHtml(sticker),
}));
"""


def test_group_message_rendering_keeps_its_own_media_classes(tmp_path):
    """Group media keeps the classes p0_11.css sizes it with.

    Group images and stickers are deliberately smaller than direct-chat ones,
    and those rules hang on group-only classes. A shared renderer that stops
    emitting them leaves the CSS dead and silently widens every group image to
    the direct-chat size.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to render message_content.js behaviourally")

    harness = tmp_path / "message_content_harness.cjs"
    harness.write_text(MESSAGE_CONTENT_HARNESS, encoding="utf-8")
    script = (
        Path(__file__).resolve().parents[1]
        / "src" / "character_memory" / "web" / "message_content.js"
    )
    completed = subprocess.run(
        [node, str(harness), str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)

    assert "group-message-image" in payload["groupImage"]
    assert "group-message-sticker" in payload["groupSticker"]
    # Direct chat keeps the plain sizing; the hook is group-only on purpose.
    assert "group-message-image" not in payload["directImage"]
    assert "group-message-sticker" not in payload["directSticker"]


def test_old_version_override_scripts_are_removed():
    web = Path(__file__).resolve().parents[1] / "src" / "character_memory" / "web"
    for name in ["p0_5.js", "p0_6.js", "p0_7.js", "p0_8.js", "p0_11.js"]:
        assert not (web / name).exists(), name
