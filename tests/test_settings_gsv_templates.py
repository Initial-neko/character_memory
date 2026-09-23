"""The settings surface after references moved into templates."""

from __future__ import annotations

import pytest

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


class _FakeProviderClient:
    """Stands in for the Settings Center's runtime HTTP client.

    ``tts_inventory`` is a closure inside ``create_settings_app`` and reads this
    client, so feeding it is the only way to exercise the real injection path.
    """

    def __init__(self, providers: dict[str, dict]):
        self._providers = providers

    def get(self, url: str, *, timeout: float | None = None):
        provider_id = url.rstrip("/").rsplit("/", 1)[-1]
        payload = self._providers.get(provider_id)
        if payload is None:
            raise RuntimeError(f"no fake provider for {url}")

        class _Response:
            @staticmethod
            def raise_for_status() -> None: ...

            @staticmethod
            def json() -> dict:
                return {"provider": payload}

        return _Response()

    def close(self) -> None: ...


_GSV_PROVIDER = {
    "id": "gsv",
    "label": "GSV-TTS-Lite",
    "ready": True,
    "loaded": False,
    "voices": ["murasame", "haru"],
    "default_voice": "murasame",
}


def _gsv_field(client) -> dict:
    schema = client.get("/v1/settings").json()["schema"]
    section = next(item for item in schema if item["id"] == "gsv-runtime")
    return next(field for field in section["fields"] if field["name"] == "GSV_TTS_VOICE")


def test_gsv_section_offers_the_registry_as_options(tmp_path):
    """Settings Center renders the template list; no new field type needed."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import character_memory.settings_server as settings_server
    from character_memory.settings_store import SettingsStore

    app = settings_server.create_settings_app(
        store=SettingsStore(str(tmp_path / "config.yaml"), env_path=str(tmp_path / ".env")),
        runtime_http_client=_FakeProviderClient({"gsv": _GSV_PROVIDER}),
    )

    with TestClient(app) as client:
        field = _gsv_field(client)

    assert field["type"] == "select"
    assert [option["value"] for option in field["options"]] == ["murasame", "haru"]
    assert all(not option["disabled"] for option in field["options"])


def test_the_template_field_offers_nothing_when_the_sidecar_lists_no_templates(tmp_path):
    """Nothing to choose from, so point at where templates come from.

    A sidecar that *is* listing templates is a different case -- its options stay
    selectable even while it reports itself unready, because that is the state
    the selector exists to repair (see
    ``test_settings_center.py::test_a_missing_default_template_does_not_lock_the_field_that_fixes_it``).
    """

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import character_memory.settings_server as settings_server
    from character_memory.settings_store import SettingsStore

    not_ready = {**_GSV_PROVIDER, "ready": False, "voices": []}
    app = settings_server.create_settings_app(
        store=SettingsStore(str(tmp_path / "config.yaml"), env_path=str(tmp_path / ".env")),
        runtime_http_client=_FakeProviderClient({"gsv": not_ready}),
    )

    with TestClient(app) as client:
        field = _gsv_field(client)

    assert field["options"] == [
        {"value": "", "label": "尚无模板，请到 TTS Lab 的声音合成页创建一个", "disabled": True}
    ]


def test_a_configured_template_that_no_longer_exists_stays_visible(tmp_path):
    """Otherwise the dropdown shows the field as empty and the user cannot tell
    that the runtime is still pointing somewhere."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import character_memory.settings_server as settings_server
    from character_memory.settings_store import SettingsStore

    # env_path is pinned into tmp_path on purpose: SettingsStore defaults it to
    # the repo's real .env, and a test must never write there.
    store = SettingsStore(str(tmp_path / "config.yaml"), env_path=str(tmp_path / ".env"))
    store.save_values({"GSV_TTS_VOICE": "ghost"})
    app = settings_server.create_settings_app(
        store=store,
        runtime_http_client=_FakeProviderClient({"gsv": _GSV_PROVIDER}),
    )

    with TestClient(app) as client:
        field = _gsv_field(client)

    first = field["options"][0]
    assert first["value"] == "ghost"
    assert "当前配置" in first["label"]
    assert "GSV 模板不存在" in first["label"]
    assert first["disabled"] is True
    assert "不受这个警告影响" in field["help"]
    assert "Provider=kokoro" in field["help"]


def test_missing_gsv_default_is_a_blocking_warning_when_gsv_is_formal_provider(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import character_memory.settings_server as settings_server
    from character_memory.settings_store import SettingsStore

    store = SettingsStore(str(tmp_path / "config.yaml"), env_path=str(tmp_path / ".env"))
    store.save_values({
        "GSV_TTS_VOICE": "ghost",
        "tts_provider": "gsv",
        "tts_voice": "murasame",
    })
    app = settings_server.create_settings_app(
        store=store,
        runtime_http_client=_FakeProviderClient({"gsv": _GSV_PROVIDER}),
    )

    with TestClient(app) as client:
        field = _gsv_field(client)

    assert "当前正式 TTS 正在使用 GSV" in field["help"]
    assert "会受影响" in field["help"]
