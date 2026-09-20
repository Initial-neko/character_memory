# GSV 音色模板（Voice Templates）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「一个音色」收敛成唯一载体——模板（`voices/<名>.yaml`）——让角色的 `voice.yaml` 退化成一行 `template: <名>`，并让角色能在聊天页 drawer 里选声线。

**Architecture:** 模板与角色引用是两棵树，加载期合并成一个 `{voice_id: VoiceProfile}` 注册表，运行期查表。模板是唯一的参考音频持有者；角色只持有名字。固化 = 写 WAV → 写模板 → 写引用（顺序固定，崩溃只留孤儿不留悬空引用）。旧数据由两条一次性迁移搬过来，**迁移必须跑在严格校验之前**。

**Tech Stack:** Python 3.12 / Pydantic v2 / FastAPI / pytest / 原生浏览器 JS（IIFE + `CM.registerFeature`，无构建步骤）/ GSV-TTS-Lite sidecar（`:9014`）/ TTS Lab（`:9002`）。

## Global Constraints

- **`voices/` 整体 gitignore**（spec §4）。`personas/<id>/voice.yaml` 是配置、要提交；音频是资产、不提交。
- **原型路径必须绝对**：`GSV_TTS_PERSONA_ROOT` 与新的 `GSV_TTS_VOICES_ROOT` 都由启动脚本与 `dev_stack` 钉绝对路径。相对路径会随 cwd 变化而崩（既有的 `_persona_root` 注释已说明，`gsv_tts_experiment.py:93-104`）。
- **`VoiceProfileError` 是跨模块契约**：错误串要逐字稳定，`tests/test_tts_lab_voice_freeze.py` / `test_gsv_tts_experiment.py` 依赖文案。
- **跨边界必须有往返测试**：写者 ↔ 读者（`voices.py:44-56` 的 docstring 记录了这条教训——漂移上线过一次且静默失败）。
- **降级 vs 报错的分界**（spec §5.2）：角色**没有** `voice.yaml` → 静默用默认模板；角色**有** `voice.yaml` 但模板失效 → **响亮抛 `VoiceProfileError`**。
- **`configure` 语义**：`None` = 不改，`""` = 覆盖。`ref_audio`/`ref_text` 没有空串兜底（`gsv_tts_experiment.py:459-473`）。
- **不改 `tts_voice`**（跨 provider 字段，spec §6.3）。
- **冻结产物不可再生**：VoiceDesign 无种子，所以固化必须持久化**当年那串字节**，绝不重新合成。
- **测试绝不碰真实的 `.env`**：`SettingsStore` 的 `env_path` 默认指向仓库根的真实 `.env`。任何测试若构造 `SettingsStore` 并写入，都必须显式传 `env_path=<tmp_path>/.env`。这个文件由用户自己管理。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/character_memory/voices.py` | 模板 / 角色引用的**读取、校验、合并解析**。纯读，不写盘 | 改 |
| `src/character_memory/voice_migration.py` | 两条一次性迁移，**唯一**允许写 `voices/` 与角色 `voice.yaml` 的模块（除固化路由外） | 新建 |
| `src/character_memory/gsv_tts_experiment.py` | sidecar：注册表加载、三级解析、就绪条件 | 改 |
| `src/character_memory/tts_lab.py` | 固化改造（三写入）、保存为模板 | 改 |
| `src/character_memory/voice_web.py` | 聊天页的声线读/写端点。**必须挂在这里而不是 `tts_lab`**：`CM.api` 用相对路径 `fetch`（`app.js:61`），打的是聊天页自己的源，够不到 `:9002`。范式照 `avatar_web.py` | 新建 |
| `src/character_memory/server.py` | 注册 `attach_voice_routes` | 改 |
| `src/character_memory/settings_store.py` | GSV 段 5 字段 → 3 字段 | 改 |
| `src/character_memory/settings_server.py` | `_gsv_payload` 省略键、GSV 段动态选项 | 改 |
| `src/character_memory/web/voices.js` | drawer 声线面板 | 新建 |
| `src/character_memory/web/voices.css` | 它的样式 | 新建 |
| `src/character_memory/web/index.html` | 加载上面两个文件 | 改 |
| `src/character_memory/web/tts_lab.js` + `.html` | 「保存为模板」按钮 + 覆盖警告 | 改 |

---

### Task 1: `voices.py` — 模板读取与发现

**Files:**
- Modify: `src/character_memory/voices.py`
- Test: `tests/test_voices_templates.py` (新建)

**Interfaces:**
- Consumes: 无（第一个任务）
- Produces:
  - `TEMPLATE_FILE_SUFFIX: str = ".yaml"`
  - `load_template(path: str | Path) -> VoiceProfile`
  - `discover_templates(root: str | Path) -> dict[str, VoiceProfile]`
  - `template_root(configured: str | Path | None = None) -> Path`

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_voices_templates.py`：

```python
"""Template-side readers.

A template is a named ``VoiceProfile`` under ``voices/``. It is the only thing
in the repo that owns a reference clip; a character only ever names one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from character_memory.voices import (
    VoiceProfile,
    VoiceProfileError,
    discover_templates,
    load_template,
    template_root,
)


def _write_template(root: Path, name: str, **fields) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    audio = root / name / "clip.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFF")
    document = {
        "ref_audio": fields.pop("ref_audio", str(audio)),
        "ref_text": fields.pop("ref_text", "你好，今天天气不错。"),
        **fields,
    }
    path = root / f"{name}.yaml"
    import yaml

    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_load_template_reads_a_named_profile(tmp_path):
    _write_template(tmp_path, "haru")

    profile = load_template(tmp_path / "haru.yaml")

    assert isinstance(profile, VoiceProfile)
    assert profile.voice_id == "haru"          # defaults to the file stem
    assert profile.ref_text == "你好，今天天气不错。"


def test_load_template_rejects_ref_audio_that_does_not_exist(tmp_path):
    path = _write_template(tmp_path, "haru", ref_audio=str(tmp_path / "missing.wav"))

    with pytest.raises(VoiceProfileError, match="ref_audio not found"):
        load_template(path)


def test_load_template_rejects_empty_ref_text(tmp_path):
    path = _write_template(tmp_path, "haru", ref_text="   ")

    with pytest.raises(VoiceProfileError, match="ref_text is empty"):
        load_template(path)


def test_load_template_rejects_unknown_fields(tmp_path):
    """A typo must stay loud: sovits_mdoel would otherwise inherit the global model."""

    path = _write_template(tmp_path, "haru", sovits_mdoel="x.pth")

    with pytest.raises(VoiceProfileError, match="invalid voice profile"):
        load_template(path)


def test_discover_templates_is_empty_when_root_is_missing(tmp_path):
    """A missing root must never stop the sidecar from starting."""

    assert discover_templates(tmp_path / "nope") == {}


def test_discover_templates_rejects_duplicate_voice_id(tmp_path):
    _write_template(tmp_path, "haru")
    _write_template(tmp_path, "haru2", voice_id="haru")

    with pytest.raises(VoiceProfileError, match="duplicate voice_id 'haru'"):
        discover_templates(tmp_path)


def test_template_root_prefers_the_explicit_argument(tmp_path, monkeypatch):
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "from-env"))

    assert template_root(tmp_path / "explicit") == tmp_path / "explicit"
    assert template_root(None) == tmp_path / "from-env"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_voices_templates.py -q --no-header`
Expected: FAIL — `ImportError: cannot import name 'load_template' from 'character_memory.voices'`

- [ ] **Step 3: 实现**

在 `src/character_memory/voices.py` 的 `load_voice_profile` **之前**插入（保留 `load_voice_profile` / `discover_voice_profiles` 原样不动——它们是迁移要用的 legacy 读者，Task 5 会加 docstring 说明）：

```python
import os

TEMPLATE_FILE_SUFFIX = ".yaml"


def template_root(configured: str | Path | None = None) -> Path:
    """Resolve the templates directory, preferring the argument over the env.

    Mirrors ``gsv_tts_experiment._persona_root``: a relative glob resolves
    against the *cwd*, which the sidecar does not control, so the start script
    and the dev stack pin an absolute path through ``GSV_TTS_VOICES_ROOT``.
    """

    if configured is not None:
        value = str(configured).strip()
        if value:
            return Path(value)
    return Path((os.getenv("GSV_TTS_VOICES_ROOT") or "").strip() or "voices")


def load_template(path: str | Path) -> VoiceProfile:
    """Load a template document.

    Same schema as :class:`VoiceProfile` plus the provenance fields, so the
    reader is shared with the legacy per-character loader. Raises
    :class:`VoiceProfileError` for anything unusable: GSV cannot recover from a
    bad reference clip at synthesis time.
    """

    path = Path(path)
    if not path.is_file():
        raise VoiceProfileError(f"{path}: template not found")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise VoiceProfileError(f"{path}: unreadable template: {exc}") from exc

    if raw is None:
        raise VoiceProfileError(f"{path}: empty template; ref_audio and ref_text are required")
    if not isinstance(raw, dict):
        raise VoiceProfileError(f"{path}: expected a YAML mapping, got {type(raw).__name__}")

    try:
        document = _VoiceDocument.model_validate(raw)
    except ValidationError as exc:
        raise VoiceProfileError(
            f"{path}: invalid voice profile: {_describe_validation_error(exc)}"
        ) from exc

    voice_id = (document.voice_id or "").strip() or path.stem

    raw_audio = (document.ref_audio or "").strip()
    if not raw_audio:
        raise VoiceProfileError(f"{path}: ref_audio is required")
    audio_path = Path(raw_audio)
    if not audio_path.is_absolute():
        audio_path = path.parent / audio_path
    resolved_audio = audio_path.resolve()
    if not resolved_audio.is_file():
        raise VoiceProfileError(f"{path}: ref_audio not found: {resolved_audio}")

    ref_text = (document.ref_text or "").strip()
    if not ref_text:
        raise VoiceProfileError(f"{path}: ref_text is empty; GSV needs a reference transcript")

    return VoiceProfile(
        voice_id=voice_id,
        ref_audio=str(resolved_audio),
        ref_text=ref_text,
        gpt_model=_optional_str(document.gpt_model),
        sovits_model=_optional_str(document.sovits_model),
    )


def discover_templates(root: str | Path | None = None) -> dict[str, VoiceProfile]:
    """Build the ``{voice_id: profile}`` registry for every ``voices/*.yaml``.

    A missing root contributes nothing. Duplicate voice ids are an error: one
    template silently shadowing another is exactly what this registry prevents.
    """

    directory = template_root(root)
    paths = sorted(directory.glob(f"*{TEMPLATE_FILE_SUFFIX}")) if directory.exists() else []

    profiles: dict[str, VoiceProfile] = {}
    sources: dict[str, Path] = {}
    for path in paths:
        profile = load_template(path)
        previous = sources.get(profile.voice_id)
        if previous is not None:
            raise VoiceProfileError(
                f"duplicate voice_id {profile.voice_id!r}: {previous} and {path}"
            )
        profiles[profile.voice_id] = profile
        sources[profile.voice_id] = path
    return profiles
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_voices_templates.py -q --no-header`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add src/character_memory/voices.py tests/test_voices_templates.py
git commit -m "Add template readers to the voice registry"
```

---

### Task 2: `voices.py` — 角色引用与两棵树合并

**Files:**
- Modify: `src/character_memory/voices.py`
- Test: `tests/test_voices_templates.py` (追加)

**Interfaces:**
- Consumes: Task 1 的 `load_template` / `discover_templates` / `VoiceProfile`
- Produces:
  - `CHARACTER_VOICE_FILENAME: str = "voice.yaml"`
  - `load_character_voice(persona_path: str | Path) -> str | None`
  - `discover_character_voices(persona_paths: Iterable[str | Path]) -> dict[str, str]`
  - `resolve_voice_registry(*, templates: dict[str, VoiceProfile], character_voices: dict[str, str]) -> dict[str, VoiceProfile]`

**关键语义（spec §5.2）**：角色**没有** `voice.yaml` → 不进注册表（静默降级给调用方）；角色**有** `voice.yaml` 但模板不存在 → `VoiceProfileError`。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_voices_templates.py`：

