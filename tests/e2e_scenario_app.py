"""Isolated real HTTP application for relationship-practice acceptance.

A deterministic model records every call in a JSONL file, so tests can prove
that reads and lifecycle actions never call it and that one practice turn calls
it exactly once. No provider, network or embedding download is involved.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from character_memory.activity_web import attach_activity_routes
from character_memory.api import create_api
from character_memory.application.chat_service import ChatService
from character_memory.avatar_web import attach_avatar_routes
from character_memory.call_invitation_web import attach_call_invitation_routes
from character_memory.application.clock import RealClock
from character_memory.config import Settings, discover_character_profiles, load_persona
from character_memory.domain.models import ActionDecision, ActionType, Event, EventType, PersonReaction
from character_memory.domain.relationship_scenario import ScenarioRoundOutput
from character_memory.history_web import attach_history_routes
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.memory_web import attach_memory_routes
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.scenario_web import attach_scenario_routes
from character_memory.storage.sqlite import SQLiteStore


DB_PATH = Path(os.environ["CHARACTER_MEMORY_SCENARIO_DB"])
MEDIA_DIR = Path(os.environ["CHARACTER_MEMORY_SCENARIO_MEDIA"])
PERSONA_ROOT = Path(os.environ["CHARACTER_MEMORY_SCENARIO_PERSONAS"])
CALL_LOG = Path(os.environ["SCENARIO_MODEL_CALL_LOG"])


def _log(kind: str, prompt: str = "") -> None:
    with CALL_LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"pid": os.getpid(), "kind": kind, "prompt": prompt},
                                ensure_ascii=False) + "\n")


class ScenarioModel(PersonModel):
    model = "e2e-scenario-fake"
    vision_model = "e2e-scenario-fake"

    def react(self, context: str) -> PersonReaction:
        _log("react")
        return PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE,
                                                      message="activity reply")])

    def structured_call_for_session(self, prompt, schema, session):
        if schema.__name__ == "SuggestionOutput":
            _log("suggestions", prompt)
            return SimpleNamespace(value=schema.model_validate({"suggestions": [
                {"intent": "澄清", "text": "我想先说说我的计划，你们先别急着反对。"},
                {"intent": "表达感受", "text": "我有点怕一说就被否定，所以想慢慢讲。"},
                {"intent": "提出计划", "text": "我们可以先一起算算最坏的情况。"},
            ]}))
        _log("scenario", prompt)
        current_turn = prompt.rsplit("# User's current turn", 1)[-1]
        if "FAIL_SCENARIO" in current_turn:
            raise RuntimeError("deterministic scenario provider failure")
        body = prompt.split("# Scenario\n", 1)[1].split("\n# Explicit User Experience Preferences", 1)[0]
        scenario = json.loads(body)
        perspectives = []
        for index, item in enumerate(scenario["participants"]):
            key = item["participant_key"]
            if "DUPLICATE_VIEWS" in current_turn:
                perspectives.append({"participant_id": key, "stance": "同一个答案",
                                     "perspective": "大家都应该先冷静下来。"})
                continue
            if key.startswith("sim:"):
                stance = "担心风险" if index == 1 else "先算成本"
            else:
                stance = "先照顾自己"
            perspectives.append({
                "participant_id": key,
                "stance": stance,
                "perspective": f"{item.get('display_name') or key}：{stance}。这是我自己的看法，"
                               f"和你听到的其他人不一样也没关系。",
            })
        replies = [{"participant_id": item["participant_key"],
                    "message": f"{item['display_name']}说：我听见了。你得给我一个具体的安排，"
                               f"比如下个月的钱怎么办。"}
                   for item in scenario["participants"]
                   if item["actor_type"] == "SIMULATED_CHARACTER"]
        _log("scenario_round", json.dumps({"perspectives": perspectives}, ensure_ascii=False))
        return SimpleNamespace(value=schema.model_validate(
            {"perspectives": perspectives, "replies": replies}))


settings = Settings(
    api_key="e2e-scenario-key",
    embedding_provider="deterministic",
    db_path=str(DB_PATH),
    media_dir=str(MEDIA_DIR),
    sticker_dir=str(DB_PATH.parent / "scenario-stickers"),
    avatar_dir=str(DB_PATH.parent / "scenario-avatars"),
    persona_path=str(PERSONA_ROOT / "rin" / "persona.yaml"),
    proactive_wake_enabled=False,
)
store = SQLiteStore(settings.db_path)
embeddings = DeterministicEmbedding()
model = ScenarioModel()
profiles = discover_character_profiles(settings)
personas = {profile["id"]: load_persona(profile["persona_path"]) for profile in profiles}
recall = VectorRecall(store, embeddings, limit=settings.recall_limit)
runtimes = {
    character_id: PersonRuntime(store, recall, embeddings, model, persona)
    for character_id, persona in personas.items()
}
clock = RealClock()
bundle = SimpleNamespace(
    settings=settings,
    store=store,
    runtime=next(iter(runtimes.values())),
    runtimes=runtimes,
    characters=profiles,
    chat=ChatService(store, runtimes, clock),
    embeddings=embeddings,
    model=model,
    clock=clock,
    init_timings={"total_ms": 0.0},
)

app = create_api(bundle=bundle)
attach_memory_routes(app)
attach_activity_routes(app)
attach_scenario_routes(app)
# Keep the isolated page free of unrelated "route missing" status text so the
# screenshots show the practice surface, not fixture gaps.
attach_call_invitation_routes(app)
attach_avatar_routes(app)
attach_history_routes(app)


# The shared shell asks for the Space notification badge on every page. The
# practice acceptance does not need the Space runtime, so this fixture serves
# the empty contract instead of importing the scheduler (and its tz database).
@app.get("/v1/space/notifications")
def space_notifications(unread_only: bool = True, limit: int = 20):
    return {"notifications": [], "unread_count": 0, "max_items": 100,
            "background_push": False}


@app.get("/v1/groups")
def groups(archived: bool = False):
    # The shared sidebar loads groups on every page. This fixture owns no
    # groups; returning the real empty contract keeps bootstrap from failing
    # and leaves the practice surface with no unrelated error text.
    return {"groups": []}


@app.get("/__e2e/scenario-runtime")
def runtime():
    calls: list[dict] = []
    if CALL_LOG.exists():
        calls = [json.loads(line) for line in CALL_LOG.read_text(encoding="utf-8").splitlines()]
    return {"pid": os.getpid(), "calls": calls,
            "counts": {kind: sum(1 for item in calls if item["kind"] == kind)
                       for kind in ("react", "suggestions", "scenario")}}


@app.get("/__e2e/audit")
def audit():
    read = app.state.character_memory.read_store
    tables = {row[0] for row in read.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    def count(sql: str, args=()):
        return int(read.conn.execute(sql, args).fetchone()[0])

    scope_rows = read.conn.execute(
        "SELECT experience_scope_json FROM scenario_events").fetchall()
    bad_scope = 0
    for row in scope_rows:
        try:
            scope = json.loads(row[0])
        except ValueError:
            bad_scope += 1
            continue
        if not (isinstance(scope, dict) and scope.get("schema_version") == 1
                and scope.get("reality") == "SIMULATED" and scope.get("activity_id")):
            bad_scope += 1
    return {
        "pid": os.getpid(),
        "memories": count("SELECT COUNT(*) FROM memories"),
        "events": count("SELECT COUNT(*) FROM events"),
        "conversation_events": (count("SELECT COUNT(*) FROM conversation_events")
                                if "conversation_events" in tables else 0),
        "scenario_events": count("SELECT COUNT(*) FROM scenario_events"),
        "scenario_events_bad_scope": bad_scope,
        "real_leaks": {
            "memories": count("SELECT COUNT(*) FROM memories WHERE content LIKE '%辞职换%'"),
            "events": count("SELECT COUNT(*) FROM events WHERE content LIKE '%辞职换%'"),
            "conversation_events": (count(
                "SELECT COUNT(*) FROM conversation_events WHERE content LIKE '%辞职换%'")
                if "conversation_events" in tables else 0),
        },
        "personas_table": "personas" in tables,
    }


@app.post("/__e2e/seed-real-fact")
def seed_real_fact(body: dict):
    event = store.append_message(Event(
        character_id="rin", event_type=EventType.CHARACTER_MESSAGE,
        event_time=datetime.now(timezone.utc),
        content=str(body.get("content") or "真实私聊里的一句普通问候")))
    return {"event_id": event.id}


def _close_test_store() -> None:
    store.close()


app.router.on_shutdown.append(_close_test_store)
