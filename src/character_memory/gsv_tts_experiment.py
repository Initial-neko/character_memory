from __future__ import annotations

import argparse
import gc
from dataclasses import dataclass
import importlib.util
import io
import os
from pathlib import Path
import threading
import time
import wave
from typing import Any, Callable
from urllib.parse import quote

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from character_memory.web_lifecycle import on_app_event
from character_memory.config import read_archive_state
from character_memory.voices import (
    TEMPLATE_FILE_SUFFIX,
    VoiceProfile,
    VoiceProfileError,
    _character_id,
    discover_character_voices,
    discover_templates,
    resolve_voice_registry,
    template_root,
)


DEFAULT_PORT = 9014
DEFAULT_VOICE = "murasame"
DEFAULT_PERSONA_ROOT = "personas"
DEFAULT_SAMPLE_RATE = 32000
# GSV-TTS-Lite takes no generator= argument and draws every random number --
# token sampling and decoder noise -- from PyTorch's global RNG. Seeding it makes
# a voice reproducible; without a seed the same line varies by up to ~88% in
# duration between turns. See docs/current/GSV_TTS_EXPERIMENT.md.
DEFAULT_SEED = 1234

#: Values that switch GSV_TTS_SEED back to the old unseeded behaviour.
_SEED_DISABLED = {"", "none", "off", "random", "-1"}


def _env_seed(raw: str | None) -> int | None:
    if raw is None:
        return DEFAULT_SEED
    value = str(raw).strip().lower()
    if value in _SEED_DISABLED:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"GSV_TTS_SEED must be an integer or one of {sorted(_SEED_DISABLED)}: {raw!r}") from exc


@dataclass(frozen=True)
class GsvTtsResult:
    audio: bytes
    sample_rate: int
    model: str
    device: str
    voice: str
    inference_ms: float
    audio_ms: float
    rtf: float
    cuda_allocated_mb: float | None = None
    cuda_reserved_mb: float | None = None
    cuda_peak_mb: float | None = None
    seed: int | None = None


class GsvTtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str = Field(default=DEFAULT_VOICE, max_length=128)
    language: str = Field(default="zh", max_length=16)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    # None = fall back to the runtime default (GSV_TTS_SEED, i.e. DEFAULT_SEED).
    # Per-character voices will pin their own seed once the voice registry lands.
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)


class GsvRuntimeConfigRequest(BaseModel):
    # Voice manifests change only through POST /v1/voices/reload. ``configure``
    # unloads the engine whenever a tracked field changes, so accepting a
    # ``voices`` field here would drop the warm GPT/SoVITS weights for what is
    # purely a file change. Rejecting unknown fields keeps that door shut -- and
    # it is also what turns the retired ``ref_audio``/``ref_text`` pair into a
    # 4xx instead of a silent no-op: the reference belongs to a template now, so
    # a caller still sending one has to be told, not humoured.
    model_config = ConfigDict(extra="forbid")

    gpt_model: str | None = None
    sovits_model: str | None = None
    device: str | None = None
    models_dir: str | None = None
    voice: str | None = None
    language: str | None = None
    prompt_language: str | None = None
    preload: bool = False


def _persona_root(persona_root: str | Path | None) -> str:
    """Resolve the personas directory, preferring the argument over the env.

    A relative glob is resolved against the *cwd*, which the sidecar does not
    control, so ``scripts/start-gsv-tts.sh`` and the stack both pin an absolute
    path through ``GSV_TTS_PERSONA_ROOT``.
    """
    if persona_root is not None:
        value = str(persona_root).strip()
        if value:
            return value
    return (os.getenv("GSV_TTS_PERSONA_ROOT") or "").strip() or DEFAULT_PERSONA_ROOT


def _voices_root(voices_root: str | Path | None) -> str:
    """Templates live outside the persona tree; same absolute-path rule applies."""

    if voices_root is not None:
        value = str(voices_root).strip()
        if value:
            return value
    return str(template_root(None))


def float_audio_to_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    values = np.asarray(samples, dtype=np.float32).reshape(-1)
    values = np.nan_to_num(values, nan=0.0, posinf=1.0, neginf=-1.0)
    pcm = (np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate))
        wav.writeframes(pcm)
    return output.getvalue()