```python
# --- character references ---------------------------------------------------

from character_memory.voices import (
    discover_character_voices,
    load_character_voice,
    resolve_voice_registry,
)


def _write_character(
    root: Path,
    character_id: str,
    document: dict | None,
    *,
    persona_id: str | None = None,
) -> Path:
    """``character_id`` names the directory; ``persona_id`` fills the ``id`` field.

    They are separate parameters because they are separate things in the app, and
    one case below depends on them disagreeing.
    """

    persona = root / character_id / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text(
        f"id: {persona_id or character_id}\nname: {character_id}\n", encoding="utf-8"
    )
    if document is not None:
        import yaml

        (persona.parent / "voice.yaml").write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    return persona


def test_load_character_voice_returns_none_without_a_file(tmp_path):
    persona = _write_character(tmp_path, "haru", None)

    assert load_character_voice(persona) is None


def test_load_character_voice_reads_the_template_name(tmp_path):
    persona = _write_character(tmp_path, "haru", {"template": "murasame"})

    assert load_character_voice(persona) == "murasame"


def test_load_character_voice_rejects_a_self_contained_profile(tmp_path):
    """The old form has no writer any more; seeing one means migration did not run."""

    persona = _write_character(
        tmp_path, "haru", {"ref_audio": "voice/x.wav", "ref_text": "你好"}
    )

    with pytest.raises(VoiceProfileError, match="invalid voice profile"):
        load_character_voice(persona)


def test_load_character_voice_requires_a_non_empty_name(tmp_path):
    persona = _write_character(tmp_path, "haru", {"template": "   "})

    with pytest.raises(VoiceProfileError, match="template is empty"):
        load_character_voice(persona)


def test_resolve_voice_registry_maps_a_character_onto_its_template(tmp_path):
    _write_template(tmp_path / "voices", "murasame")
    personas = tmp_path / "personas"
    persona = _write_character(personas, "haru", {"template": "murasame"})

    registry = resolve_voice_registry(
        templates=discover_templates(tmp_path / "voices"),
        character_voices=discover_character_voices([persona]),
    )

    assert registry["haru"].ref_text == "你好，今天天气不错。"
    assert registry["haru"].voice_id == "haru"   # the character id wins
    assert registry["murasame"].voice_id == "murasame"  # the template stays addressable


def test_resolve_voice_registry_raises_when_a_referenced_template_is_missing(tmp_path):
    """A character that *has* a voice file but a dead reference is a config error."""

    personas = tmp_path / "personas"
    persona = _write_character(personas, "haru", {"template": "ghost"})

    with pytest.raises(VoiceProfileError, match="haru references unknown template 'ghost'"):
        resolve_voice_registry(
            templates={},
            character_voices=discover_character_voices([persona]),
        )


def test_resolve_voice_registry_inherits_the_template_models(tmp_path):
    """A character cannot set models itself -- ``_CharacterVoiceDocument`` forbids
    it, because the same clip under two characters must not resolve to two
    different models. It inherits whatever the template declares."""
    _write_template(tmp_path / "voices", "murasame", gpt_model="base.ckpt")
    personas = tmp_path / "personas"
    persona = _write_character(personas, "haru", {"template": "murasame"})

    registry = resolve_voice_registry(
        templates=discover_templates(tmp_path / "voices"),
        character_voices=discover_character_voices([persona]),
    )

    assert registry["haru"].gpt_model == "base.ckpt"


def test_discover_character_voices_keys_on_the_persona_id(tmp_path):
    """The browser sends ``voice: <profile["id"]>`` -- the ``id`` field, not the
    directory name. A registry keyed on the directory would never match that
    request and the character would fall back to the default template in
    silence, which is the failure this whole layer exists to prevent."""

    personas = tmp_path / "personas"
    persona = _write_character(
        personas, "haru", {"template": "murasame"}, persona_id="haruka"
    )

    assert discover_character_voices([persona]) == {"haruka": "murasame"}


def test_a_character_without_a_voice_file_stays_out_of_the_registry(tmp_path):
    """The other half of spec 5.2: never given a voice is normal, not an error."""

    personas = tmp_path / "personas"
    persona = _write_character(personas, "haru", None)

    assert discover_character_voices([persona]) == {}


def test_load_character_voice_rejects_a_document_that_is_not_a_mapping(tmp_path):
    """A list is the realistic typo (a stray ``- ``), and it must not read as
    'no voice configured' -- that would be the silent fallback this forbids."""

    persona = _write_character(tmp_path, "haru", None)
    (persona.parent / "voice.yaml").write_text("- murasame\n", encoding="utf-8")

    with pytest.raises(VoiceProfileError, match="expected a YAML mapping with a 'template' key"):
        load_character_voice(persona)


def test_discover_character_voices_rejects_two_personas_claiming_one_id(tmp_path):
    """Two directories may declare the same ``id``, and the app keeps whichever
    it finds first while dropping the rest. A voice cannot be attached to an id
    two personas claim: whichever one won, the other would be silently wrong."""

    personas = tmp_path / "personas"
    first = _write_character(personas, "haru", {"template": "murasame"})
    second = _write_character(
        personas, "haru-alt", {"template": "murasame"}, persona_id="haru"
    )

    with pytest.raises(VoiceProfileError, match="duplicate character id 'haru'"):
        discover_character_voices([first, second])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_voices_templates.py -q --no-header`
Expected: FAIL — `ImportError: cannot import name 'load_character_voice'`

- [ ] **Step 2b: 先收敛读文档的重复，再加第三个读者**

Task 1 的审查把这条列为 Important，并建议**随本任务做**而不是回头返工：`load_template`（`voices.py:127-142`、`157-169`）与 `load_voice_profile`（`voices.py:216-235`、`246-262`）的「读盘 → `yaml.safe_load` → `isinstance(raw, dict)` → `model_validate` → 包装成 `VoiceProfileError`」这一段几乎逐字相同。本任务正要加**第三个**读者 `load_character_voice`，不先收敛就是抄第三遍；而这个模块 docstring 记的教训恰恰是「同一份 schema 的多个读者漂移，而且静默失败」。

**先抽 helper，再写新读者。** 目标形状：

```python
def _read_document(path, model, *, unreadable, empty) -> BaseModel:
    """读盘、解析、校验一份 voice 文档，把每种失败统一包装成 VoiceProfileError。"""
```

- 读盘 + `yaml.safe_load` + `OSError`/`YAMLError` 包装
- `raw is None` 与「不是 mapping」的两种分支（各调用方文案不同，用参数传）
- `model_validate` + `ValidationError` 包装

三个调用方各自只留**真正属于自己**的部分：读哪个路径、缺省 `voice_id` 从哪来（模板是 `path.stem`、角色是父目录名）、以及返回什么（`VoiceProfile` 还是模板名字符串）。文件不存在时的行为也不同（`load_template` 抛错、`load_voice_profile` 返回 `None`、`load_character_voice` 返回 `None`），这部分**留在各调用方**，不进 helper。

**硬约束：既有错误文案逐字节不变。** 回归闸门是

Run: `uv run pytest tests/test_voices.py tests/test_voices_templates.py -q --no-header`
Expected: `tests/test_voices.py` 与改动前**同样通过**（该文件对 `load_voice_profile` 有约 30 条断言，其中 4 条直接比对文案：`tests/test_voices.py:81,118,128,150`，含 `str(missing.resolve()) in str(excinfo.value)`）。

**若你发现某条文案无法在不改变字节的前提下共享，停下报告，不要改文案。** 跨模块契约优先于 DRY；这不是可以自行权衡的地方。

- [ ] **Step 3: 实现**

追加到 `src/character_memory/voices.py` 末尾：

```python
CHARACTER_VOICE_FILENAME = "voice.yaml"


class _CharacterVoiceDocument(BaseModel):
    """Raw shape of a character's ``voice.yaml``: a reference and nothing else.

    ``extra="forbid"`` is load-bearing twice over. It keeps the old
    self-contained form (``ref_audio``/``ref_text``) from silently working,
    which would let a character hold a clip again; and it keeps ``gpt_model``
    out, because inheriting a base model is a template-level decision -- the
    same clip under two characters must not resolve to two different models.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    template: str


def load_character_voice(persona_path: str | Path) -> str | None:
    """Return the template a character references, or ``None`` when it has none.

    ``None`` is the normal case and means "this character was never given a
    voice"; the caller degrades to the default template. A file that exists but
    cannot be trusted raises: the user configured this character deliberately,
    and silently falling back would make a wrong voice hard to notice.
    """

    persona_path = Path(persona_path)
    voice_path = persona_path.parent / CHARACTER_VOICE_FILENAME
    if not voice_path.is_file():
        return None

    try:
        raw = yaml.safe_load(voice_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise VoiceProfileError(f"{voice_path}: unreadable voice reference: {exc}") from exc

    if not isinstance(raw, dict):
        raise VoiceProfileError(
            f"{voice_path}: expected a YAML mapping with a 'template' key"
        )

    try:
        document = _CharacterVoiceDocument.model_validate(raw)
    except ValidationError as exc:
        raise VoiceProfileError(
            f"{voice_path}: invalid voice profile: {_describe_validation_error(exc)}"
        ) from exc

    name = (document.template or "").strip()
    if not name:
        raise VoiceProfileError(f"{voice_path}: template is empty")
    return name


def _character_id(persona_path: Path) -> str:
    """Resolve a character's id exactly as ``discover_character_profiles`` does.

    The ``id`` field wins and the directory name is the fallback. It has to be
    the *same expression* as ``config.py:180``, because the browser asks for a
    voice by ``profile["id"]`` -- that field, not the directory. A registry
    anchored on the directory name answers a question nobody asks for any
    persona whose two disagree, and the character drops to the default template
    without a word.

    An unreadable persona document degrades to the directory name rather than
    raising: whether a persona is usable is the persona loader's call to make
    (``discover_character_profiles`` drops it), and this function has no
    standing to fail the whole voice registry over it.
    """

    try:
        data = yaml.safe_load(persona_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        data = None
    if not isinstance(data, dict):
        data = {}
    return str(data.get("id") or persona_path.parent.name).strip()


def discover_character_voices(
    persona_paths: Iterable[str | Path],
) -> dict[str, str]:
    """Build ``{character_id: template_name}`` for every persona that names one.

    ``persona_paths`` are ``*/persona.yaml`` paths, the same set the app
    discovers. The key is the character's id per :func:`_character_id`, which is
    what the browser sends; a character with no ``voice.yaml`` is simply absent,
    and one whose ``voice.yaml`` is unusable raises from
    :func:`load_character_voice` before this function has an id to key on.
    """

    references: dict[str, str] = {}
    for persona_path in persona_paths:
        persona_path = Path(persona_path)
        name = load_character_voice(persona_path)
        if name is None:
            continue
        character_id = _character_id(persona_path)
        if not character_id:
            # Reachable only when the ``id`` field is present but blank, since
            # the directory name is the fallback. ``discover_character_profiles``
            # drops that character entirely, so there is nothing for a voice to
            # attach to -- and failing loud beats registering an unreachable key.
            raise VoiceProfileError(
                f"{persona_path}: persona id is empty; omit it to default to the directory name"
            )
        previous = references.get(character_id)
        if previous is not None:
            raise VoiceProfileError(
                f"duplicate character id {character_id!r}: two voice files claim it"
            )
        references[character_id] = name
    return references


def resolve_voice_registry(
    *,
    templates: dict[str, VoiceProfile],
    character_voices: dict[str, str],
) -> dict[str, VoiceProfile]:
    """Merge the template tree and the character tree into one registry.

    The result maps both template names and character ids onto profiles, so a
    request for either resolves in one lookup. A character overrides a template
    of the same name, because the character is the more specific answer.

    A character pointing at a template that does not exist raises: that is a
    configuration error, not an unconfigured character. An *unreferenced* missing
    template is not this function's business -- readiness (``_asset_status``)
    reports a default template that was never created.
    """

    registry: dict[str, VoiceProfile] = dict(templates)

    for character_id, template_name in character_voices.items():
        profile = templates.get(template_name)
        if profile is None:
            raise VoiceProfileError(
                f"{character_id} references unknown template {template_name!r}"
            )
        # Re-key onto the character id so the browser's ``voice: <character id>``
        # resolves directly. Copied, not shared: ``registry[k].voice_id == k``
        # then holds for every key, and two characters on one template cannot
        # alias a single mutable object.
        registry[character_id] = profile.model_copy(update={"voice_id": character_id})

    return registry
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_voices_templates.py -q --no-header`
Expected: 19 passed（Task 1 遗留 8 条 + 本任务 11 条。计数四度改过，每次都记在这里免得下一个人以为是自己数错：先写「14」（漏算本文件的模板侧用例），改「15」；id 锚点从目录名改为 `id` 字段时补两条（id 定 key、无 voice.yaml 不进注册表）；审查又指出角色侧三条新错误串与重复 id 分支全无覆盖，再补两条（非 mapping 文档、两个 persona 抢同一个 id））

- [ ] **Step 5: 提交**

```bash
git add src/character_memory/voices.py tests/test_voices_templates.py
git commit -m "Resolve character voice references against the template registry"
```

---

### Task 3: `voice_migration.py` — 两条一次性迁移

**Files:**
- Create: `src/character_memory/voice_migration.py`
- Test: `tests/test_voice_migration.py` (新建)

**Interfaces:**
- Consumes: Task 1 的 `discover_templates` / `template_root`；既有 `load_voice_profile` / `discover_voice_profiles`（legacy 读者）
- Produces:
  - `MigrationReport` (dataclass: `created_templates: list[str]`, `migrated_characters: list[str]`, `skipped: bool`)
  - `migrate_voices(*, personas_root, voices_root, legacy_env: dict[str, str]) -> MigrationReport`

