from pathlib import Path


def test_stack_entrypoint_and_runtime_ports_are_declared():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    script = Path("src/character_memory/dev_stack.py").read_text(encoding="utf-8")
    assert 'character-stack = "character_memory.dev_stack:main"' in pyproject
    assert 'character-settings = "character_memory.settings_server:main"' in pyproject
    assert 'character-tts-lab = "character_memory.tts_lab:main"' in pyproject
    assert "127.0.0.1:8000/health" in script
    assert "127.0.0.1:8001/health" in script
    assert "127.0.0.1:8002/health" in script
    assert "127.0.0.1:8003/health" in script
    assert '"http://127.0.0.1:9002/tts"' in script
    assert '"http://127.0.0.1:9002/health"' not in script
    assert '"character_memory.media_bootstrap"' in script
    assert '"character_memory.dev_server"' in script
    assert '"character_memory.settings_server"' in script
    assert '"character_memory.tts_lab"' in script
    assert 'choices=("dev", "chat", "settings", "tts")' in script


def test_dev_console_is_linked_from_settings_center():
    html = Path("src/character_memory/web/settings.html").read_text(encoding="utf-8")
    assert 'href="http://127.0.0.1:8000"' in html
    assert 'href="http://127.0.0.1:8002/dev"' in html
    assert 'href="http://127.0.0.1:9002/tts"' in html
    assert "Settings Center" in html


def test_configured_tts_provider_accepts_edge_and_gsv(tmp_path):
    from character_memory.dev_stack import _configured_tts_provider

    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "edge"\n', encoding="utf-8")
    assert _configured_tts_provider(str(config)) == "edge"

    config.write_text('tts_provider: "gsv"\n', encoding="utf-8")
    assert _configured_tts_provider(str(config)) == "gsv"


def test_qwen3_is_not_a_formal_stack_provider(tmp_path):
    from character_memory.dev_stack import _configured_tts_provider

    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "qwen3"\n', encoding="utf-8")
    try:
        _configured_tts_provider(str(config))
    except ValueError as exc:
        assert "Unsupported formal TTS provider" in str(exc)
    else:
        raise AssertionError("qwen3 must not be accepted as a formal TTS provider")


def test_dev_stack_reserves_voice_design_base_without_starting_it():
    script = Path("src/character_memory/dev_stack.py").read_text(encoding="utf-8")
    assert '"CHARACTER_TTS_QWEN3_VOICE_DESIGN_BASE"' in script
    assert '"http://127.0.0.1:9015"' in script
    assert '"Qwen3-TTS 1.7B VoiceDesign Runtime"' not in script
    # VoiceDesign 1.7B reserves ~4.36 GB, which on an 8 GB card leaves less than
    # GSV needs (~1.45 GB), so the two can never be resident together. Starting
    # it here would break live chat, not just cost memory. Asserting on the
    # display name alone would not catch a renamed spawn.
    assert '"character_memory.qwen3_voice_design_experiment"' not in script
    assert "QWEN3_VOICE_DESIGN_PRELOAD" not in script
    assert "9015/health" not in script


def test_dev_stack_declares_gsv_sidecar_startup():
    script = Path("src/character_memory/dev_stack.py").read_text(encoding="utf-8")
    assert '"GSV-TTS-Lite Runtime"' in script
    assert '"http://127.0.0.1:9014/health"' in script
    assert '"GSV_TTS_GPT_MODEL"' in script
    assert '"CHARACTER_TTS_GSV_BASE"' in script


def test_gsv_missing_assets_follows_the_template_readiness_contract(tmp_path):
    """Readiness is the sidecar's contract: two models *and* the default template.

    The legacy ``GSV_TTS_REF_AUDIO``/``GSV_TTS_REF_TEXT`` pair stopped taking part
    in it when the reference moved into the template, so reporting them as
    "incomplete" pointed the operator at two variables that change nothing and
    never mentioned the real cause -- a default template that does not exist.
    """
    from character_memory.dev_stack import _gsv_missing_assets

    voices = tmp_path / "voices"
    values = {
        "GSV_TTS_GPT_MODEL": "gpt.ckpt",
        "GSV_TTS_SOVITS_MODEL": "sovits.pth",
        "GSV_TTS_VOICES_ROOT": str(voices),
        # Unset on purpose: the legacy pair must not appear in the verdict.
        "GSV_TTS_REF_AUDIO": "",
        "GSV_TTS_REF_TEXT": "",
    }

    missing = _gsv_missing_assets(values)

    assert len(missing) == 1, missing
    assert "murasame" in missing[0]
    assert str(voices / "murasame.yaml") in missing[0]
    assert "GSV_TTS_REF_AUDIO" not in missing[0]

    # A template with a clip of its own completes the picture, legacy keys and all.
    clip = voices / "murasame" / "clip.wav"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"RIFF")
    (voices / "murasame.yaml").write_text(
        "ref_audio: murasame/clip.wav\nref_text: 参考文本。\n", encoding="utf-8"
    )
    assert _gsv_missing_assets(values) == []

    # An unusable template is reported with its reason, not counted as present.
    clip.unlink()
    unusable = _gsv_missing_assets(values)
    assert len(unusable) == 1 and "ref_audio" in unusable[0], unusable


