"""Source-gender fidelity at the Memory admission boundary (issue #662).

The reaction model wrote "他"/"她" into ``memory_candidates`` although the
source event carried no gender information at all, and the candidate then
became durable Memory. Prompt-level source fidelity did not stop it, so the
rule is enforced deterministically after the model and before persistence.
"""

from datetime import datetime, timedelta, timezone

import pytest

from character_memory.application.clock import FixedClock
from character_memory.application.group_conversation_service import GroupConversationService
from character_memory.domain.models import ActionDecision, ActionType, Event, EventType, MemoryCandidate, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.pronoun_fidelity import (
    enforce_source_gender_fidelity,
    has_gender_evidence,
)
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


NOW = datetime(2026, 10, 27, 9, 6, 24, tzinfo=timezone.utc)


class CandidateModel(PersonModel):
    """Returns one fixed memory candidate, whatever the context says."""

    def __init__(self, content, memory_type="USER", importance=0.8):
        self.content = content
        self.memory_type = memory_type
        self.importance = importance
        self.contexts = []

    def react(self, context):
        self.contexts.append(context)
        return PersonReaction(
            actions=[ActionDecision(type=ActionType.MESSAGE, message="嗯。")],
            memory_candidates=[
                MemoryCandidate(content=self.content, memory_type=self.memory_type, importance=self.importance),
            ],
        )


def build_runtime(store, model, persona="你是 Rin，话不多。"):
    embeddings = DeterministicEmbedding()
    return PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, persona)


def stored_contents(store, character_id="rin"):
    return [memory.content for memory in store.list_memories(character_id, include_embedding=False)]


# --- unit level: the rule itself ------------------------------------------------


def test_guard_neutralizes_pronoun_without_any_source_evidence():
    content, count = enforce_source_gender_fidelity(
        "她今天去看了电影，觉得很好看",
        "USER",
        source_text="今天去看了场电影，挺好看的",
    )
    assert count == 1
    assert content == "TA今天去看了电影，觉得很好看"
    assert "她" not in content and "他" not in content


def test_guard_replaces_every_pronoun_and_keeps_the_rest_of_the_sentence():
    content, count = enforce_source_gender_fidelity(
        "他给她买了礼物，她很高兴",
        "SHARED",
        source_text="买了个礼物，挺开心的",
    )
    assert count == 3
    assert content == "TA给TA买了礼物，TA很高兴"


def test_guard_keeps_pronoun_when_source_states_gender():
    content, count = enforce_source_gender_fidelity(
        "她觉得这部电影很好看",
        "USER",
        source_text="我是女生，今天看了部电影",
    )
    assert (content, count) == ("她觉得这部电影很好看", 0)


def test_guard_keeps_pronoun_when_source_itself_uses_a_pronoun():
    content, count = enforce_source_gender_fidelity(
        "她今天很开心",
        "USER",
        source_text="我妈今天来了，她给我带了饭",
    )
    assert (content, count) == ("她今天很开心", 0)


def test_guard_ignores_candidates_without_third_person_pronouns():
    content, count = enforce_source_gender_fidelity(
        "用户今天加班到九点，很累",
        "USER",
        source_text="今天加班到九点",
    )
    assert (content, count) == ("用户今天加班到九点，很累", 0)


def test_guard_uses_persona_only_for_character_scoped_memory():
    source = "今天搬完家了，收拾到很晚"
    persona = "你是 Rin，一个女孩子。"
    # The person's own gender is part of the character's setting.
    assert enforce_source_gender_fidelity("她今天搬完家，很累", "SELF", source_text=source, persona_text=persona) == (
        "她今天搬完家，很累",
        0,
    )
    # The same sentence attributed to the user is not licensed by the
    # character's setting: the character's gender is not the user's gender.
    content, count = enforce_source_gender_fidelity("她今天搬完家，很累", "USER", source_text=source, persona_text=persona)
    assert count == 1
    assert content == "TA今天搬完家，很累"


def test_guard_is_idempotent_for_already_neutral_content():
    content, count = enforce_source_gender_fidelity("TA今天很开心", "USER", source_text="今天很开心")
    assert (content, count) == ("TA今天很开心", 0)


# --- neutral compounds and Latin look-alikes -----------------------------------


@pytest.mark.parametrize(
    "neutral",
    ["其他", "吉他", "他乡", "他人", "他日", "他杀", "字母", "分母", "声母",
     "韵母", "母语", "师父", "弟子", "螺母", "酵母", "航母", "航空母舰"],
)
def test_neutral_compounds_are_not_gender_evidence(neutral):
    assert has_gender_evidence(neutral) is False


@pytest.mark.parametrize("gendered", ["他说", "她是", "父母", "兄弟", "姐妹", "女生", "男朋友"])
def test_real_gender_words_remain_evidence(gendered):
    assert has_gender_evidence(gendered) is True


