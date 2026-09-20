"""The settings surface after references moved into templates."""

from __future__ import annotations

from character_memory.settings_store import (
    ENV_EDITABLE_FIELDS,
    GSV_RUNTIME_FIELDS,
    SETTINGS_SCHEMA,
)


def _gsv_field_names() -> set[str]:
    section = next(s for s in SETTINGS_SCHEMA if s["id"] == "gsv-runtime")
    return {field["name"] for field in section["fields"]}


def test_reference_fields_are_gone_from_the_settings_schema():
    assert _gsv_field_names() == {
        "GSV_TTS_GPT_MODEL",
        "GSV_TTS_SOVITS_MODEL",
        "GSV_TTS_VOICE",
    }


def test_the_remaining_fields_stay_env_backed_and_hot_applied():
    assert GSV_RUNTIME_FIELDS == {
        "GSV_TTS_GPT_MODEL",
        "GSV_TTS_SOVITS_MODEL",
        "GSV_TTS_VOICE",
    }
    assert GSV_RUNTIME_FIELDS <= ENV_EDITABLE_FIELDS


def test_gsv_payload_omits_the_reference_keys_entirely():
    """Writing "" would blank the live reference.

    ``configure`` treats ``None`` as "leave alone" and ``""`` as "overwrite",
    and ref_audio/ref_text have no empty-string fallback (unlike default_voice
    and friends). So the key must be *absent*, not empty -- otherwise removing
    the settings fields silently blanks the running sidecar's reference, which
    shows up as "GSV got worse", not as an error.

    This is a behaviour assertion on the returned dict. A source-string match
    could not notice a value that quietly became "".
    """

    from character_memory.settings_server import build_gsv_payload

    payload = build_gsv_payload(
        {
            "GSV_TTS_GPT_MODEL": "base.ckpt",
            "GSV_TTS_SOVITS_MODEL": "base.pth",
            # Deliberately present in the input, even though nothing reads
            # them any more: the payload builder must not pass these keys on
            # no matter what a merged settings snapshot happens to contain.
            "GSV_TTS_REF_AUDIO": "C:/old/ref.wav",
            "GSV_TTS_REF_TEXT": "旧参考文本",
            "GSV_TTS_VOICE": "murasame",
        }
    )

    assert "ref_audio" not in payload
    assert "ref_text" not in payload
    assert payload["gpt_model"] == "base.ckpt"
    assert payload["voice"] == "murasame"


def test_gsv_payload_fills_a_default_template_name_when_unset():
    from character_memory.settings_server import build_gsv_payload

    payload = build_gsv_payload({})

    assert payload["voice"] == "murasame"