def test_gsv_missing_assets_names_the_configured_default_template(tmp_path):
    """The name is the operator's, and a relative root belongs to the child's cwd."""
    from character_memory.dev_stack import ROOT, _gsv_missing_assets

    missing = _gsv_missing_assets(
        {"GSV_TTS_VOICE": "haru", "GSV_TTS_VOICES_ROOT": "voices"}
    )

    assert missing[:2] == ["GSV_TTS_GPT_MODEL", "GSV_TTS_SOVITS_MODEL"]
    assert "haru" in missing[2]
    # Children are spawned with cwd=ROOT, so that is where a relative root lands.
    assert str(ROOT / "voices" / "haru.yaml") in missing[2]


def test_dev_stack_keeps_gsv_health_checkable_without_requiring_runtime_assets():
    script = Path("src/character_memory/dev_stack.py").read_text(encoding="utf-8")
    assert '"Qwen3-TTS Runtime"' not in script
    assert 'if gsv_python.is_file()' in script
    assert 'gsv_env["GSV_TTS_PRELOAD"] = "1" if tts_provider == "gsv" and not missing_gsv else "0"' in script
    assert 'Settings Center can configure them without restarting the stack' in script
    assert 'gsv_env["GSV_TTS_DEVICE"] = tts_device' in script
    assert 'gsv_env.setdefault("GSV_TTS_VOICE"' in script
    assert 'media_env["CHARACTER_MEDIA_TTS_DEVICE"] = tts_device' in script


def test_dev_stack_keeps_project_dotenv_out_of_general_child_process_environment():
    script = Path("src/character_memory/dev_stack.py").read_text(encoding="utf-8")
    assert 'parse_env_file' in script
    assert 'base_env = os.environ.copy()' in script
    assert 'persisted_env = {**file_env, **os.environ}' in script
    assert 'gsv_env = {**file_env, **base_env}' in script
    assert 'base_env = {**file_env, **os.environ}' not in script


def test_dev_stack_pins_the_gsv_persona_root_to_an_absolute_path():
    """A relative persona root would break the moment the sidecar's cwd differs."""
    script = Path("src/character_memory/dev_stack.py").read_text(encoding="utf-8")
    assert 'gsv_env.setdefault("GSV_TTS_PERSONA_ROOT", str(ROOT / "personas"))' in script
    assert 'gsv_env.setdefault("GSV_TTS_VOICES_ROOT", str(ROOT / "voices"))' in script


def test_every_child_that_touches_templates_gets_the_absolute_voices_root():
    """Settings Center and TTS Lab are pinned too, not just the sidecar.

    They are spawned with ``cwd=ROOT``, so a relative "voices" happens to work
    today -- which is exactly the accident the absolute path exists to remove.
    Whoever reads templates in those processes next must not inherit the answer
    from the launcher's working directory.
    """
    from character_memory.dev_stack import ROOT, _settings_env, _tts_lab_env

    expected = str(ROOT / "voices")

    assert _settings_env({})["GSV_TTS_VOICES_ROOT"] == expected
    assert _tts_lab_env({}, "config.yaml")["GSV_TTS_VOICES_ROOT"] == expected


def test_gsv_start_script_pins_the_persona_root_to_an_absolute_path():
    script = Path("scripts/start-gsv-tts.sh").read_text(encoding="utf-8")
    assert 'PERSONA_ROOT="$ROOT/personas"' in script
    # Git Bash hands ROOT over as /c/Users/..., which native Python on Windows
    # reads as C:\c\Users\... -- so the value must be converted before export.
    assert 'cygpath -w "$PERSONA_ROOT"' in script
    assert 'export GSV_TTS_PERSONA_ROOT="${GSV_TTS_PERSONA_ROOT:-$PERSONA_ROOT}"' in script