**硬约束（spec §8）**：迁移**必须跑在严格校验之前**——它读的正是新 reader 会拒掉的旧格式。所以本模块用 legacy 读者，且只能由「`voices/` 为空」触发。

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_voice_migration.py`：

```python
"""One-shot migrations onto the template model.

Both migrations run before any strict reader sees the tree: migration B reads
the old self-contained character files, which the new reader rejects by design.
Running validation first would fail on the very files this needs to move.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from character_memory.voice_migration import migrate_voices


def _character(root: Path, character_id: str, document: dict) -> Path:
    persona = root / character_id / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text(f"id: {character_id}\nname: {character_id}\n", encoding="utf-8")
    (persona.parent / "voice.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return persona


def _legacy_character(root: Path, character_id: str) -> Path:
    """Write the old form: audio beside the persona, referenced relatively."""

    persona = root / character_id / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text(f"id: {character_id}\nname: {character_id}\n", encoding="utf-8")
    audio = persona.parent / "voice" / "abcdef0123456789.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFF-old")
    (persona.parent / "voice.yaml").write_text(
        yaml.safe_dump(
            {
                "voice_id": character_id,
                "ref_audio": "voice/abcdef0123456789.wav",
                "ref_text": "你好，这是试听。",
                "created_at": "2026-09-20T11:26:53+00:00",
                "instruct": "可爱萝莉音",
                "model": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return persona


def test_migrates_a_legacy_character_into_a_template_and_a_reference(tmp_path):
    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    _legacy_character(personas, "haru")

    report = migrate_voices(personas_root=personas, voices_root=voices, legacy_env={})

    assert report.migrated_characters == ["haru"]
    assert report.created_templates == ["haru"]

    template = yaml.safe_load((voices / "haru.yaml").read_text(encoding="utf-8"))
    assert template["ref_text"] == "你好，这是试听。"
    assert template["instruct"] == "可爱萝莉音"

    # The WAV is copied, not moved: migration stays reversible.
    assert (voices / "haru" / "abcdef0123456789.wav").read_bytes() == b"RIFF-old"
    assert (personas / "haru" / "voice" / "abcdef0123456789.wav").exists()

    # ...and the character is now a reference.
    reference = yaml.safe_load((personas / "haru" / "voice.yaml").read_text(encoding="utf-8"))
    assert reference == {"template": "haru"}


def test_migrates_the_legacy_env_into_a_template(tmp_path):
    voices = tmp_path / "voices"
    audio = tmp_path / "ref.wav"
    audio.write_bytes(b"RIFF-env")

    report = migrate_voices(
        personas_root=tmp_path / "personas",
        voices_root=voices,
        legacy_env={
            "GSV_TTS_REF_AUDIO": str(audio),
            "GSV_TTS_REF_TEXT": "参考文本",
            "GSV_TTS_VOICE": "murasame",
        },
    )

    assert report.created_templates == ["murasame"]
    template = yaml.safe_load((voices / "murasame.yaml").read_text(encoding="utf-8"))
    assert template["ref_audio"] == str(audio)
    assert template["ref_text"] == "参考文本"
    assert template["gpt_model"] is None      # inherits the shared base model


def test_legacy_env_without_a_ref_text_is_not_migrated(tmp_path):
    """Half-configured env is not migratable; it must not create a broken template."""

    voices = tmp_path / "voices"
    audio = tmp_path / "ref.wav"
    audio.write_bytes(b"RIFF")

    report = migrate_voices(
        personas_root=tmp_path / "personas",
        voices_root=voices,
        legacy_env={"GSV_TTS_REF_AUDIO": str(audio), "GSV_TTS_REF_TEXT": "  "},
    )

    assert report.created_templates == []
    assert not voices.exists()


def test_a_non_empty_voices_directory_skips_both_migrations(tmp_path):
    """Migrations run once. A later run must never overwrite what the user made."""

    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    _legacy_character(personas, "haru")
    voices.mkdir(parents=True)
    (voices / "handmade.yaml").write_text("ref_text: x\n", encoding="utf-8")

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={"GSV_TTS_REF_AUDIO": "a.wav", "GSV_TTS_REF_TEXT": "t"},
    )

    assert report.skipped is True
    assert report.created_templates == []
    assert report.migrated_characters == []


def test_both_migrations_run_together_without_colliding(tmp_path):
    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    _legacy_character(personas, "haru")
    audio = tmp_path / "ref.wav"
    audio.write_bytes(b"RIFF-env")

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={
            "GSV_TTS_REF_AUDIO": str(audio),
            "GSV_TTS_REF_TEXT": "参考文本",
            "GSV_TTS_VOICE": "murasame",
        },
    )

    assert sorted(report.created_templates) == ["haru", "murasame"]
    assert report.migrated_characters == ["haru"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_voice_migration.py -q --no-header`
Expected: FAIL — `ModuleNotFoundError: No module named 'character_memory.voice_migration'`

- [ ] **Step 3: 实现**

新建 `src/character_memory/voice_migration.py`：

```python
"""One-shot migrations onto the template model.

Two independent moves, both triggered the same way and both run at most once:

A. The legacy ``GSV_TTS_REF_AUDIO``/``GSV_TTS_REF_TEXT`` env pair becomes a
   template, so the settings page can drop those two fields.
B. A character that still holds its own reference clip becomes a template plus
   a one-line reference.

**Ordering is a hard constraint.** B reads the old self-contained character
files, which the new strict reader rejects (``extra="forbid"``). Running
validation before migration would fail on the very files this module exists to
move, so callers must migrate first and validate second.

Nothing here deletes anything. The legacy env keys and the old WAVs stay where
they are: the migration only adds files, which keeps it reversible and keeps it
from touching user configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil

import yaml

from character_memory.voices import (
    VoiceProfileError,
    load_voice_profile,
    template_root,
)


@dataclass
class MigrationReport:
    created_templates: list[str] = field(default_factory=list)
    migrated_characters: list[str] = field(default_factory=list)
    skipped: bool = False
    notes: list[str] = field(default_factory=list)


def _write_template(voices_root: Path, name: str, document: dict) -> None:
    """Atomic template write, mirroring ``persona_builder.save_persona``."""

    voices_root.mkdir(parents=True, exist_ok=True)
    target = voices_root / f"{name}.yaml"
    temp = target.with_suffix(".yaml.tmp")
    temp.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    temp.replace(target)


def _copy_audio(source: Path, voices_root: Path, name: str) -> str:
    """Copy a clip into the template's own directory and return the bare filename.

    Copied rather than moved: the persona tree is the user's, and a migration
    that empties it is not reversible.
    """

    target_dir = voices_root / name
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if not target.exists():
        shutil.copy2(source, target)
    return source.name


def _migrate_character_clips(personas_root: Path, voices_root: Path, report: MigrationReport) -> None:
    paths = sorted(personas_root.glob("*/persona.yaml")) if personas_root.exists() else []
    for persona_path in paths:
        character_id = persona_path.parent.name
        try:
            profile = load_voice_profile(persona_path)
        except VoiceProfileError as exc:
            # Already a reference, or unusable. Either way this migration has
            # nothing to move; the strict reader gets to report it later.
            report.notes.append(f"{character_id}: skipped ({exc})")
            continue
        if profile is None:
            continue

        # Read the raw document too. VoiceProfile does not carry the provenance
        # fields, and they cannot be reconstructed -- the VoiceDesign run that
        # produced this clip is gone. The drawer shows them (spec 11), so losing
        # them here would be a silent, permanent loss.
        try:
            raw = yaml.safe_load((persona_path.parent / "voice.yaml").read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}

        audio_source = Path(profile.ref_audio)
        filename = _copy_audio(audio_source, voices_root, profile.voice_id)
        _write_template(
            voices_root,
            profile.voice_id,
            {
                "voice_id": profile.voice_id,
                "ref_audio": f"{profile.voice_id}/{filename}",
                "ref_text": profile.ref_text,
                "gpt_model": profile.gpt_model,
                "sovits_model": profile.sovits_model,
                **{key: raw[key] for key in ("created_at", "instruct", "model") if key in raw},
            },
        )

        reference = persona_path.parent / "voice.yaml"
        temp = reference.with_suffix(".yaml.tmp")
        temp.write_text(
            yaml.safe_dump({"template": profile.voice_id}, sort_keys=False), encoding="utf-8"
        )
        temp.replace(reference)

        report.created_templates.append(profile.voice_id)
        report.migrated_characters.append(character_id)


def _migrate_legacy_env(voices_root: Path, legacy_env: dict[str, str], report: MigrationReport) -> None:
    ref_audio = str(legacy_env.get("GSV_TTS_REF_AUDIO") or "").strip()
    ref_text = str(legacy_env.get("GSV_TTS_REF_TEXT") or "").strip()
    if not ref_audio or not ref_text:
        # Half-configured env is not migratable. Creating a template from it
        # would turn "not configured yet" into "configured but broken".
        return
    if not Path(ref_audio).is_file():
        report.notes.append(f"legacy reference audio not found: {ref_audio}")
        return

    name = str(legacy_env.get("GSV_TTS_VOICE") or "").strip() or "default"
    _write_template(
        voices_root,
        name,
        {
            "voice_id": name,
            "ref_audio": ref_audio,
            "ref_text": ref_text,
            "gpt_model": None,      # inherits the shared base model
            "sovits_model": None,
        },
    )
    report.created_templates.append(name)


def migrate_voices(
    *,
    personas_root: str | Path,
    voices_root: str | Path | None,
    legacy_env: dict[str, str],
) -> MigrationReport:
    """Run both migrations if the template tree is still empty.

    A non-empty ``voices/`` means a previous run already happened (or the user
    built templates by hand); either way the migrations are done and must not
    overwrite anything.
    """

    personas_root = Path(personas_root)
    voices_root = template_root(voices_root)
    report = MigrationReport()

    if voices_root.exists() and any(voices_root.glob("*.yaml")):
        report.skipped = True
        return report

    _migrate_character_clips(personas_root, voices_root, report)
    _migrate_legacy_env(voices_root, legacy_env, report)
    return report
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_voice_migration.py -q --no-header`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add src/character_memory/voice_migration.py tests/test_voice_migration.py
git commit -m "Add one-shot migrations onto the template model"
```

---

### Task 4: sidecar 接入模板注册表

**Files:**
- Modify: `src/character_memory/gsv_tts_experiment.py:93-104` (`_persona_root` 旁加 `_voices_root`)、`:173-203` (`_load_voices` / `_voice_ids`)、`:259-274` (`_asset_status`)
- Modify: `src/character_memory/dev_stack.py:278`（钉 `GSV_TTS_VOICES_ROOT` 绝对路径）
- Test: `tests/test_gsv_voice_templates.py` (新建)