def test_neutral_compounds_in_source_do_not_disable_the_guard():
    # Before this fix, 其他 and 吉他 matched the bare 他 substring, so the source
    # looked gendered and the unsupported 她 survived.
    content, count = enforce_source_gender_fidelity(
        "她今天在别的城市出差，晚上练了会儿吉他",
        "USER",
        source_text="今天在其他城市出差，晚上练了会儿吉他",
    )
    assert count == 1
    assert content == "TA今天在别的城市出差，晚上练了会儿吉他"


def test_neutral_compounds_in_candidate_are_not_rewritten():
    # The replacement must not corrupt a neutral word either: 其他 is not 其TA.
    content, count = enforce_source_gender_fidelity(
        "用户其他时间都在练吉他，还背了字母表，她很喜欢",
        "USER",
        source_text="今天很忙",
    )
    assert count == 1
    assert content == "用户其他时间都在练吉他，还背了字母表，TA很喜欢"


def test_latin_ta_inside_english_words_is_not_evidence():
    content, count = enforce_source_gender_fidelity(
        "她今天把 data 和 start 时间都记下了",
        "USER",
        source_text="I checked the data and started early",
    )
    assert count == 1
    assert content.startswith("TA今天")


@pytest.mark.parametrize("neutral", ["data", "start", "table", "metadata", "status"])
def test_english_words_containing_ta_are_not_evidence(neutral):
    assert has_gender_evidence(neutral) is False


@pytest.mark.parametrize("neutral_pronoun", ["TA", "ta", "Ta"])
def test_standalone_latin_neutral_pronoun_is_evidence(neutral_pronoun):
    assert has_gender_evidence(f"{neutral_pronoun}今天来了") is True


def test_provenance_accepts_only_user_authored_turns():
    """A tick, an intent or the person's own message is not gender evidence."""

    user_turn = Event(
        character_id="rin",
        event_type=EventType.USER_MESSAGE,
        event_time=NOW,
        content="今天有点累",
    )
    character_turn = Event(
        character_id="rin",
        event_type=EventType.CHARACTER_MESSAGE,
        event_time=NOW,
        content="她今天好像很累。",
    )
    proactive = Event(
        character_id="rin",
        event_type=EventType.PROACTIVE_INTENT,
        event_time=NOW,
        content="之前留下的意图：她说最近很累。",
    )
    assert PersonRuntime._memory_source_text(proactive, [user_turn, character_turn]) == "今天有点累"
    assert PersonRuntime._memory_source_text(user_turn, [character_turn]) == "今天有点累"
    # With no user-authored turn there is nothing the reaction could have learned
    # the gender from, so an unsupported pronoun is neutralized.
    content, count = enforce_source_gender_fidelity(
        "她很累",
        "USER",
        source_text=PersonRuntime._memory_source_text(proactive, [character_turn]),
    )
    assert (content, count) == ("TA很累", 1)


# --- direct runtime path --------------------------------------------------------


def test_unsupported_female_pronoun_is_neutralized_before_durable_memory(tmp_path):
    store = SQLiteStore(tmp_path / "direct-female.db")
    try:
        model = CandidateModel("她今天下班路上看到一只很大的橘猫，很喜欢")
        runtime = build_runtime(store, model)

        result = runtime.handle(
            Event(
                character_id="rin",
                event_type=EventType.USER_MESSAGE,
                event_time=NOW,
                content="今天下班路上看到一只很大的橘猫，蹲在便利店门口",
            )
        )

        # The real fact survives, only the unsupported gender attribution goes.
        assert stored_contents(store) == ["TA今天下班路上看到一只很大的橘猫，很喜欢"]
        assert result.created_memory_ids
        decision = store.get_runtime_trace(result.event.id)["memory_decisions"][0]
        assert decision["decision"] == "WRITE"
        assert decision["gender_pronoun_neutralized"] == 1
        assert decision["normalized_content"] == "TA今天下班路上看到一只很大的橘猫，很喜欢"
        # The trace still records what the model actually said.
        assert decision["candidate"]["content"] == "她今天下班路上看到一只很大的橘猫，很喜欢"
    finally:
        store.close()


def test_unsupported_male_pronoun_is_neutralized_too(tmp_path):
    store = SQLiteStore(tmp_path / "direct-male.db")
    try:
        runtime = build_runtime(store, CandidateModel("他今天加班到九点，很累"))
        runtime.handle(
            Event(
                character_id="rin",
                event_type=EventType.USER_MESSAGE,
                event_time=NOW,
                content="今天加班到九点，累死了",
            )
        )
        assert stored_contents(store) == ["TA今天加班到九点，很累"]
    finally:
        store.close()


