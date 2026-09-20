from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TtsProviderSpec:
    id: str
    label: str
    default_voice: str
    voices: tuple[str, ...]
    supports_speed: bool = True
    device_mode: str = "local"  # local | cloud
    device_hot_apply: bool = False


FORMAL_TTS_PROVIDERS: tuple[TtsProviderSpec, ...] = (
    TtsProviderSpec(
        id="kokoro",
        label="Kokoro 82M v1.1 zh",
        default_voice="zf_001",
        voices=("zf_001", "zf_002", "zf_003", "zf_004"),
        device_hot_apply=False,
    ),
    TtsProviderSpec(
        id="sherpa",
        label="Sherpa VITS",
        default_voice="0",
        voices=("0", "2", "5"),
        device_hot_apply=False,
    ),
    TtsProviderSpec(
        id="edge",
        label="Microsoft Edge TTS (online)",
        default_voice="zh-CN-XiaoxiaoNeural",
        voices=(
            "zh-CN-XiaoxiaoNeural",
            "zh-CN-XiaoyiNeural",
            "zh-CN-YunjianNeural",
            "zh-CN-YunxiNeural",
            "zh-CN-YunyangNeural",
        ),
        device_mode="cloud",
        device_hot_apply=True,
    ),
    TtsProviderSpec(
        id="gsv",
        label="GSV-TTS-Lite (local)",
        default_voice="murasame",
        voices=("murasame",),
        device_hot_apply=True,
    ),
)

FORMAL_TTS_PROVIDER_IDS: tuple[str, ...] = tuple(item.id for item in FORMAL_TTS_PROVIDERS)
FORMAL_TTS_PROVIDER_SET = frozenset(FORMAL_TTS_PROVIDER_IDS)
FORMAL_TTS_PROVIDER_PATTERN = r"^(" + "|".join(FORMAL_TTS_PROVIDER_IDS) + r")$"
TTS_PROVIDER_BY_ID = {item.id: item for item in FORMAL_TTS_PROVIDERS}


def provider_spec(provider_id: str) -> TtsProviderSpec:
    key = str(provider_id or "").strip().lower()
    try:
        return TTS_PROVIDER_BY_ID[key]
    except KeyError as exc:
        allowed = ", ".join(FORMAL_TTS_PROVIDER_IDS)
        raise ValueError(f"Unsupported formal TTS provider: {key or '<empty>'}. Allowed: {allowed}") from exc