**Interfaces:**
- Consumes: Task 1/2 的 `discover_templates` / `discover_character_voices` / `resolve_voice_registry` / `template_root`；Task 3 的 `migrate_voices`
- Produces:
  - `GsvTtsRuntime(..., voices_root: str | Path | None = None)`（新 kwarg）
  - `GsvTtsRuntime._load_voices() -> dict[str, VoiceProfile]`（语义变更：现在合并两棵树）
  - `GsvTtsRuntime.migrate_if_needed() -> MigrationReport`

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_gsv_voice_templates.py`：

```python
"""The sidecar's view of the template registry."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from character_memory.gsv_tts_experiment import GsvTtsRuntime


def _template(root: Path, name: str) -> None:
    audio = root / name / "clip.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFF")
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {"ref_audio": str(audio), "ref_text": f"{name} 的参考文本"},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _runtime(tmp_path: Path, **kwargs) -> GsvTtsRuntime:
    """A runtime whose base models exist on disk, so only the template varies.

    The models are written here rather than stubbed because ``_asset_status``
    checks ``Path(...).is_file()``; a relative path would resolve against the
    cwd and the check would fail for the wrong reason.
    """

    ckpt = tmp_path / "base.ckpt"
    ckpt.write_bytes(b"ckpt")
    sovits = tmp_path / "base.pth"
    sovits.write_bytes(b"pth")
    return GsvTtsRuntime(
        gpt_model=str(ckpt),
        sovits_model=str(sovits),
        ref_audio="",
        ref_text="",
        tts_factory=lambda **_: None,
        persona_root=tmp_path / "personas",
        voices_root=tmp_path / "voices",
        **kwargs,
    )


def test_registry_merges_templates_and_character_references(tmp_path):
    _template(tmp_path / "voices", "murasame")
    persona = tmp_path / "personas" / "haru" / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text("id: haru\n", encoding="utf-8")
    (persona.parent / "voice.yaml").write_text("template: murasame\n", encoding="utf-8")

    runtime = _runtime(tmp_path)

    assert set(runtime._voices) == {"murasame", "haru"}
    voice, ref_audio, ref_text, _, _ = runtime._resolve_voice("haru")
    assert voice == "haru"
    assert ref_text == "murasame 的参考文本"


def test_voice_ids_lists_templates_so_settings_can_offer_them(tmp_path):
    _template(tmp_path / "voices", "murasame")

    runtime = _runtime(tmp_path, default_voice="murasame")

    assert runtime._voice_ids() == ["murasame"]


def test_asset_status_requires_the_default_template_to_resolve(tmp_path):
    """Readiness moved from 'four legacy env fields' to 'the default template works'."""

    _template(tmp_path / "voices", "murasame")
    runtime = _runtime(tmp_path, default_voice="murasame")

    ready, reason = runtime._asset_status()

    assert ready is True, reason


def test_asset_status_reports_a_missing_default_template(tmp_path):
    runtime = _runtime(tmp_path, default_voice="ghost")

    ready, reason = runtime._asset_status()

    assert ready is False
    assert "ghost" in reason


def test_a_character_with_no_voice_file_degrades_silently(tmp_path):
    """Not configured is fine; the browser sends an id for every character."""

    _template(tmp_path / "voices", "murasame")
    runtime = _runtime(tmp_path, default_voice="murasame")

    voice, _, ref_text, _, _ = runtime._resolve_voice("someone-with-no-voice")

    assert voice == "murasame"
    assert ref_text == "murasame 的参考文本"


def test_the_fallback_never_uses_the_legacy_reference_fields(tmp_path):
    """The sharp version of the test above.

    Once the settings page stops driving ``GSV_TTS_REF_AUDIO``/``_REF_TEXT``
    (Task 5), both attributes are empty strings. The old fallback returned them
    directly, which would hand GSV a blank reference for every character with no
    voice -- a silent, total breakage. Assert they are empty *and* that a real
    reference still comes back, so the test cannot pass by accident.
    """

    _template(tmp_path / "voices", "murasame")
    runtime = _runtime(tmp_path, default_voice="murasame")
    assert runtime.ref_audio == "" and runtime.ref_text == ""

    _, ref_audio, ref_text, _, _ = runtime._resolve_voice("someone-with-no-voice")

    assert ref_audio.endswith("clip.wav")
    assert ref_text == "murasame 的参考文本"


def test_a_character_with_a_dead_reference_raises_at_load(tmp_path):
    """Configured but broken is a config error, not an unconfigured character."""

    persona = tmp_path / "personas" / "haru" / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text("id: haru\n", encoding="utf-8")
    (persona.parent / "voice.yaml").write_text("template: ghost\n", encoding="utf-8")

    from character_memory.voices import VoiceProfileError

    with pytest.raises(VoiceProfileError, match="haru references unknown template 'ghost'"):
        _runtime(tmp_path)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_gsv_voice_templates.py -q --no-header`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'voices_root'`

- [ ] **Step 3: 实现**

**(a)** `gsv_tts_experiment.py` 的 import 区加入：

```python
from character_memory.voice_migration import MigrationReport, migrate_voices
from character_memory.voices import (
    VoiceProfile,
    discover_character_voices,
    discover_templates,
    resolve_voice_registry,
    template_root,
)
```

（`VoiceProfile` 可能已经在 import 里；合并进既有 import 语句，不要重复。）

同时补一个模块 logger——本模块目前没有，而 `migrate_if_needed` 需要它（见 (g)）：

```python
import logging

logger = logging.getLogger("character_memory.gsv_tts_experiment")
```

（仓库惯例，照 `visual_web.py:22` / `avatar_web.py:14` 的写法：logger 名是 `character_memory.<模块名>`。）

**(b)** 在 `_persona_root`（`:93-104`）之后加一个同形状的函数：

```python
def _voices_root(voices_root: str | Path | None) -> str:
    """Templates live outside the persona tree; same absolute-path rule applies."""

    if voices_root is not None:
        value = str(voices_root).strip()
        if value:
            return value
    return str(template_root(None))
```

**(c)** `__init__` 签名（`:128-146`）加 `voices_root: str | Path | None = None,`，并在 `self.persona_root = _persona_root(persona_root)`（`:163`）之后加：

```python
        self.voices_root = _voices_root(voices_root)
```

**(d)** 替换 `_load_voices`（`:173-185`）为：

```python
    def _load_voices(self) -> dict[str, VoiceProfile]:
        """Merge the template tree and the character reference tree.

        Templates own the reference clips; characters only name one. Both are
        globbed the same way the app discovers personas, and a missing root
        contributes nothing -- voice assets must never stop the sidecar.

        A character that *has* a ``voice.yaml`` but names a template that does
        not exist raises (``resolve_voice_registry``): that is a configuration
        error the user needs to see. A character with no file is simply absent
        and degrades to the default template at request time.
        """
        personas = Path(self.persona_root)
        persona_paths = sorted(personas.glob("*/persona.yaml")) if personas.exists() else []
        return resolve_voice_registry(
            templates=discover_templates(self.voices_root),
            character_voices=discover_character_voices(persona_paths),
        )
```

**(d2)** 替换 `_resolve_voice`（`:205-237`）。**这一步不可省**：现在的第三级兜底返回 `self.ref_audio` / `self.ref_text`（`:222-229`），而这两个字段从 Task 5 起不再被设置页驱动，删完设置字段后它们是**空串**——没配声音的角色会拿到空参考音频，GSV 直接炸。第三级必须改成解析**默认模板**：

```python
    def _resolve_voice(
        self, requested: str | None
    ) -> tuple[str, str, str, str, str]:
        """Map a request's voice name onto the reference audio to clone.

        Returns ``(voice, ref_audio, ref_text, gpt_model, sovits_model)``.

        Three tiers, in order: the name as registered (which covers both a
        template name and a character id, since the registry is merged); the
        default template; and finally the runtime-global reference.

        The first tier must not raise -- the browser sends ``voice: <character
        id>`` for *every* character, so rejecting unknown ids would mute every
        character that has not been given a voice yet. (A character that *has*
        a voice.yaml but a dead reference is a different case and raised at
        load time; see ``resolve_voice_registry``.)

        The returned ``voice`` is the one actually used, so ``X-TTS-Voice`` and
        ``GsvTtsResult.voice`` never report a profile that was not applied.
        """
        name = str(requested or self.default_voice).strip() or self.default_voice
        # The registry is one flat namespace, and a character id is allowed to
        # shadow a template name: ``resolve_voice_registry`` re-keys a
        # character's profile onto its id, so the character wins the key. Reading
        # the fallback out of that same map is deliberate -- the specific answer
        # beats the general one -- and the consequence is that a character
        # *named after* the default template becomes the fallback for every
        # unknown name. That is accepted, not a bug to repair here with a second,
        # template-only lookup: two lookup paths would let ``X-TTS-Voice`` report
        # a profile the engine did not use.
        profile = self._voices.get(name) or self._voices.get(self.default_voice)
        if profile is None:
            # Even the default template is missing. Still must not raise: this
            # is on the request path for every character, and ``_asset_status``
            # already reports the sidecar as not ready, so it will not be asked
            # to synthesize. ``self.ref_audio``/``self.ref_text`` survive only
            # for this corner and for the migration to read.
            return (
                self.default_voice,
                self.ref_audio,
                self.ref_text,
                self.gpt_model,
                self.sovits_model,
            )
        # A profile only overrides the models it pins; unset means "inherit".
        return (
            profile.voice_id,
            profile.ref_audio,
            profile.ref_text,
            profile.gpt_model or self.gpt_model,
            profile.sovits_model or self.sovits_model,
        )
```

**(e)** `_voice_ids`（`:198-203`）**不变**——它列的就是 `self._voices`，而模板现在已在其中。这正是 spec §6.2 说的「Settings Center 自动出现模板下拉」。

**(f)** 替换 `_asset_status`（`:259-274`）为：

```python
    def _asset_status(self) -> tuple[bool, str | None]:
        """Ready when the shared base models and the default template both resolve.

        The two model paths stay required: every template inherits them unless it
        pins its own. The reference clip moved into the template, so the check
        follows it there -- readiness is now "the default template loads", which
        subsumes the old four-field check.
        """
        required = {
            "GSV_TTS_GPT_MODEL": self.gpt_model,
            "GSV_TTS_SOVITS_MODEL": self.sovits_model,
        }
        missing_config = [name for name, value in required.items() if not value]
        if missing_config:
            return False, f"Missing GSV configuration: {', '.join(missing_config)}"

        missing_files = [value for value in required.values() if value and not Path(value).is_file()]
        if missing_files:
            return False, f"GSV asset not found: {', '.join(missing_files)}"

        profile = self._voices.get(self.default_voice)
        if profile is None:
            return False, (
                f"Default template {self.default_voice!r} is not defined; create one in the "
                "TTS Lab voice design page or pick an existing template in Settings Center."
            )
        if not Path(profile.ref_audio).is_file():
            return False, f"Default template {self.default_voice!r} ref_audio not found: {profile.ref_audio}"
        return True, None
```

**(g)** 加迁移入口（放在 `reload_voices` 之前）：

```python
    def migrate_if_needed(self) -> MigrationReport:
        """Move legacy voices onto the template model, at most once.

        MUST run before the first ``_load_voices``: migration B reads the old
        self-contained character files, which the strict readers reject. Doing
        this afterwards would mean validating first and failing on the files
        this is meant to move.
        """
        with self._lock:
            report = migrate_voices(
                personas_root=self.persona_root,
                voices_root=self.voices_root,
                legacy_env={
                    "GSV_TTS_REF_AUDIO": self.ref_audio,
                    "GSV_TTS_REF_TEXT": self.ref_text,
                    "GSV_TTS_VOICE": self.default_voice,
                },
            )
            if not report.skipped:
                # ``MigrationReport.notes`` is the only record of what the
                # migration declined to touch, and it is exactly what an
                # operator needs when a character comes out voiceless after an
                # upgrade. Without this the field is written and never read.
                for note in report.notes:
                    logger.warning("GSV voice migration: %s", note)
                if report.created_templates:
                    logger.info(
                        "GSV voice migration created %d template(s) and migrated %d "
                        "character(s): %s",
                        len(report.created_templates),
                        len(report.migrated_characters),
                        ", ".join(report.created_templates),
                    )
                self._voices = self._load_voices()
            return report
```

**(h)** 在 `__init__` 里，把 `self._voices = ...` 那一段（`:166-168`）改成先迁移：

```python
        # Migration first: it reads the pre-template layout, which the strict
        # readers below reject. Skipped entirely when a registry was injected,
        # since an injected registry is authoritative and tests must not touch disk.
        if voices is None:
            self.migrate_if_needed()
        self._voices: dict[str, VoiceProfile] = (
            dict(voices) if voices is not None else self._load_voices()
        )
```

- [ ] **Step 3b: 在 `dev_stack` 里钉住 voices root**

Task 1 的审查发现计划漏了这一步：全局约束要求 `GSV_TTS_VOICES_ROOT` 与 `GSV_TTS_PERSONA_ROOT` 一样由**启动脚本与 `dev_stack` 两处**钉绝对路径，但只有 Task 12 钉了脚本，无人钉 `dev_stack`。而 `uv run character-stack` 正是日常启动路径——缺这一步，sidecar 拿到的就是相对的 `Path("voices")`，随 cwd 解析，正是那条约束警告的失败方式（且是静默的：模板全查不到，角色全部落到默认声线）。

`dev_stack.py:278` 是既有的同级写法：

```python
        gsv_env.setdefault("GSV_TTS_PERSONA_ROOT", str(ROOT / "personas"))
```

紧挨它加一行：

```python
        # Same reason as the persona root: a relative root resolves against the
        # sidecar's cwd, which dev_stack does not control.
        gsv_env.setdefault("GSV_TTS_VOICES_ROOT", str(ROOT / "voices"))
```

- [ ] **Step 3c: 先把 `voices/` 加进 `.gitignore`**

这一步原先在 Task 12。挪到这里，因为**本任务是 `voices/` 第一次能在磁盘上真实出现的地方**：`migrate_if_needed` 在 `GsvTtsRuntime.__init__` 里跑，而 Task 4 之后每一步 UI 验证（设置页、drawer、TTS Lab）都要起 stack——每次都会真的建出 `voices/` 并复制 WAV 进去。全局约束要求 `voices/` 整体 gitignore，而一条全局约束应当在违反它的可能性出现**之前**就位，不是等到最后。

在 `.gitignore` 的 `personas/character-*/` 规则附近加：

```
# Voice templates own the reference clips; they are local assets, not source.
# personas/<id>/voice.yaml stays tracked -- that is configuration, not audio.
voices/
```

**验证忽略真的生效时别被骗**：`git check-ignore -v voices/` 对一个「带尾斜杠且尚不存在」的路径会返回 rc=0 并印出一个空 pattern 行，看起来像「已忽略」。要问具体文件：`git check-ignore -v voices/murasame.yaml` —— 规则生效时它才给出真正的答案（rc=0 且印出 `voices/` 这条 pattern；未生效则 rc=1）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_gsv_voice_templates.py tests/test_gsv_tts_experiment.py tests/test_voices.py -q --no-header`
Expected: 新增 7 passed；既有测试若因 `_asset_status` 语义变更而失败，**不要改断言去迁就**——先读失败原因，确认是「测试在断言旧的 4 字段就绪条件」还是「实现有 bug」，在 commit message 里写清是前者。

- [ ] **Step 5: 提交**

```bash
git add .gitignore src/character_memory/gsv_tts_experiment.py src/character_memory/dev_stack.py tests/test_gsv_voice_templates.py
git commit -m "Load templates and character references into the GSV registry"
```

---

### Task 5: 设置页 5 字段 → 3 字段

**Files:**
- Modify: `src/character_memory/settings_store.py:40-46` (`GSV_RUNTIME_FIELDS`)、`:117-157` (GSV 段 schema)、`:498`
- Modify: `src/character_memory/settings_server.py:105-120` (`_gsv_payload`)
- Test: `tests/test_settings_gsv_templates.py` (新建)

**Interfaces:**
- Consumes: 无
- Produces: `GSV_RUNTIME_FIELDS` 只剩 3 个名字；`_gsv_payload` 不再发出 `ref_audio`/`ref_text` 键

**这一条是 spec §6.1.1 的静默失败点**：`configure` 对 `ref_audio`/`ref_text` **没有**空串兜底（`gsv_tts_experiment.py:459-473`），所以发 `""` 会把运行期 ref 抹空，症状是「删完设置字段后 GSV 反而更坏」。必须**省略键**。

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_settings_gsv_templates.py`：

```python
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
            # Deliberately present in the input, as they would be in a stale
            # .env that still carries the legacy keys.
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_settings_gsv_templates.py -q --no-header`
Expected: FAIL — `test_reference_fields_are_gone_from_the_settings_schema` 断言 5 个名字，实际 5 个但集合不等（含 `GSV_TTS_REF_AUDIO`/`GSV_TTS_REF_TEXT`）；`ref_audio` 仍在 payload 里。

- [ ] **Step 3: 实现**

**(a)** `settings_store.py:40-46` 改为：

```python
GSV_RUNTIME_FIELDS = {
    # The two shared base models plus the default template name. The reference
    # clip and its transcript moved into templates (voices/<name>.yaml), so the
    # per-field pair that used to live here is gone.
    "GSV_TTS_GPT_MODEL",
    "GSV_TTS_SOVITS_MODEL",
    "GSV_TTS_VOICE",
}
```

**(b)** 删掉 GSV 段里的 `GSV_TTS_REF_AUDIO`（`:127-132`）与 `GSV_TTS_REF_TEXT`（`:134-139`）两个 field 定义，并把 `GSV_TTS_VOICE` 的 `type` 从 `"text"` 改成 `"select"`——选项由 Task 6 在运行期注入，与既有的 `tts_voice` 字段同构。

再把段的 `description` 改为：

```python
        "description": "GSV 本地资产配置，持久化到项目 .env，不写入 config.yaml。两个底模 + 默认模板名；参考音频与参考文本由模板（voices/<名>.yaml）提供，在 TTS Lab 的声音合成页创建。保存后热配置 sidecar，无需重启整个 stack。",
```

并把 `GSV_TTS_VOICE` 的 label 改为 `"Default Template"`，`placeholder` 改为 `"murasame"`（语义已变）。

**(c)** `settings_store.py:498` 的 fallback 行保持 `"murasame" if name == "GSV_TTS_VOICE" else ""` 不变。

**(d)** `settings_server.py:105-120` 的 `_gsv_payload` 改为模块级函数（便于直接单测）并省略两个键：

```python
def build_gsv_payload(merged: dict[str, Any]) -> dict[str, Any]:
    """Map settings onto the sidecar's ``configure`` body.

    ``ref_audio``/``ref_text`` are deliberately *absent* rather than empty.
    ``configure`` treats ``None`` as "leave alone" and ``""`` as "overwrite", and
    those two attributes have no empty-string fallback (unlike ``default_voice``
    and friends). Sending ``""`` would blank the live reference -- which showed
    up as "GSV got worse after I removed the settings fields", not as an error.
    """

    # The dict below is the original body of _gsv_payload (settings_server.py:109-118)
    # with exactly two lines removed -- the ref_audio and ref_text entries.
    return {
        "gpt_model": str(merged.get("GSV_TTS_GPT_MODEL") or "").strip(),
        "sovits_model": str(merged.get("GSV_TTS_SOVITS_MODEL") or "").strip(),
        "voice": str(merged.get("GSV_TTS_VOICE") or "murasame").strip() or "murasame",
        "device": (str(merged.get("tts_device") or "cuda").strip() or "cuda") if str(merged.get("tts_provider") or "").strip().lower() == "gsv" else "cuda",
    }
```

并在 `create_settings_app` 内把 `_gsv_payload` 改成薄包装（保留 `preload` 处理）：

```python
    def _gsv_payload(values: dict[str, Any] | None = None, *, preload: bool | None = None) -> dict[str, Any]:
        merged = dict(settings_store.snapshot()["values"])
        if values:
            merged.update(values)
        payload = build_gsv_payload(merged)
        if preload is not None:
            payload["preload"] = bool(preload)
        return payload
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_settings_gsv_templates.py tests/test_settings_center.py -q --no-header`
Expected: 新文件 4 passed。`test_settings_center.py` 若断言旧的 5 字段，**先判断它是在测「字段存在」还是「字段能存进去」**：前者要改断言（字段确实删了），后者若涉及 `GSV_TTS_REF_*` 则要删掉那个用例。改动写进 commit message。

- [ ] **Step 5: 提交**

```bash
git add src/character_memory/settings_store.py src/character_memory/settings_server.py tests/test_settings_gsv_templates.py
git commit -m "Drop the GSV reference fields now that templates own them"
```

---

### Task 6: 设置页 GSV 段拿到动态模板下拉

**Files:**
- Modify: `src/character_memory/settings_server.py:139-196` (`settings_snapshot`)
- Test: `tests/test_settings_gsv_templates.py` (追加)

**Interfaces:**
- Consumes: Task 5 的字段集；既有 `tts_inventory()`
- Produces: `GSV_TTS_VOICE` 字段在 `settings_snapshot()["schema"]` 里带 `options`

**模式是既有的**（spec §6.2）：`settings_snapshot` 已经 deep-copy schema 并按 section id + field name 注入 `options`，并已处理「当前值不在列表」与「未就绪则 disabled」。**照搬，不发明新机制。**

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_settings_gsv_templates.py`：

`tts_inventory` 是 `create_settings_app` 内的**嵌套函数**（`settings_server.py:55`），patch 不到模块属性。它读的是注入进来的 `runtime_http_client`（`:53`）打的 `/v1/providers/<id>`，所以用假客户端喂数据——这也顺带让测试走的是真实的注入路径，而不是绕过它。

```python
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


def test_the_template_field_is_disabled_while_gsv_is_not_ready(tmp_path):
    """A dead sidecar cannot apply a choice, so do not pretend it can."""

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
    assert first["disabled"] is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_settings_gsv_templates.py::test_gsv_section_offers_the_registry_as_options -q --no-header`
Expected: FAIL — `KeyError: 'options'`（GSV 段是静态 text 字段，没有 options）

- [ ] **Step 3: 实现**

在 `settings_server.py` 的 `settings_snapshot` 里，紧接既有 `if voice_section is not None:` 块**之后**、`snapshot["schema"] = schema` **之前**插入：

```python
        # Same injection the voice section already does, applied to the GSV
        # section: the template list is runtime state, the schema is static, so
        # the options are grafted on per request. Scope differs from tts_voice --
        # this field must name a *template*, not any name a provider resolves.
        gsv_section = next((section for section in schema if section.get("id") == "gsv-runtime"), None)
        if gsv_section is not None:
            template_field = next(
                (field for field in gsv_section.get("fields", []) if field.get("name") == "GSV_TTS_VOICE"),
                None,
            )
            if template_field is not None:
                # Deliberately the gsv provider, not the locally-bound ``selected``
                # one: this field must name a GSV template by definition, so it
                # must not follow whichever provider the chat is currently using.
                gsv_provider = next((item for item in providers if item["id"] == "gsv"), None)
                templates = list((gsv_provider or {}).get("voices") or [])
                gsv_ready = bool(gsv_provider and gsv_provider.get("ready"))
                current_template = str(snapshot["values"].get("GSV_TTS_VOICE") or "").strip()
                template_field["options"] = [
                    {"value": name, "label": name, "disabled": not gsv_ready}
                    for name in templates
                ]
                if current_template and current_template not in templates:
                    template_field["options"].insert(
                        0,
                        {
                            "value": current_template,
                            "label": f"{current_template} (当前配置 · 尚无此模板)",
                            "disabled": True,
                        },
                    )
                if not template_field["options"]:
                    # An empty dropdown gives the user nothing to do; say where
                    # templates come from instead.
                    template_field["options"] = [
                        {"value": "", "label": "尚无模板，请到 TTS Lab 的声音合成页创建一个", "disabled": True}
                    ]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_settings_gsv_templates.py -q --no-header`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add src/character_memory/settings_server.py tests/test_settings_gsv_templates.py
git commit -m "Offer the template registry in the GSV settings section"
```

---

### Task 7: 固化改造 —— 三写入，模板在 `voices/`

**Files:**
- Modify: `src/character_memory/tts_lab.py:978-1047` (`voice_design_freeze`)
- Test: `tests/test_tts_lab_voice_freeze.py` (改既有往返测试)

**Interfaces:**
- Consumes: Task 1 的 `template_root`
- Produces:
  - `voice_design_freeze` 写 `voices/<角色id>/<sha>.wav` → `voices/<角色id>.yaml` → `personas/<id>/voice.yaml`
  - 响应新增 `shared_with: list[str]`（还引用同一模板的**其它**角色）

**顺序不可换**（spec §7.1）：WAV → 模板 → 引用。反过来会造出「角色指向一个没有音频的模板」。崩溃只应留下孤儿。

- [ ] **Step 1: 写失败的测试**

改写 `tests/test_tts_lab_voice_freeze.py` 里断言落点的用例，并**新增**五个。既有那个「写出的 voice.yaml 能被注册表读回来」的往返测试**保留意图但换目标**——现在写者产出的是模板，读者是 `load_template`。

该文件已有 `_write_persona(root, character_id)`、`_persona_root(tmp_path)`、`_build_app(tmp_path)`、`_generate(client, *, text, instruct) -> tuple[bytes, str]`、`_freeze(client, *, character_id, artifact_id)`、`_tree(root)`；`yaml` / `Path` / `TestClient` 也都已 import。直接复用，不要另起 helper。

注意 `template_root(None)` 读 `GSV_TTS_VOICES_ROOT`，所以每个用例都要 monkeypatch 这个环境变量。

```python
def test_freezing_writes_a_template_then_a_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    app, _ = _build_app(tmp_path)
    _write_persona(_persona_root(tmp_path), "momo")
    client = TestClient(app)
    _, artifact_id = _generate(client, text="你好，这是试听。", instruct="可爱萝莉音")

    response = _freeze(client, character_id="momo", artifact_id=artifact_id)

    assert response.status_code == 200
    template = yaml.safe_load((tmp_path / "voices" / "momo.yaml").read_text(encoding="utf-8"))
    # The reference text is the string the audio was synthesized from, so it is
    # exact by construction -- no human transcription, which is the failure mode
    # that makes zero-shot cloning come out wrong.
    assert template["ref_text"] == "你好，这是试听。"
    assert template["instruct"] == "可爱萝莉音"
    # ref_audio is relative to the template file, so resolve it the same way
    # load_template does rather than against the cwd.
    assert (tmp_path / "voices" / template["ref_audio"]).is_file()

    reference = yaml.safe_load(
        (_persona_root(tmp_path) / "momo" / "voice.yaml").read_text(encoding="utf-8")
    )
    assert reference == {"template": "momo"}


def test_freezing_writes_exactly_three_files(tmp_path, monkeypatch):
    """Pin the write set, so a stray or half-written file cannot appear unnoticed."""

    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    app, _ = _build_app(tmp_path)
    _write_persona(_persona_root(tmp_path), "momo")
    client = TestClient(app)
    _, artifact_id = _generate(client, text="你好，这是试听。", instruct="可爱萝莉音")

    _freeze(client, character_id="momo", artifact_id=artifact_id)

    written = _tree(tmp_path)
    clips = {name for name in written if name.startswith("voices/momo/")}

    assert len(clips) == 1 and clips.pop().endswith(".wav")
    assert written - {name for name in written if name.startswith("voices/momo/")} == {
        "voices/momo.yaml",
        "personas/momo/voice.yaml",
    }


def test_the_frozen_template_round_trips_through_the_reader(tmp_path, monkeypatch):
    """Writer to reader, across the module boundary. This drift shipped once."""

    from character_memory.voices import load_template

    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    app, _ = _build_app(tmp_path)
    _write_persona(_persona_root(tmp_path), "momo")
    client = TestClient(app)
    _, artifact_id = _generate(client, text="你好，这是试听。", instruct="可爱萝莉音")
    _freeze(client, character_id="momo", artifact_id=artifact_id)

    profile = load_template(tmp_path / "voices" / "momo.yaml")

    assert profile.voice_id == "momo"
    assert profile.ref_text == "你好，这是试听。"


def test_freezing_twice_overwrites_the_template_but_keeps_both_clips(tmp_path, monkeypatch):
    """The old WAV stays as an orphan on purpose.

    Content addressing is what makes this safe: a design cannot be regenerated,
    so overwriting the clip an older template still pointed at would lose a
    voice permanently. An orphan only costs disk.
    """

    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    app, _ = _build_app(tmp_path)
    _write_persona(_persona_root(tmp_path), "momo")
    client = TestClient(app)

    _, first = _generate(client, text="第一次", instruct="可爱萝莉音")
    _freeze(client, character_id="momo", artifact_id=first)
    _, second = _generate(client, text="第二次", instruct="可爱萝莉音")
    _freeze(client, character_id="momo", artifact_id=second)

    template = yaml.safe_load((tmp_path / "voices" / "momo.yaml").read_text(encoding="utf-8"))
    assert template["ref_text"] == "第二次"
    assert len(list((tmp_path / "voices" / "momo").glob("*.wav"))) == 2


def test_freezing_reports_other_characters_sharing_the_template(tmp_path, monkeypatch):
    """Spec 7.1: overwriting changes their voice too, so the caller must be told."""

    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    app, _ = _build_app(tmp_path)
    personas = _persona_root(tmp_path)
    _write_persona(personas, "momo")
    _write_persona(personas, "haru")
    client = TestClient(app)

    _, first = _generate(client, text="你好，这是试听。", instruct="可爱萝莉音")
    _freeze(client, character_id="momo", artifact_id=first)
    # haru now points at momo's template, so re-freezing momo affects haru too.
    (personas / "haru" / "voice.yaml").write_text("template: momo\n", encoding="utf-8")

    _, second = _generate(client, text="第二次", instruct="可爱萝莉音")
    response = _freeze(client, character_id="momo", artifact_id=second)

    assert response.json()["shared_with"] == ["haru"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_tts_lab_voice_freeze.py -q --no-header`
Expected: FAIL — 模板文件不存在 / `KeyError: 'shared_with'`

- [ ] **Step 3: 实现**

替换 `tts_lab.py:978-1047` 的 `voice_design_freeze` 主体：

```python
    @app.post("/v1/voice-design/freeze")
    def voice_design_freeze(req: VoiceDesignFreezeRequest):
        profile = next(
            (item for item in get_character_profiles() if item["id"] == req.character_id),
            None,
        )
        if profile is None:
            raise HTTPException(status_code=404, detail=f"Unknown character: {req.character_id}")

        artifact = voice_artifacts.get(req.artifact_id)
        if artifact is None:
            raise HTTPException(
                status_code=404,
                detail="This auditioned voice is no longer available; generate it again before freezing.",
            )

        character_id = profile["id"]
        persona_dir = Path(profile["persona_path"]).parent
        voices_root = template_root(None)
        template_name = character_id
        # WAV and template first, the reference last. This order means a crash
        # leaves an unreferenced file, never a reference to a template with no
        # audio. The helper writes the first two; the reference is this route's
        # own, because only the route knows which character is being pointed.
        ref_audio = _write_voice_template(voices_root, template_name, artifact)

        # Who else is already using this template? Overwriting changes their
        # voice too, so the caller needs to be able to warn before it happens.
        shared_with = sorted(
            other
            for other in get_character_profiles()
            if other["id"] != character_id
            and _references_template(Path(other["persona_path"]).parent / "voice.yaml", template_name)
        )

        reference_path = persona_dir / "voice.yaml"
        reference_temp = reference_path.with_suffix(".yaml.tmp")
        reference_temp.write_text(
            yaml.safe_dump({"template": template_name}, sort_keys=False), encoding="utf-8"
        )
        reference_temp.replace(reference_path)

        activated, reason = _reload_voices()

        return {
            "ok": True,
            "character_id": character_id,
            "voice_id": template_name,
            "template": template_name,
            "ref_audio": ref_audio,
            "ref_text": artifact.text,
            "shared_with": shared_with,
            "activated": activated,
            "reason": reason,
        }
```

在 `tts_lab.py` 模块级加三个 helper（放在 `VoiceDesignArtifactStore` 附近）。**Task 8 与 Task 9 复用它们**——写入块与 reload 块各只有一份实现，改一处不会漏另一处：

```python
def _write_voice_template(voices_root: Path, name: str, artifact) -> str:
    """Write the clip and the template document; return the relative ref_audio.

    Shared by the freeze route and the save-as-template route. They differ only
    in how ``name`` is chosen and whether a reference is written afterwards --
    everything about *how a template is persisted* is here, so the two writers
    cannot drift into emitting two shapes.

    Content-addressed: designs cannot be regenerated, so re-writing must never
    overwrite the clip an older template still points at. An orphaned WAV costs
    disk; a clobbered one loses a voice permanently.
    """

    template_dir = voices_root / name
    template_dir.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256(artifact.audio).hexdigest()[:16]
    audio_path = template_dir / f"{digest}.wav"
    if not audio_path.exists():
        audio_temp = audio_path.with_suffix(".wav.tmp")
        audio_temp.write_bytes(artifact.audio)
        audio_temp.replace(audio_path)

    # Relative to the template file, which is how migration B writes it and how
    # ``load_template`` resolves it. An absolute path would work too, but a
    # relative path is what survives moving the tree.
    ref_audio = f"{name}/{audio_path.name}"

    template_path = voices_root / f"{name}.yaml"
    template_temp = template_path.with_suffix(".yaml.tmp")
    template_temp.write_text(
        yaml.safe_dump(
            {
                "voice_id": name,
                "ref_audio": ref_audio,
                "ref_text": artifact.text,
                "gpt_model": None,
                "sovits_model": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "instruct": artifact.instruct,
                "model": artifact.model,
            },
            allow_unicode=True,
            sort_keys=False,
            width=120,
        ),
        encoding="utf-8",
    )
    template_temp.replace(template_path)
    return ref_audio


def _reload_voices() -> tuple[bool, str | None]:
    """Ask GSV to re-read its registry. Never fatal.

    The files are already on disk and GSV loads them at next start, so a dead
    sidecar degrades to "takes effect later" rather than losing the freeze.
    """

    try:
        voice_reloader_tool.reload()
        return True, None
    except Exception as exc:
        logger.warning("GSV voice reload failed: %s", exc)
        return False, str(exc) or exc.__class__.__name__


def _references_template(voice_path: Path, template_name: str) -> bool:
    """Best-effort read: a broken sibling must not break someone else's freeze."""

    try:
        document = yaml.safe_load(voice_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(document, dict) and str(document.get("template") or "").strip() == template_name
```

并确认 `tts_lab.py` 顶部 import 了 `from character_memory.voices import template_root`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_tts_lab_voice_freeze.py -q --no-header`
Expected: 全 passed（含 5 条新增）

- [ ] **Step 5: 提交**

```bash
git add src/character_memory/tts_lab.py tests/test_tts_lab_voice_freeze.py
git commit -m "Freeze a character voice into a template plus a reference"
```

---

### Task 8: 保存为模板 + 模板名校验

**Files:**
- Modify: `src/character_memory/tts_lab.py`（新增路由，放在 freeze 之后）
- Test: `tests/test_tts_lab_voice_templates.py` (新建)

**Interfaces:**
- Consumes: Task 7 的 artifact store 与 `voice_reloader_tool`
- Produces:
  - `validate_template_name(name: str) -> str`（模块级，纯函数，返回 strip 后的名字或抛 `ValueError`）
  - `POST /v1/voice-design/save-template` 请求体 `{artifact_id, name}`

**校验只对这条自由命名路径有效**（spec §7.3）：固化路径的名字来自 `character_id`，安全性由 `discover_character_profiles()` 白名单保证。

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_tts_lab_voice_templates.py`：

```python
"""Free-naming a template, and the string rules that keep it a filename."""

from __future__ import annotations

import pytest

from character_memory.tts_lab import validate_template_name


@pytest.mark.parametrize("name", ["murasame", "haru-2", "中文名", "a_b"])
def test_accepts_reasonable_names(name):
    assert validate_template_name(f"  {name}  ") == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "a/b",
        "a\\b",
        "..",
        "../etc/passwd",
        "/abs/path",
        "C:/windows",
        "con",
        "PRN",
        "com1",
        "lpt9",
        "trailing.",
        "trailing ",
    ],
)
def test_rejects_names_that_would_break_the_write(name):
    """Every rule maps to a real failure: these become voices/<name>.yaml and a directory."""

    with pytest.raises(ValueError):
        validate_template_name(name)