def test_supported_pronoun_survives_when_user_states_gender(tmp_path):
    store = SQLiteStore(tmp_path / "direct-supported.db")
    try:
        runtime = build_runtime(store, CandidateModel("她觉得那家店的红茶很好喝"))
        result = runtime.handle(
            Event(
                character_id="rin",
                event_type=EventType.USER_MESSAGE,
                event_time=NOW,
                content="我是女生，今天去了那家店，红茶很好喝",
            )
        )
        assert stored_contents(store) == ["她觉得那家店的红茶很好喝"]
        decision = store.get_runtime_trace(result.event.id)["memory_decisions"][0]
        assert "gender_pronoun_neutralized" not in decision
    finally:
        store.close()


def test_pronoun_is_kept_when_an_earlier_user_message_established_gender(tmp_path):
    store = SQLiteStore(tmp_path / "direct-earlier.db")
    try:
        runtime = build_runtime(store, CandidateModel("她很累"))
        runtime.handle(
            Event(
                character_id="rin",
                event_type=EventType.USER_MESSAGE,
                event_time=NOW - timedelta(hours=1),
                content="我是男生。",
            )
        )
        runtime.handle(
            Event(
                character_id="rin",
                event_type=EventType.USER_MESSAGE,
                event_time=NOW,
                content="今天有点累",
            )
        )
        assert stored_contents(store) == ["她很累"]
    finally:
        store.close()


def test_candidate_without_pronoun_is_untouched(tmp_path):
    store = SQLiteStore(tmp_path / "direct-plain.db")
    try:
        runtime = build_runtime(store, CandidateModel("用户今天加班到九点，很累"))
        runtime.handle(
            Event(
                character_id="rin",
                event_type=EventType.USER_MESSAGE,
                event_time=NOW,
                content="今天加班到九点，累死了",
            )
        )
        assert stored_contents(store) == ["用户今天加班到九点，很累"]
    finally:
        store.close()


def test_person_own_words_do_not_license_a_user_gender_claim(tmp_path):
    """One fabrication must not become the source for the next one."""

    store = SQLiteStore(tmp_path / "direct-propagate.db")
    try:
        runtime = build_runtime(store, CandidateModel("她很累"))
        # A previous character turn already guessed the user's gender.
        store.append_event(
            Event(
                character_id="rin",
                event_type=EventType.CHARACTER_MESSAGE,
                event_time=NOW - timedelta(minutes=5),
                content="她今天好像很累。",
            )
        )
        runtime.handle(
            Event(
                character_id="rin",
                event_type=EventType.USER_MESSAGE,
                event_time=NOW,
                content="今天有点累",
            )
        )
        assert stored_contents(store) == ["TA很累"]
    finally:
        store.close()


# --- shared admission gate: group path -------------------------------------------


class SequenceModel(PersonModel):
    def __init__(self, reactions):
        self.reactions = list(reactions)
        self.contexts = []

    def react(self, context):
        self.contexts.append(context)
        return self.reactions.pop(0)


def test_group_memory_candidate_pronoun_is_neutralized(tmp_path):
    store = SQLiteStore(tmp_path / "group.db")
    try:
        embeddings = DeterministicEmbedding()
        model = SequenceModel(
            [
                PersonReaction(
                    actions=[ActionDecision(type=ActionType.MESSAGE, message="嗯。")],
                    memory_candidates=[
                        MemoryCandidate(content="她终于把那个问题修好了", memory_type="USER", importance=0.8),
                    ],
                ),
                PersonReaction(actions=[]),
            ]
        )
        runtimes = {
            "rin": PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, "你是 Rin。"),
            "momo": PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, "你是 Momo。"),
        }
        service = GroupConversationService(
            store,
            runtimes,
            FixedClock(NOW),
            profiles=[{"id": "rin", "name": "Rin"}, {"id": "momo", "name": "Momo"}],
        )
        group = service.create_group("测试群", ["rin", "momo"])

        service.send(group.id, "终于把那个问题修好了")

        assert stored_contents(store, "rin") == ["TA终于把那个问题修好了"]
    finally:
        store.close()


@pytest.mark.parametrize("memory_type", ["USER", "SHARED", "EPISODIC"])
def test_gate_neutralizes_across_uncertain_memory_types(tmp_path, memory_type):
    store = SQLiteStore(tmp_path / f"type-{memory_type}.db")
    try:
        embeddings = DeterministicEmbedding()
        runtime = PersonRuntime(store, VectorRecall(store, embeddings), embeddings, None, "你是 Rin，一个女孩子。")
        accepted, decisions = runtime._prepare_memory_writes(
            "rin",
            NOW,
            [MemoryCandidate(content="她今天很开心", memory_type=memory_type, importance=0.8)],
            source_text="今天很开心",
        )
        assert [candidate.content for candidate, _, _ in accepted] == ["TA今天很开心"]
        assert decisions[0]["gender_pronoun_neutralized"] == 1
    finally:
        store.close()