class GsvTtsRuntime:
    """Isolated GSV-TTS-Lite runtime.

    Heavy gsv_tts/torch imports happen only inside this sidecar process.
    Character Memory talks to it through HTTP so the main environment stays
    independent from the CUDA/Torch ABI used by GSV-TTS-Lite.
    """

    def __init__(
        self,
        *,
        gpt_model: str,
        sovits_model: str,
        device: str = "cuda",
        models_dir: str = "",
        default_voice: str = DEFAULT_VOICE,
        default_language: str = "zh",
        prompt_language: str = "auto",
        preload: bool = False,
        seed: int | None = DEFAULT_SEED,
        tts_factory: Callable[..., Any] | None = None,
        torch_module: Any | None = None,
        voices: dict[str, VoiceProfile] | None = None,
        persona_root: str | Path | None = None,
        voices_root: str | Path | None = None,
    ):
        self.gpt_model = str(gpt_model or "").strip()
        self.sovits_model = str(sovits_model or "").strip()
        self.device_requested = str(device or "cuda").strip()
        self.models_dir = str(models_dir or "").strip()
        self.default_voice = str(default_voice or DEFAULT_VOICE).strip() or DEFAULT_VOICE
        self.default_language = str(default_language or "zh").strip() or "zh"
        self.prompt_language = str(prompt_language or "auto").strip() or "auto"
        self.preload = bool(preload)
        self.seed = int(seed) if seed is not None else None
        self._tts_factory = tts_factory
        self._tts = None
        # Injected in tests (symmetric with tts_factory); load() imports the real
        # torch when it is None.
        self._torch = torch_module
        self.persona_root = _persona_root(persona_root)
        self.voices_root = _voices_root(voices_root)
        # An injected registry is authoritative: embedders and tests supply one
        # rather than depending on whatever happens to be on disk. With no
        # template tree to read, every injected name counts as a template --
        # nothing else can tell them apart, and only the Settings Center
        # selector reads the distinction (``_voice_ids``). ``_load_voices``
        # overwrites this from the tree it actually found.
        self._templates: dict[str, VoiceProfile] = dict(voices or {})
        self._archived_character_ids: set[str] = set()
        self._voices_error: str | None = None
        if voices is not None:
            self._voices: dict[str, VoiceProfile] = dict(voices)
        else:
            self._voices = {}
            self._rebuild_voices()
        self._load_ms: float | None = None
        self._load_error: str | None = None
        self._lock = threading.RLock()

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
        all_persona_paths = sorted(personas.glob("*/persona.yaml")) if personas.exists() else []
        self._archived_character_ids = {
            character_id
            for path in all_persona_paths
            if read_archive_state(path) is not None
            for character_id in [_character_id(path)]
            if character_id
        }
        persona_paths = [
            path for path in all_persona_paths
            if read_archive_state(path) is None
        ]
        templates = discover_templates(self.voices_root)
        # The template-only map is kept alongside the merged one because
        # ``status()["voices"]`` feeds the Settings Center selector, and
        # ``GSV_TTS_VOICE`` must name a template: a character id is resolvable on
        # the request path but is not a valid value for the setting, and it can
        # shadow a template name in the merged registry.
        self._templates = templates
        return resolve_voice_registry(
            templates=templates,
            character_voices=discover_character_voices(
                persona_paths, voices_root=self.voices_root
            ),
        )

    def _rebuild_voices(self) -> None:
        """Rebuild the registry, recording an unusable tree instead of dying of it.

        Spec §10: a template that does not parse is ``ready: false`` with a
        reason, and **the stack still starts**. dev_stack runs this process
        beside the rest of the stack and tears all of it down when this one
        exits, so an operator's stale experiment file used to cost them the
        whole console. The failure is reported through ``/health`` instead --
        the readers stay strict, so nothing is silently skipped or downgraded,
        and ``/v1/tts`` refuses with the same sentence ``/health`` reports.

        A failed *reload* keeps the registry it already has. The read is
        all-or-nothing, so committing a half-read tree would trade working
        voices for none of them; the error is still recorded, which is what
        makes ``/health`` and ``/v1/tts`` report the tree rather than the stale
        registry. ``_templates`` is restored alongside ``_voices`` because
        ``_load_voices`` assigns it on the way in, before it can raise.
        """
        previous_voices = self._voices
        previous_templates = self._templates
        previous_archived = self._archived_character_ids
        try:
            self._voices = self._load_voices()
        except VoiceProfileError as exc:
            self._voices = previous_voices
            self._templates = previous_templates
            self._archived_character_ids = previous_archived
            self._voices_error = str(exc)
            print(f"gsv-tts voices unavailable: {exc}", flush=True)
        else:
            self._voices_error = None

    def reload_voices(self) -> dict:
        """Re-read the template tree and the character references in place.

        Both trees feed one registry, so they are reloaded together: refreshing
        only one of them would leave a template that is newer than the
        characters pointing at it.

        Deliberately does not touch the engine: the GPT/SoVITS weights and the
        warm speaker cache stay resident, and a newly added reference clip is
        encoded lazily by ``infer_batched`` on its first use.
        """
        with self._lock:
            self._rebuild_voices()
            if self._voices_error:
                # Same external contract as before: the route turns this into a
                # 400 naming the file. The registry is left unready either way,
                # so the refusal and ``/health`` cannot disagree about it.
                raise VoiceProfileError(self._voices_error)
            return self.status()

    def _voice_ids(self) -> list[str]:
        """Template names, plus ``default_voice`` -- which need not be registered.

        Templates only, not the merged registry's keys: this list is the Settings
        Center ``GSV_TTS_VOICE`` selector, and that setting must name a template
        (spec §6.2). A character id resolves on the request path but is not a
        legal value here, and offering it would let an id that shadows a
        template mean something different from the template it hides.
        """
        voices = list(self._templates)
        if self.default_voice not in voices:
            voices.append(self.default_voice)
        return voices

    def _resolve_voice(
        self, requested: str | None
    ) -> tuple[str, str, str, str, str]:
        """Map a request's voice name onto the reference audio to clone.

        Returns ``(voice, ref_audio, ref_text, gpt_model, sovits_model)``.

        Two tiers, in order: the name as registered (which covers both a
        template name and a character id, since the registry is merged), then
        the default template.

        The first tier must not raise -- the browser sends ``voice: <character
        id>`` for *every* character, so rejecting unknown ids would mute every
        character that has not been given a voice yet. (A character that *has*
        a voice.yaml but a dead reference is a different case and raised at
        load time; see ``resolve_voice_registry``.)

        The second tier is the entire fallback, and it is a real profile with a
        real clip. There is deliberately no third tier: the runtime-global
        ``ref_audio``/``ref_text`` pair used to answer here, and it is no longer
        a reference the settings page maintains. What it *was*, though, is the
        proof that it must not come back -- because ``load()`` short-circuits on
        ``already_loaded``, a warm engine reached this branch after its default
        template went away and was handed that stale pair, which is HTTP 200
        with the wrong voice while ``/health`` reported not ready. So a request
        that resolves to nothing now raises, and names what is missing.

        The returned ``voice`` is the one actually used, so ``X-TTS-Voice`` and
        ``GsvTtsResult.voice`` never report a profile that was not applied.
        """
        name = str(requested or self.default_voice).strip() or self.default_voice
        if name in self._archived_character_ids:
            raise RuntimeError(
                f"Voice synthesis is disabled for archived character {name!r}; restore the character before speaking."
            )
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
            if self._voices_error:
                # Say what actually failed. "missing or unusable" would send the
                # operator hunting for a file the loader already read and named.
                raise RuntimeError(
                    f"No voice profile for {name!r}: the voice tree did not load. "
                    f"{self._voices_error}"
                )
            template = Path(self.voices_root) / f"{self.default_voice}{TEMPLATE_FILE_SUFFIX}"
            raise RuntimeError(
                f"No voice profile for {name!r} and no default template "
                f"{self.default_voice!r}: the template file {template} is missing or "
                "unusable. Create the default template in the TTS Lab voice design page "
                "or pick an existing template in Settings Center."
            )
        if not Path(profile.ref_audio).is_file():
            # Checked here, on the profile the request actually resolved to,
            # rather than as part of readiness: readiness is about the engine,
            # and a clip that was renamed or deleted since the tree was read is
            # about this one reference. A warm engine answers without re-reading
            # the tree, so this is the only place the two cannot drift apart.
            raise RuntimeError(
                f"Voice {profile.voice_id!r} ref_audio not found: {profile.ref_audio}"
            )
        # A profile only overrides the models it pins; unset means "inherit".
        return (
            profile.voice_id,
            profile.ref_audio,
            profile.ref_text,
            profile.gpt_model or self.gpt_model,
            profile.sovits_model or self.sovits_model,
        )

    @property
    def model_label(self) -> str:
        gpt = Path(self.gpt_model).name if self.gpt_model else "unset-gpt"
        sovits = Path(self.sovits_model).name if self.sovits_model else "unset-sovits"
        return f"{gpt}+{sovits}"

    def _dependency_status(self) -> tuple[bool, str | None]:
        if self._tts_factory is not None:
            return True, None
        try:
            available = importlib.util.find_spec("gsv_tts") is not None
        except (ImportError, ValueError):
            available = False
        if not available:
            return False, (
                "GSV-TTS-Lite is not importable. Start this sidecar with "
                "scripts/start-gsv-tts.sh so its external Python environment is used."
            )
        return True, None

    def _asset_status(self) -> tuple[bool, str | None]:
        """Ready when the shared base models exist and the voice tree loaded.

        The two model paths stay required: every template inherits them unless it
        pins its own.

        The *default* template is deliberately not part of this. It is the
        fallback for a character with no ``voice.yaml`` of its own, so a missing
        default costs that one character its voice -- while folding it in here
        cost every character its voice: a request naming a template that exists
        was refused with the default's name, because ``load`` checks readiness
        before it looks at what was asked for. What is broken about the default
        is still reported, by ``_default_template_problem``.
        """
        if self._voices_error:
            # The tree failed to load whole. Reported verbatim: it already names
            # the offending file, and the "not defined" reason below would send
            # the operator looking for a file that is sitting right there.
            return False, self._voices_error

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
        return True, None

    def _default_template_problem(self) -> str | None:
        """Why the fallback voice would fail, or ``None`` when it would not.

        Reported next to readiness rather than through it: the fallback being
        unusable is a fact the operator needs, and one that a request for some
        other template has no reason to care about.
        """
        if self._voices_error:
            # The tree failure is already the readiness reason; repeating it
            # here would put two sentences about one broken file side by side.
            return None
        if self.default_voice not in self._templates:
            return (
                f"Default template {self.default_voice!r} is not defined; create one in the "
                "TTS Lab voice design page or pick an existing template in Settings Center."
            )
        profile = self._voices.get(self.default_voice)
        if profile is not None and not Path(profile.ref_audio).is_file():
            return f"Default template {self.default_voice!r} ref_audio not found: {profile.ref_audio}"
        return None

    def _factory(self):
        if self._tts_factory is not None:
            return self._tts_factory
        from gsv_tts import TTS

        return TTS

    def _import_torch(self) -> None:
        if self._tts_factory is not None or self._torch is not None:
            return
        import torch

        self._torch = torch

    def _device(self) -> str:
        if self._tts is not None:
            config = getattr(self._tts, "tts_config", None)
            device = getattr(config, "device", None)
            if device is not None:
                return str(device)
        return self.device_requested

    def _cuda_memory(self, *, peak: bool = False) -> tuple[float | None, float | None, float | None]:
        torch = self._torch
        if torch is None or not self._device().startswith("cuda"):
            return None, None, None
        try:
            allocated = torch.cuda.memory_allocated() / (1024 * 1024)
            reserved = torch.cuda.memory_reserved() / (1024 * 1024)
            peak_value = torch.cuda.max_memory_allocated() / (1024 * 1024) if peak else None
            return (
                round(allocated, 1),
                round(reserved, 1),
                round(peak_value, 1) if peak_value is not None else None,
            )
        except Exception:
            return None, None, None

    def _reset_cuda_peak(self) -> None:
        torch = self._torch
        if torch is None or not self._device().startswith("cuda"):
            return
        try:
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
        except Exception:
            pass

    def _sync_cuda(self) -> None:
        torch = self._torch
        if torch is None or not self._device().startswith("cuda"):
            return
        try:
            torch.cuda.synchronize()
        except Exception:
            pass

    def resolve_seed(self, request: GsvTtsRequest) -> int | None:
        return request.seed if request.seed is not None else self.seed

    def _apply_seed(self, seed: int | None) -> int | None:
        """Seed PyTorch's global RNG and return the seed actually applied.

        MUST run inside ``self._lock`` and immediately before ``infer_batched``:
        GSV-TTS-Lite reads the *global* RNG (``torch.empty_like().exponential_``
        for token sampling in ``GPT_SoVITS/GPT/utils.py``, ``torch.randn_like``
        for decoder noise in ``GPT_SoVITS/SoVITS/models.py``) and accepts no
        ``generator=`` argument. Another thread drawing from the RNG in between
        would silently un-stabilise this request.
        """
        if seed is None:
            return None
        torch = self._torch
        if torch is None:
            return None
        value = int(seed)
        torch.manual_seed(value)
        return value

    def status(self) -> dict:
        deps_ready, deps_reason = self._dependency_status()
        assets_ready, assets_reason = self._asset_status()
        allocated, reserved, _peak = self._cuda_memory()
        reason = self._load_error or deps_reason or assets_reason
        return {
            "id": "gsv",
            "label": "GSV-TTS-Lite",
            "ready": bool(deps_ready and assets_ready and not self._load_error),
            "loaded": self._tts is not None,
            "model": self.model_label,
            "device": self._device(),
            "voices": self._voice_ids(),
            "default_voice": self.default_voice,
            "default_language": self.default_language,
            "seed": self.seed,
            "supports_speed": True,
            "supports_seed": True,
            "sample_rate": DEFAULT_SAMPLE_RATE,
            "load_ms": self._load_ms,
            "cuda_allocated_mb": allocated,
            "cuda_reserved_mb": reserved,
            "reason": reason,
            # Not folded into ``reason``: this one does not make the sidecar
            # unready, and a reader that treats every reason as a refusal would
            # take a working engine for a dead one.
            "default_template_problem": self._default_template_problem(),
            "note": "Full-WAV GSV sidecar used by both TTS Lab and formal :8001 routing.",
        }

    def load(self) -> dict:
        with self._lock:
            # Readiness is authoritative even for a warm engine, so it is
            # checked *before* the ``already_loaded`` shortcut. The registry can
            # change underneath a loaded engine (``/v1/voices/reload``, a
            # template renamed, its clip deleted), and a hot engine must not be
            # the reason a request reaches ``infer_batched`` with a reference
            # that no longer resolves.
            deps_ready, deps_reason = self._dependency_status()
            assets_ready, assets_reason = self._asset_status()
            if not deps_ready:
                raise RuntimeError(deps_reason or "GSV-TTS-Lite dependency unavailable")
            if not assets_ready:
                raise RuntimeError(assets_reason or "GSV-TTS-Lite assets unavailable")

            if self._tts is not None:
                return {**self.status(), "already_loaded": True}

            started = time.perf_counter()
            try:
                factory = self._factory()
                kwargs = {
                    "gpt_cache": [(1, 512), (1, 1024), (4, 512), (4, 1024)],
                    "sovits_cache": [50, 55],
                    "device": None if self.device_requested.lower() == "auto" else self.device_requested,
                    "use_bert": True,
                    "use_flash_attn": False,
                    "always_load_cnhubert": True,
                    "always_load_sv": True,
                }
                if self.models_dir:
                    kwargs["models_dir"] = self.models_dir
                engine = factory(**kwargs)
                engine.load_gpt_model(self.gpt_model)
                engine.load_sovits_model(self.sovits_model)
                # Deliberately no reference warmup: the clip is chosen per
                # request (from a template) and gsv_tts caches it on first use
                # (TTS.py:665-666,685-688), so warming one here would only make
                # "the engine loaded" depend on a file this step does not need --
                # and an empty or missing one fails as ``Invalid argument
                # returned 22``, a message that names no path at all.
                self._tts = engine
                self._import_torch()
                self._sync_cuda()
                self._load_ms = round((time.perf_counter() - started) * 1000.0, 1)
                self._load_error = None
                return {**self.status(), "already_loaded": False}
            except Exception as exc:
                self._tts = None
                self._load_error = str(exc)
                raise RuntimeError(f"GSV-TTS-Lite load failed: {exc}") from exc

    def unload(self) -> dict:
        with self._lock:
            engine = self._tts
            self._tts = None
            self._load_error = None
            self._load_ms = None
            if engine is not None:
                del engine
            gc.collect()
            torch = self._torch
            if torch is not None and self._device().startswith("cuda"):
                try:
                    torch.cuda.empty_cache()
                    torch.cuda.synchronize()
                except Exception:
                    pass
            return {**self.status(), "unloaded": True}

    def configure(self, request: GsvRuntimeConfigRequest) -> dict:
        updates = {
            "gpt_model": request.gpt_model,
            "sovits_model": request.sovits_model,
            "device_requested": request.device,
            "models_dir": request.models_dir,
            "default_voice": request.voice,
            "default_language": request.language,
            "prompt_language": request.prompt_language,
        }
        changed: list[str] = []
        with self._lock:
            for attr, raw in updates.items():
                if raw is None:
                    continue
                value = str(raw).strip()
                if attr == "default_voice":
                    value = value or DEFAULT_VOICE
                elif attr == "default_language":
                    value = value or "zh"
                elif attr == "prompt_language":
                    value = value or "auto"
                elif attr == "device_requested":
                    value = value or "cuda"
                if getattr(self, attr) != value:
                    setattr(self, attr, value)
                    changed.append(attr)

            if changed and self._tts is not None:
                engine = self._tts
                self._tts = None
                self._load_error = None
                self._load_ms = None
                del engine
                gc.collect()
                torch = self._torch
                if torch is not None:
                    try:
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
            elif changed:
                self._load_error = None
                self._load_ms = None

        status = self.status()
        if request.preload and status.get("ready"):
            status = self.load()
        return {**status, "changed": changed}

    def synthesize(self, request: GsvTtsRequest) -> GsvTtsResult:
        value = request.text.strip()
        if not value:
            raise ValueError("empty GSV-TTS text")

        with self._lock:
            # Resolved under the lock so a concurrent /v1/voices/reload cannot
            # swap the registry between choosing a reference and using it.
            voice, ref_audio, ref_text, gpt_model, sovits_model = self._resolve_voice(
                request.voice
            )
            self.load()
            self._reset_cuda_peak()
            applied_seed = self._apply_seed(self.resolve_seed(request))
            started = time.perf_counter()
            try:
                clips = self._tts.infer_batched(
                    spk_audio_paths=ref_audio,
                    prompt_audio_paths=ref_audio,
                    prompt_audio_texts=ref_text,
                    texts=value,
                    text_languages=(request.language or self.default_language).strip() or self.default_language,
                    prompt_languages=self.prompt_language,
                    return_subtitles=False,
                    speed=float(request.speed),
                    gpt_model=gpt_model,
                    sovits_model=sovits_model,
                )
                self._sync_cuda()
            except Exception as exc:
                raise RuntimeError(f"GSV-TTS-Lite synthesis failed: {exc}") from exc

            if not clips:
                raise RuntimeError("GSV-TTS-Lite returned no audio")
            clip = clips[0]
            raw_audio = getattr(clip, "audio_data", None)
            if raw_audio is None:
                raise RuntimeError("GSV-TTS-Lite returned a clip without audio_data")
            samples = np.asarray(raw_audio, dtype=np.float32).reshape(-1)
            if not samples.size:
                raise RuntimeError("GSV-TTS-Lite returned empty audio")
            sample_rate = int(getattr(clip, "samplerate", DEFAULT_SAMPLE_RATE) or DEFAULT_SAMPLE_RATE)
            inference_ms = (time.perf_counter() - started) * 1000.0
            audio_ms = len(samples) * 1000.0 / sample_rate
            allocated, reserved, peak = self._cuda_memory(peak=True)
            return GsvTtsResult(
                audio=float_audio_to_wav(samples, sample_rate),
                sample_rate=sample_rate,
                model=self.model_label,
                device=self._device(),
                voice=voice,
                inference_ms=round(inference_ms, 1),
                audio_ms=round(audio_ms, 1),
                rtf=round(inference_ms / audio_ms, 4) if audio_ms > 0 else 0.0,
                cuda_allocated_mb=allocated,
                cuda_reserved_mb=reserved,
                cuda_peak_mb=peak,
                seed=applied_seed,
            )