def test_rejects_an_overlong_name():
    with pytest.raises(ValueError, match="too long"):
        validate_template_name("x" * 65)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_tts_lab_voice_templates.py -q --no-header`
Expected: FAIL — `ImportError: cannot import name 'validate_template_name'`

- [ ] **Step 3: 实现**

在 `tts_lab.py` 模块级加入（放在 `_references_template` 旁）：

```python
_TEMPLATE_NAME_MAX = 64
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_template_name(raw: str) -> str:
    """Validate a user-chosen template name.

    Unlike ``character_id`` -- whose safety comes from a whitelist lookup -- a
    template name is new, so it needs explicit rules. Each one below maps to a
    real failure: the name becomes ``voices/<name>.yaml`` and ``voices/<name>/``,
    so a separator escapes the tree and a Windows reserved word fails the write
    in a way that is hard to read.
    """

    name = str(raw or "").strip()
    if not name:
        raise ValueError("模板名不能为空")
    if len(name) > _TEMPLATE_NAME_MAX:
        raise ValueError(f"模板名 too long (max {_TEMPLATE_NAME_MAX})")
    if "/" in name or "\\" in name:
        raise ValueError("模板名不能包含路径分隔符")
    if name in {".", ".."} or ".." in name:
        raise ValueError("模板名不能包含 '..'")
    if Path(name).is_absolute() or (len(name) > 1 and name[1] == ":"):
        raise ValueError("模板名不能是绝对路径")
    if name.upper().split(".")[0] in _WINDOWS_RESERVED:
        raise ValueError(f"{name} 是 Windows 保留名，请换一个")
    if name != name.rstrip(". "):
        raise ValueError("模板名不能以 '.' 或空格结尾")
    return name


def _references_template(voice_path: Path, template_name: str) -> bool:
    ...
```

再加入路由（放在 `voice_design_freeze` 之后）：

```python
    @app.post("/v1/voice-design/save-template")
    def voice_design_save_template(req: VoiceDesignSaveTemplateRequest):
        """Save an auditioned voice as a reusable template, with no character involved."""

        try:
            name = validate_template_name(req.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        artifact = voice_artifacts.get(req.artifact_id)
        if artifact is None:
            raise HTTPException(
                status_code=404,
                detail="This auditioned voice is no longer available; generate it again before saving.",
            )

        voices_root = template_root(None)
        template_path = voices_root / f"{name}.yaml"
        if template_path.exists():
            # A name the user typed colliding is a mistake, not an intended
            # overwrite. Re-freezing a character is the deliberate overwrite path.
            raise HTTPException(
                status_code=409,
                detail=f"模板 {name} 已存在；换个名字，或直接固化到角色以覆盖。",
            )

        # Same writer the freeze route uses: one place decides how a template is
        # persisted, so the two paths cannot drift into two shapes.
        _write_voice_template(voices_root, name, artifact)

        activated, reason = _reload_voices()

        return {"ok": True, "template": name, "ref_text": artifact.text, "activated": activated, "reason": reason}
```

请求模型放在 `VoiceDesignFreezeRequest` 旁：

```python
class VoiceDesignSaveTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_tts_lab_voice_templates.py -q --no-header`
Expected: 3 passed（参数化后共 18 个用例）

- [ ] **Step 5: 提交**

```bash
git add src/character_memory/tts_lab.py tests/test_tts_lab_voice_templates.py
git commit -m "Save an auditioned voice as a reusable template"
```

---

### Task 9: 声线读写端点（`voice_web.py`）

**Files:**
- Create: `src/character_memory/voice_web.py`
- Modify: `src/character_memory/server.py`
- Test: `tests/test_voice_web.py` (新建)

**Interfaces:**
- Consumes: Task 2 的 `load_character_voice` / `CHARACTER_VOICE_FILENAME`；Task 1 的 `template_root` / `load_template`；`VoiceProfileError`；既有 `discover_character_profiles()`（`config.py:172`）与 `GsvVoiceReloader`（`tts_lab.py:681`）
- Produces:
  - `voice_snapshot(*, characters: Iterable[dict], voices_root: Path) -> dict`
  - `write_character_voice(*, persona_dir: Path, template: str | None, voices_root: Path) -> None`
  - `attach_voice_routes(app) -> None`，注册 `GET /v1/voice-templates` 与 `POST /v1/characters/{character_id}/voice`

**为什么挂在聊天页的 app 上，而不是 `tts_lab`：** `CM.api`（`app.js:59-61`）是**相对路径** `fetch(path)`，打的是聊天页自己的源。`:9002` 是另一个进程、另一个源，相对路径够不到。仓库里「聊天页对一个角色动手」的既有落点全是 `attach_*_routes(app)` 模块（`avatar_web.py`、`wake_web.py`），本任务沿用。

**为什么把逻辑做成纯函数：** `test_avatar_web_assets.py` 那类测试只做源码字符串匹配，在跨模块边界上完全失明（本仓库吃过这个亏）。把「读快照」与「写引用」做成接收显式入参的纯函数，就能用 `tmp_path` 做**真行为测试**，而不必先搭起一个完整的 `create_api`。

**`voice_snapshot` 收角色列表而不是根目录：** 路由手里本来就有 `discover_character_profiles(settings)` 的结果，再传一个根目录让它自己 glob 一遍，等于把「谁是角色」这件事算两遍（`character_id = data.get("id") or path.parent.name` 说明目录名不是权威）。收列表既去掉重复，也免了跨模块引用 `config._persona_root` 这个私有函数。

**`used_by` 是 spec §11 的硬要求：** 每个模板要标注被哪些角色引用，否则 §7.1 的覆盖会打断别人而不自知。这个数只能服务端算——前端只看得到自己的角色列表。

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_voice_web.py`：

```python
"""The chat page's voice endpoints.

The logic is exercised as plain functions over tmp_path rather than through
HTTP. Every interesting failure -- a dead reference, a miscounted ``used_by``,
an id reaching the filesystem -- lives in the logic, and the routes are a thin
shell over it. The real proof that the wiring works is the manual verification
pass, not a string match on server.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from character_memory.voice_web import voice_snapshot, write_character_voice


def _personas(root: Path, character_id: str, template: str | None, *, raw: str | None = None) -> Path:
    """Create a persona directory and return it."""

    persona = root / character_id / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text(f"id: {character_id}\nname: {character_id}\n", encoding="utf-8")
    if raw is not None:
        (persona.parent / "voice.yaml").write_text(raw, encoding="utf-8")
    elif template is not None:
        (persona.parent / "voice.yaml").write_text(
            yaml.safe_dump({"template": template}, sort_keys=False), encoding="utf-8"
        )
    return persona.parent


def _characters(*dirs: Path) -> list[dict]:
    """The shape ``discover_character_profiles`` returns, with only what we read."""

    return [{"id": d.name, "persona_path": str(d / "persona.yaml")} for d in dirs]


def _template(root: Path, name: str) -> None:
    audio = root / name / "clip.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFF")
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "ref_audio": str(audio),
                "ref_text": f"{name} 的参考文本",
                "created_at": "2026-09-20T11:26:53+00:00",
                "instruct": "可爱萝莉音",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_snapshot_lists_templates_with_their_provenance(tmp_path):
    voices = tmp_path / "voices"
    _template(voices, "murasame")
    haru = _personas(tmp_path / "personas", "haru", "murasame")

    snapshot = voice_snapshot(characters=_characters(haru), voices_root=voices)

    entry = next(item for item in snapshot["templates"] if item["name"] == "murasame")
    assert entry["ref_text"] == "murasame 的参考文本"
    assert entry["instruct"] == "可爱萝莉音"
    assert entry["used_by"] == ["haru"]


def test_snapshot_counts_every_character_that_references_a_template(tmp_path):
    """This is what makes the overwrite warning in the spec possible."""

    voices = tmp_path / "voices"
    _template(voices, "murasame")
    haru = _personas(tmp_path / "personas", "haru", "murasame")
    momo = _personas(tmp_path / "personas", "momo", "murasame")

    snapshot = voice_snapshot(characters=_characters(haru, momo), voices_root=voices)

    entry = next(item for item in snapshot["templates"] if item["name"] == "murasame")
    assert entry["used_by"] == ["haru", "momo"]


def test_snapshot_separates_unset_from_missing(tmp_path):
    """The whole point: never conflate 'not configured' with 'misconfigured'."""

    voices = tmp_path / "voices"
    _template(voices, "murasame")
    haru = _personas(tmp_path / "personas", "haru", None)      # never given a voice
    momo = _personas(tmp_path / "personas", "momo", "ghost")   # points at a template that is gone

    snapshot = voice_snapshot(characters=_characters(haru, momo), voices_root=voices)

    assert snapshot["characters"]["haru"]["status"] == "unset"
    assert snapshot["characters"]["haru"]["error"] is None
    assert snapshot["characters"]["momo"]["status"] == "missing"
    assert "ghost" in snapshot["characters"]["momo"]["error"]


def test_snapshot_reports_a_character_still_in_the_old_shape(tmp_path):
    """Migration B did not run, or someone hand-wrote the pre-template form."""

    voices = tmp_path / "voices"
    _template(voices, "murasame")
    haru = _personas(
        tmp_path / "personas", "haru", None, raw="ref_audio: voice/x.wav\nref_text: 你好\n"
    )

    snapshot = voice_snapshot(characters=_characters(haru), voices_root=voices)

    assert snapshot["characters"]["haru"]["status"] == "error"


def test_a_broken_template_is_reported_without_blanking_the_list(tmp_path):
    """One unusable template must not cost the user the whole picker."""

    voices = tmp_path / "voices"
    _template(voices, "murasame")
    (voices / "broken.yaml").write_text("ref_text: 有文本但没音频\n", encoding="utf-8")

    snapshot = voice_snapshot(characters=[], voices_root=voices)

    assert [item["name"] for item in snapshot["templates"]] == ["murasame"]
    assert any("broken" in note for note in snapshot["errors"])


def test_snapshot_on_empty_input_is_not_an_error(tmp_path):
    snapshot = voice_snapshot(characters=[], voices_root=tmp_path / "nope")

    assert snapshot == {"templates": [], "characters": {}, "errors": []}


def test_writing_a_reference_leaves_only_the_one_line(tmp_path):
    voices = tmp_path / "voices"
    persona_dir = _personas(tmp_path / "personas", "haru", None)
    _template(voices, "murasame")

    write_character_voice(persona_dir=persona_dir, template="murasame", voices_root=voices)

    written = yaml.safe_load((persona_dir / "voice.yaml").read_text(encoding="utf-8"))
    assert written == {"template": "murasame"}
    assert not list(persona_dir.glob("*.tmp"))


def test_writing_replaces_an_existing_reference(tmp_path):
    voices = tmp_path / "voices"
    persona_dir = _personas(tmp_path / "personas", "haru", "murasame")
    _template(voices, "murasame")
    _template(voices, "haru")

    write_character_voice(persona_dir=persona_dir, template="haru", voices_root=voices)

    written = yaml.safe_load((persona_dir / "voice.yaml").read_text(encoding="utf-8"))
    assert written == {"template": "haru"}


def test_clearing_a_reference_deletes_the_file(tmp_path):
    voices = tmp_path / "voices"
    persona_dir = _personas(tmp_path / "personas", "haru", "murasame")
    _template(voices, "murasame")

    write_character_voice(persona_dir=persona_dir, template=None, voices_root=voices)

    assert not (persona_dir / "voice.yaml").exists()


def test_clearing_an_absent_reference_is_not_an_error(tmp_path):
    """The drawer posts None for a character that never had a voice; that is fine."""

    voices = tmp_path / "voices"
    persona_dir = _personas(tmp_path / "personas", "haru", None)
    _template(voices, "murasame")

    write_character_voice(persona_dir=persona_dir, template=None, voices_root=voices)

    assert not (persona_dir / "voice.yaml").exists()


def test_writing_an_unknown_template_is_refused(tmp_path):
    voices = tmp_path / "voices"
    persona_dir = _personas(tmp_path / "personas", "haru", None)

    with pytest.raises(ValueError, match="ghost"):
        write_character_voice(persona_dir=persona_dir, template="ghost", voices_root=voices)


def test_server_attaches_the_voice_routes():
    """A weak wiring check, same shape as test_avatar_web_assets.py.

    It cannot prove the route works -- the manual verification pass does that.
    It catches one thing only: the module was written but never registered.
    """

    server = (
        Path(__file__).resolve().parents[1] / "src" / "character_memory" / "server.py"
    ).read_text(encoding="utf-8")

    assert "attach_voice_routes(app)" in server
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_voice_web.py -q --no-header`
Expected: FAIL — `ModuleNotFoundError: No module named 'character_memory.voice_web'`

- [ ] **Step 3: 实现**

新建 `src/character_memory/voice_web.py`：

```python
"""Voice-line endpoints for the chat page's per-character drawer.

These live on the app that serves the chat page, not on the TTS Lab. The front
end reaches them through ``CM.api``, which is a *relative* fetch and therefore
same-origin; ``:9002`` is a different process and a different origin. The shape
follows ``avatar_web.py``: an ``attach_*_routes(app)`` function over
``app.state.character_memory``.

The logic lives in plain functions taking explicit arguments, so it can be
tested behaviourally over ``tmp_path`` instead of through a full ``create_api``.
The routes are a thin shell over them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import yaml

from character_memory.voices import (
    CHARACTER_VOICE_FILENAME,
    VoiceProfileError,
    load_character_voice,
    load_template,
)


logger = logging.getLogger("character_memory.voice_web")


def voice_snapshot(*, characters: Iterable[dict], voices_root: Path) -> dict:
    """Return the template list and every character's voice status.

    ``characters`` is what ``discover_character_profiles`` returns; taking it as
    an argument rather than globbing a root keeps "who is a character" defined
    in exactly one place.

    ``templates`` carries ``used_by`` because only the server can count global
    references -- the browser sees just its own character list, and without the
    count an overwrite silently changes someone else's voice.

    A template whose file is unusable is reported in ``errors`` rather than
    raised: one bad template must not blank the whole drawer, and this list is
    only a picker -- the sidecar's own loader has the final say.
    """

    templates: list[dict] = []
    errors: list[str] = []
    paths = sorted(voices_root.glob("*.yaml")) if voices_root.exists() else []
    for path in paths:
        try:
            profile = load_template(path)
        except VoiceProfileError as exc:
            errors.append(f"{path.stem}: {exc}")
            continue
        raw: dict = {}
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, yaml.YAMLError):
            pass
        templates.append(
            {
                "name": profile.voice_id,
                "ref_text": profile.ref_text,
                # Provenance, present only on frozen templates. GSV never reads
                # these; the drawer shows them so two voices can be told apart.
                "created_at": raw.get("created_at"),
                "instruct": raw.get("instruct"),
                "used_by": [],
            }
        )

    by_name = {entry["name"]: entry for entry in templates}
    characters: dict[str, dict] = {}
    for item in characters:
        character_id = item["id"]
        persona_path = Path(item["persona_path"])
        try:
            referenced = load_character_voice(persona_path)
            error = None
        except VoiceProfileError as exc:
            # Configured but broken. Reported loudly rather than degraded,
            # because a wrong voice is harder to notice than no voice.
            referenced, error = None, str(exc)
        if error:
            status = "error"
        elif referenced is None:
            status = "unset"
        elif referenced in by_name:
            status = "set"
            by_name[referenced]["used_by"].append(character_id)
        else:
            status = "missing"
            error = f"references unknown template {referenced!r}"
        characters[character_id] = {"template": referenced, "status": status, "error": error}

    return {"templates": templates, "characters": characters, "errors": errors}


def write_character_voice(*, persona_dir: Path, template: str | None, voices_root: Path) -> None:
    """Point a character at a template, or clear the reference.

    ``None`` removes the file so the character falls back to the default
    template -- a different intent from naming one, so it is a distinct value
    rather than an empty string.

    Atomic, following ``persona_builder.save_persona``: a half-written reference
    would be read by the next reload as a corrupt file.
    """

    reference = persona_dir / CHARACTER_VOICE_FILENAME
    if template is None:
        try:
            reference.unlink()
        except FileNotFoundError:
            pass
        return

    name = str(template).strip()
    if not (voices_root / f"{name}.yaml").is_file():
        raise ValueError(f"Unknown template: {name}")

    temp = reference.with_suffix(".yaml.tmp")
    temp.write_text(yaml.safe_dump({"template": name}, sort_keys=False), encoding="utf-8")
    temp.replace(reference)


def attach_voice_routes(app) -> None:
    """Attach the drawer's voice endpoints."""

    from fastapi import HTTPException
    from pydantic import BaseModel, ConfigDict

    from character_memory.config import discover_character_profiles
    from character_memory.tts_lab import GsvVoiceReloader
    from character_memory.voices import template_root

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError(
            "create_api() must expose app.state.character_memory before voice routes are attached"
        )

    settings = access.settings
    reloader = GsvVoiceReloader()

    class CharacterVoiceRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")

        template: str | None = None

    def _persona_dir(character_id: str) -> Path:
        """Resolve through the discovered whitelist; never join the raw id.

        ``character_id`` is ``data.get("id") or path.parent.name`` upstream, so
        the directory name is not authoritative and the client string must not
        reach the filesystem.
        """

        profile = next(
            (item for item in discover_character_profiles(settings) if item["id"] == character_id),
            None,
        )
        if profile is None:
            raise HTTPException(status_code=404, detail=f"Unknown character: {character_id}")
        return Path(profile["persona_path"]).parent

    def _reload() -> tuple[bool, str | None]:
        """Non-fatal: the file is already on disk and GSV picks it up at next start.

        Deliberately local rather than reusing ``tts_lab._reload_voices``: that
        one is private and closes over ``tts_lab``'s module-level reloader, while
        this app owns its own ``GsvVoiceReloader``. Reaching across for six lines
        would couple this module to a neighbour's global to save less than it costs.
        """

        try:
            reloader.reload()
            return True, None
        except Exception as exc:
            logger.warning("GSV voice reload failed: %s", exc)
            return False, str(exc) or exc.__class__.__name__

    @app.get("/v1/voice-templates")
    def voice_templates():
        return voice_snapshot(
            characters=discover_character_profiles(settings),
            voices_root=template_root(None),
        )

    @app.post("/v1/characters/{character_id}/voice")
    def set_character_voice(character_id: str, req: CharacterVoiceRequest):
        persona_dir = _persona_dir(character_id)
        try:
            write_character_voice(
                persona_dir=persona_dir,
                template=req.template,
                voices_root=template_root(None),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        activated, reason = _reload()
        return {
            "ok": True,
            "character_id": character_id,
            "template": req.template,
            "activated": activated,
            "reason": reason,
        }
```

`server.py` 注册（import 区加一行，调用区加一行）：

```python
from character_memory.voice_web import attach_voice_routes
```
```python
attach_voice_routes(app)
```

`attach_voice_routes(app)` 紧挨 `attach_avatar_routes(app)` 放——两者都是每角色路由，并排好读。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_voice_web.py -q --no-header`
Expected: 12 passed

- [ ] **Step 5: 提交**

```bash
git add src/character_memory/voice_web.py src/character_memory/server.py tests/test_voice_web.py
git commit -m "Add voice-line endpoints for the character drawer"
```
---

### Task 10: 聊天页 drawer 的声线面板

**Files:**
- Create: `src/character_memory/web/voices.js`
- Create: `src/character_memory/web/voices.css`
- Modify: `src/character_memory/web/index.html:31`（角色名可点）、`:101` 后加载脚本
- Test: `tests/test_voice_panel_assets.py` (新建)

**Interfaces:**
- Consumes: Task 9 的 `GET /v1/voice-templates`（`templates[].used_by` + `characters[id].{template,status,error}`）与 `POST /v1/characters/{id}/voice`
- Produces: `CM.registerFeature("voice", {open, refresh})`

**入口范式照抄 `avatars.js:210-212`**：那边是点头像开面板，这边点角色名开声线面板。

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_voice_panel_assets.py`：

```python
"""Wiring checks for the character voice panel.

voices.js is a plain browser script with no module system, so there is nothing
to import it into. These assertions pin the things that break silently: the
module is registered, the entry point is wired, and the panel reads the fields
the API actually returns.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_the_panel_is_registered_as_a_feature():
    source = (WEB / "voices.js").read_text(encoding="utf-8")

    assert 'CM.registerFeature("voice"' in source
    assert "function open(characterId = CM.state.characterId)" in source


def test_the_panel_is_loaded_by_the_app_shell():
    html = (WEB / "index.html").read_text(encoding="utf-8")

    assert "/static/voices.js" in html
    assert 'href="/static/voices.css"' in html


def test_the_character_name_opens_the_panel():
    """The avatar panel is opened from the header avatar; this is its sibling."""

    source = (WEB / "voices.js").read_text(encoding="utf-8")

    assert "characterName" in source
    assert "addEventListener" in source


def test_the_panel_reads_the_fields_the_endpoint_returns():
    """The names must match voice_snapshot()'s output, not a guess at it."""

    source = (WEB / "voices.js").read_text(encoding="utf-8")

    assert "/v1/voice-templates" in source
    for field in ("templates", "characters", "status", "error"):
        assert field in source, f"the panel must handle {field}"


def test_the_panel_shows_who_else_uses_a_template():
    """Spec 11: without this, a shared overwrite changes someone else's voice
    with no warning anywhere in the UI."""

    source = (WEB / "voices.js").read_text(encoding="utf-8")

    assert "used_by" in source


def test_the_panel_does_not_share_a_stylesheet_with_the_call_ui():
    """voice.css is the microphone/playback UI; a voice line is a different thing."""

    assert (WEB / "voices.css").is_file()
    assert not (WEB / "voice.css").read_text(encoding="utf-8").count("voice-panel")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_voice_panel_assets.py -q --no-header`
Expected: FAIL — `FileNotFoundError: voices.js`

- [ ] **Step 3: 实现**

新建 `src/character_memory/web/voices.js`：

```javascript
(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before voices.js");

  const STATUS_COPY = {
    set: "已设置",
    unset: "使用默认声线",
    missing: "模板已丢失",
    error: "配置有误",
  };

  // The TTS Lab is a separate origin. There is no base helper for it -- the
  // repo hardcodes cross-origin links the same way index.html links Settings
  // Center -- so a literal with a name is clearer than an inline URL.
  const LAB_BASE = "http://127.0.0.1:9002";

  function profileFor(characterId) {
    return CM.state.characters.find(item => item.id === characterId) || null;
  }

  function optionsHtml(current, names) {
    const known = names.map(
      name => `<option value="${CM.escapeHtml(name)}" ${name === current ? "selected" : ""}>${CM.escapeHtml(name)}</option>`
    );
    if (current && !names.includes(current)) {
      known.unshift(`<option value="${CM.escapeHtml(current)}" selected>${CM.escapeHtml(current)}（已丢失）</option>`);
    }
    return [`<option value="">使用默认声线</option>`, ...known].join("");
  }

  async function loadSnapshot() {
    // One call carries both halves: every template (with who references it) and
    // every character's status. Same-origin through CM.api, like the other
    // per-character calls on this page -- the TTS Lab is a different origin and
    // a relative fetch could not reach it.
    const snapshot = await CM.api("/v1/voice-templates");
    return {
      templates: snapshot.templates || [],
      characters: snapshot.characters || {},
      errors: snapshot.errors || [],
    };
  }

  async function refresh() {
    // Re-read after a write so the picker and the "who else uses this" count are
    // never stale. The sidecar reloads its own registry server-side, so there is
    // nothing to push from here.
    return await loadSnapshot();
  }

  function render(characterId, entry, snapshot) {
    const status = entry?.status || "unset";
    const names = snapshot.templates.map(item => item.name);
    const used = snapshot.templates.find(item => item.name === entry?.template)?.used_by || [];
    const others = used.filter(id => id !== characterId);

    const errorLine = entry?.error
      ? `<p class="voice-panel-error">${CM.escapeHtml(entry.error)}</p>`
      : "";
    // Overwriting a template changes everyone pointing at it, so say so before
    // the user picks one -- this is the drawer half of the freeze warning.
    const sharedLine = others.length
      ? `<p class="voice-panel-shared">另有 ${others.length} 个角色（${CM.escapeHtml(others.join("、"))}）在用这个声音。</p>`
      : "";
    const hint = status === "unset"
      ? `<p class="muted">当前使用默认声线。到声音合成页设计一个，或用下面的下拉选一个已有模板。</p>`
      : "";
    const errorsLine = snapshot.errors.length
      ? `<p class="voice-panel-error">有模板不可用：${CM.escapeHtml(snapshot.errors.join("；"))}</p>`
      : "";

    CM.dom.drawerBody.innerHTML = `
      <section class="voice-panel">
        <div class="voice-panel-status">
          <span class="voice-panel-badge" data-status="${CM.escapeHtml(status)}">${CM.escapeHtml(STATUS_COPY[status] || status)}</span>
          <span class="voice-panel-current">${CM.escapeHtml(entry?.template || "—")}</span>
        </div>
        ${errorLine}
        ${errorsLine}
        <label class="voice-panel-picker">
          <span>选择声线模板</span>
          <select data-voice-template>${optionsHtml(entry?.template, names)}</select>
        </label>
        ${sharedLine}
        ${hint}
        <div class="voice-panel-actions">
          <button type="button" class="primary" data-voice-save>保存</button>
          <a class="voice-panel-link" href="${LAB_BASE}/tts" target="_blank" rel="noopener noreferrer">去声音合成页造一个</a>
        </div>
        <p class="voice-panel-result" data-voice-result></p>
      </section>`;

    const save = CM.dom.drawerBody.querySelector("[data-voice-save]");
    const select = CM.dom.drawerBody.querySelector("[data-voice-template]");
    const result = CM.dom.drawerBody.querySelector("[data-voice-result]");
    save.addEventListener("click", async () => {
      save.disabled = true;
      result.textContent = "保存中…";
      try {
        const chosen = select.value || null;
        await CM.api(`/v1/characters/${encodeURIComponent(characterId)}/voice`, {
          method: "POST",
          body: JSON.stringify({template: chosen}),
        });
        const snapshot = await refresh();
        render(characterId, snapshot.characters[characterId], snapshot);
      } catch (error) {
        result.textContent = `保存失败：${error.message}`;
      } finally {
        save.disabled = false;
      }
    });
  }

  async function open(characterId = CM.state.characterId) {
    if (CM.isGroupConversation()) return;
    const profile = profileFor(characterId);
    CM.openDrawer(
      `${profile?.name || characterId} · 声线`,
      "选一个模板，或到声音合成页设计一个新的"
    );
    CM.dom.drawerBody.innerHTML = "<p>正在加载…</p>";
    try {
      const snapshot = await refresh();
      render(characterId, snapshot.characters[characterId], snapshot);
    } catch (error) {
      CM.dom.drawerBody.innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`;
    }
  }

  function wireEntryPoint() {
    // Sibling of the avatar panel, which is opened from the header avatar.
    if (!CM.dom.characterName || CM.dom.characterName.dataset.voiceWired) return;
    CM.dom.characterName.dataset.voiceWired = "1";
    CM.dom.characterName.classList.add("voice-editable");
    CM.dom.characterName.title = "设置声线";
    CM.dom.characterName.addEventListener("click", () => {
      if (!CM.isGroupConversation()) open().catch(console.error);
    });
  }

  CM.on("conversationChanged", wireEntryPoint);
  CM.on("charactersLoaded", wireEntryPoint);
  wireEntryPoint();

  CM.registerFeature("voice", {open, refresh});
})();
```

新建 `src/character_memory/web/voices.css`：

```css
.voice-editable { cursor: pointer; }
.voice-editable:hover { text-decoration: underline dotted; }