def create_gsv_tts_app(runtime: GsvTtsRuntime | None = None):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import Response
    except ImportError as exc:
        raise RuntimeError("GSV-TTS-Lite sidecar requires FastAPI in its isolated environment") from exc

    engine = runtime or GsvTtsRuntime(
        gpt_model=os.getenv("GSV_TTS_GPT_MODEL", ""),
        sovits_model=os.getenv("GSV_TTS_SOVITS_MODEL", ""),
        device=os.getenv("GSV_TTS_DEVICE", "cuda"),
        models_dir=os.getenv("GSV_TTS_MODELS_DIR", ""),
        default_voice=os.getenv("GSV_TTS_VOICE", DEFAULT_VOICE),
        default_language=os.getenv("GSV_TTS_LANGUAGE", "zh"),
        prompt_language=os.getenv("GSV_TTS_PROMPT_LANGUAGE", "auto"),
        preload=os.getenv("GSV_TTS_PRELOAD", "0").strip().lower() in {"1", "true", "yes", "on"},
        seed=_env_seed(os.getenv("GSV_TTS_SEED")),
    )
    app = FastAPI(title="Character Memory GSV-TTS-Lite Experiment", version="0.1")
    app.state.gsv_tts = engine

    @on_app_event(app, "startup")
    def startup():
        if engine.preload:
            try:
                engine.load()
            except RuntimeError as exc:
                print(f"gsv-tts preload failed: {exc}", flush=True)

    @app.get("/health")
    def health():
        return {"ok": True, **engine.status()}

    @app.post("/v1/load")
    def load():
        try:
            return engine.load()
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/v1/configure")
    def configure(request: GsvRuntimeConfigRequest):
        try:
            return engine.configure(request)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/unload")
    def unload():
        try:
            return engine.unload()
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/v1/voices/reload")
    def reload_voices():
        # An invalid voice.yaml is an operator error (VoiceProfileError is a
        # ValueError), so it surfaces as 400 with the offending file named
        # rather than silently leaving a stale registry in place.
        try:
            return engine.reload_voices()
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/tts")
    def synthesize(request: GsvTtsRequest):
        try:
            result = engine.synthesize(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(
            content=result.audio,
            media_type="audio/wav",
            headers={
                "X-TTS-Provider": "gsv",
                "X-TTS-Voice": quote(result.voice, safe=""),
                "X-TTS-Model": quote(result.model, safe="/:._-"),
                "X-TTS-Device": result.device,
                "X-TTS-Inference-Ms": str(result.inference_ms),
                "X-TTS-Audio-Ms": str(result.audio_ms),
                "X-TTS-RTF": str(result.rtf),
                "X-TTS-Sample-Rate": str(result.sample_rate),
                "X-TTS-Seed": "" if result.seed is None else str(result.seed),
                "X-TTS-Cuda-Allocated-MB": "" if result.cuda_allocated_mb is None else str(result.cuda_allocated_mb),
                "X-TTS-Cuda-Reserved-MB": "" if result.cuda_reserved_mb is None else str(result.cuda_reserved_mb),
                "X-TTS-Cuda-Peak-MB": "" if result.cuda_peak_mb is None else str(result.cuda_peak_mb),
            },
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="character-gsv-tts")
    parser.add_argument("--host", default=os.getenv("GSV_TTS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("GSV_TTS_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--gpt-model", default=os.getenv("GSV_TTS_GPT_MODEL", ""))
    parser.add_argument("--sovits-model", default=os.getenv("GSV_TTS_SOVITS_MODEL", ""))
    parser.add_argument("--device", default=os.getenv("GSV_TTS_DEVICE", "cuda"))
    parser.add_argument("--models-dir", default=os.getenv("GSV_TTS_MODELS_DIR", ""))
    parser.add_argument("--voice", default=os.getenv("GSV_TTS_VOICE", DEFAULT_VOICE))
    parser.add_argument("--language", default=os.getenv("GSV_TTS_LANGUAGE", "zh"))
    parser.add_argument("--prompt-language", default=os.getenv("GSV_TTS_PROMPT_LANGUAGE", "auto"))
    parser.add_argument(
        "--seed",
        default=os.getenv("GSV_TTS_SEED", str(DEFAULT_SEED)),
        help="PyTorch RNG seed for reproducible voices; 'none' restores unseeded sampling.",
    )
    parser.add_argument(
        "--preload",
        action="store_true",
        default=os.getenv("GSV_TTS_PRELOAD", "0").strip().lower() in {"1", "true", "yes", "on"},
    )
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("GSV-TTS-Lite isolated environment must contain FastAPI and uvicorn") from exc

    runtime = GsvTtsRuntime(
        gpt_model=args.gpt_model,
        sovits_model=args.sovits_model,
        device=args.device,
        models_dir=args.models_dir,
        default_voice=args.voice,
        default_language=args.language,
        prompt_language=args.prompt_language,
        preload=args.preload,
        seed=_env_seed(args.seed),
    )
    print(
        f"gsv-tts: http://{args.host}:{args.port} model={runtime.model_label} "
        f"device={args.device} preload={args.preload} seed={runtime.seed}",
        flush=True,
    )
    uvicorn.run(create_gsv_tts_app(runtime), host=args.host, port=args.port, reload=False, timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