.voice-panel { display: flex; flex-direction: column; gap: 14px; }
.voice-panel-status { display: flex; align-items: center; gap: 8px; }
.voice-panel-badge { padding: 2px 8px; border-radius: 999px; font-size: 12px; border: 1px solid currentColor; }
.voice-panel-badge[data-status="set"] { color: #3f9c5a; }
.voice-panel-badge[data-status="unset"] { color: #888; }
.voice-panel-badge[data-status="missing"],
.voice-panel-badge[data-status="error"] { color: #c0392b; }
.voice-panel-current { font-weight: 600; }
.voice-panel-error { color: #c0392b; margin: 0; }
.voice-panel-shared { color: #b7791f; margin: 0; }
.voice-panel-picker { display: flex; flex-direction: column; gap: 6px; }
.voice-panel-actions { display: flex; align-items: center; gap: 12px; }
.voice-panel-result { margin: 0; color: #888; }
```

`index.html`：在 `:31` 给角色名加可点标记（`voices.js` 会挂 handler，这里只需保证元素存在），并在 `<head>` 加样式、在 `:101` 后加脚本：

```html
  <link rel="stylesheet" href="/static/voices.css">
```
```html
  <script src="/static/voices.js" defer></script>
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_voice_panel_assets.py -q --no-header`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add src/character_memory/web/voices.js src/character_memory/web/voices.css src/character_memory/web/index.html tests/test_voice_panel_assets.py
git commit -m "Add the per-character voice panel"
```

---

### Task 11: TTS Lab 前端的「保存为模板」与覆盖警告

**Files:**
- Modify: `src/character_memory/web/tts_lab.html:55-108`
- Modify: `src/character_memory/web/tts_lab.js`（`freezeVoiceDesign` 附近，`:419+`）
- Test: `tests/test_voice_panel_assets.py`（追加）

**Interfaces:**
- Consumes: Task 8 的 `/v1/voice-design/save-template`；Task 7 响应里的 `shared_with`
- Produces: 无新 API

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_voice_panel_assets.py`：

```python
def test_the_lab_offers_saving_a_template_without_a_character():
    source = (WEB / "tts_lab.js").read_text(encoding="utf-8")

    assert "save-template" in source
    assert "voiceDesignTemplateName" in source


def test_the_lab_warns_before_overwriting_a_shared_template():
    """Overwriting changes other characters' voices; the user must see that first."""

    source = (WEB / "tts_lab.js").read_text(encoding="utf-8")

    assert "shared_with" in source
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_voice_panel_assets.py -q --no-header`
Expected: FAIL — 断言不成立

- [ ] **Step 3: 实现**

`tts_lab.html` 的 Voice Design 面板里，在既有「固化到角色」按钮旁加：

```html
        <label class="voice-design-field">
          <span>模板名（保存为模板时必填，不需要角色）</span>
          <input type="text" id="voiceDesignTemplateName" maxlength="64" placeholder="例如 murasame">
        </label>
        <button type="button" id="saveVoiceDesignTemplate">保存为模板</button>
        <p class="voice-design-status" id="voiceDesignTemplateStatus"></p>
```

`tts_lab.js` 加入（放在 `freezeVoiceDesign` 之后，复用同一套 `voiceDesignSnapshotMatches()` 守卫）：

```javascript
  async function saveVoiceDesignTemplate() {
    const button = $("saveVoiceDesignTemplate");
    const status = $("voiceDesignTemplateStatus");
    const name = $("voiceDesignTemplateName").value.trim();
    if (!state.voiceDesignArtifact || !voiceDesignSnapshotMatches()) {
      status.textContent = "测试文本或 Instruct 已修改，请重新生成后再保存。";
      return;
    }
    if (!name) {
      status.textContent = "请先填模板名。";
      return;
    }
    button.disabled = true;
    status.textContent = "保存中…";
    try {
      const response = await fetch("/v1/voice-design/save-template", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({artifact_id: state.voiceDesignArtifact, name}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      status.textContent = `已保存为模板 ${data.template}${data.activated ? "" : "（未能热加载，GSV 未运行？）"}`;
      await loadCharacters();
    } catch (error) {
      status.textContent = `保存失败：${error.message}`;
    } finally {
      button.disabled = false;
    }
  }
```

并在 `freezeVoiceDesign` 的成功分支里，用 `shared_with` 提示：

```javascript
      // Overwriting a template changes every character that references it, so
      // say who else is affected instead of letting it happen silently.
      const shared = data.shared_with || [];
      const shareNote = shared.length
        ? `已覆盖模板，另有 ${shared.length} 个角色（${shared.join("、")}）共用这个声音，它们也会一起改变。`
        : "";
      $("voiceDesignFreezeStatus").textContent = `已固化到 ${data.character_id}。${shareNote}`;
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_voice_panel_assets.py -q --no-header`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
git add src/character_memory/web/tts_lab.js src/character_memory/web/tts_lab.html tests/test_voice_panel_assets.py
git commit -m "Offer saving a template and warn before overwriting a shared one"
```

---

### Task 12: 文档与 `.gitignore`

**Files:**
- Modify: `docs/current/GSV_TTS_EXPERIMENT.md`
- Modify: `src/character_memory/voices.py`（模块 docstring）
- Modify: `scripts/start-gsv-tts.sh`（钉 `GSV_TTS_VOICES_ROOT` 绝对路径）

> `.gitignore` 的 `voices/` 规则已在 Task 4 Step 3c 落地（那里是 `voices/` 第一次能在磁盘上真实出现的地方）。本任务不再重复。

- [ ] **Step 1: 启动脚本钉绝对路径**

`scripts/start-gsv-tts.sh` 里，紧挨既有的 `GSV_TTS_PERSONA_ROOT` 导出处加：

```bash
# Same reason as GSV_TTS_PERSONA_ROOT: a relative root resolves against the
# cwd, which the sidecar does not control.
export GSV_TTS_VOICES_ROOT="${GSV_TTS_VOICES_ROOT:-$(cd "$(dirname "$0")/.." && pwd)/voices}"
```

- [ ] **Step 2: 文档**

**(a)** `src/character_memory/voices.py` 的模块 docstring 现在只描述了 per-character 的世界（「`voice.yaml` sits next to a character's `persona.yaml`」）。Task 1 让这个模块变成两棵树——模板与角色引用——但没人认领这段 docstring，所以它现在是错的。改写为：模块同时承载模板（`voices/<名>.yaml`，持有参考音频与文本，是声音的唯一载体）与角色引用（`personas/<id>/voice.yaml`，只有一行 `template: <名>`）；保留「缺失静默降级、非法大声报错」这条原则，并说明两棵树在加载期合并成一个注册表。**另需写明一件不显然的事**：本模块为了给角色引用定 key，会读一次 `persona.yaml` 的 `id` 字段（`_character_id`，逻辑与 `config.py:180` 同一行）——即这个模块读的不只是 voice 文档。不写下来，下一个人会觉得越界。

**(b)** `docs/current/GSV_TTS_EXPERIMENT.md` 加一节说明：
- 音色现在是模板（`voices/<名>.yaml`），角色只引用（`personas/<id>/voice.yaml: template: <名>`）
- 设置页 GSV 段从 5 个字段减到 3 个
- legacy env 与旧的每角色参考音频由一次性迁移搬到 `voices/`，`.env` 里的旧键不再生效、可删
- 迁移**必须跑在严格校验之前**，以及为什么
- **`tts_voice` 改不了默认音色**（spec §6.3）：它是跨 provider 字段（kokoro 的音色名、sherpa 的 speaker id、gsv 的音色名），本次不动。在 gsv 路径上，浏览器总会发 `voice: <角色id>`，sidecar 解析失败后落到**默认模板**——所以决定「没配声音的角色用什么」的是 `GSV_TTS_VOICE`，不是 `tts_voice`。写进文档，免得将来有人改错了地方还查不出原因。

- [ ] **Step 3: 全量测试**

Run: `uv run pytest -q --no-header`
Expected: **0 failed**，且 skipped 只有 5 条（`RUN_PLAYWRIGHT=1` 门控的那几条）。

别拿一个固定数字当预期：本任务是最后一个，前面 Task 3-11 各自都在加用例，任何写死的总数到这一步都是错的（本计划早先写的「基线 480」在 Task 2 结束时实测已是 486 passed / 5 skipped）。要盯的是**没有 failed、skipped 不多不少**。

- [ ] **Step 4: 提交**

```bash
git add scripts/start-gsv-tts.sh docs/current/GSV_TTS_EXPERIMENT.md src/character_memory/voices.py
git commit -m "Document the template model and pin the voices root"
```

---

## 真机验证（合并前必做，spec §13.1）

单元测试覆盖不到迁移在**当前这份 `.env`** 上的行为，而迁移是可用性的单点：不触发的话 GSV 会从「能用」直接变成 Settings Center 里不可选。

- [ ] **备份**：`cp -r personas personas.bak && cp .env .env.bak && cp config.yaml config.yaml.bak`
- [ ] **迁移前**记录基线：`curl -s http://127.0.0.1:9014/health | python -c "import json,sys; d=json.load(sys.stdin); print(d['ready'], d['voices'])"`
- [ ] 起 stack，让它跑完迁移：`uv run character-stack --no-browser`
- [ ] **迁移后**确认 `ready: true`，且 `voices` 里出现了 `murasame` 与 `character-0b287243`
- [ ] 确认 `voices/character-0b287243.yaml` 的 `ref_text` 与迁移前 `personas/character-0b287243/voice.yaml` 的 `ref_text` **逐字相同**
- [ ] 确认 `personas/character-0b287243/voice/3d3abce967477c8d.wav` **仍在**（迁移复制不删）
- [ ] 打开 `:8003/settings`，确认 GSV 段只剩 3 个字段、默认模板下拉里有两个模板
- [ ] 打开 `:8000`，点角色名，确认声线面板显示「已设置 character-0b287243」
- [ ] 在面板里切到 `murasame`，发一条聊天消息，确认**声音变了**（这是端到端唯一真正的证明）
- [ ] 切回原模板，确认声音变回来
- [ ] `:9002/tts` 走一次「生成 → 保存为模板」，确认新模板出现在 settings 下拉里
