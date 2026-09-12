from __future__ import annotations

import base64
import io
import logging
from enum import Enum
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field, field_validator


logger = logging.getLogger("character_memory.visual_generation")


class VisualPurpose(str, Enum):
    AVATAR = "AVATAR"
    SELFIE = "SELFIE"
    SCENE = "SCENE"
    STICKER = "STICKER"


class VisualPromptPlan(BaseModel):
    purpose: VisualPurpose
    visual_intent: str = Field(min_length=1, max_length=600)
    positive_prompt: str = Field(min_length=1, max_length=6000)
    negative_prompt: str = Field(default="", max_length=2000)
    aspect_ratio: str = Field(default="1:1", max_length=16)
    identity_constraints: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("aspect_ratio")
    @classmethod
    def normalize_ratio(cls, value: str) -> str:
        ratio = str(value or "1:1").strip()
        allowed = {"1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"}
        return ratio if ratio in allowed else "1:1"


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    negative_prompt: str = Field(default="", max_length=2500)
    aspect_ratio: str = Field(default="1:1", max_length=16)
    size: str = Field(default="1K", max_length=32)
    reference_images: list[str] = Field(default_factory=list, max_length=4)


class ImageGenerationResult(BaseModel):
    provider: str
    model: str
    payload: bytes
    mime_type: str
    width: int | None = None
    height: int | None = None


class ImageGenerationProvider(Protocol):
    name: str
    supports_reference_images: bool

    def available(self) -> bool: ...

    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult: ...

    def close(self) -> None: ...


def _sniff_mime(payload: bytes, fallback: str = "image/png") -> str:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    return fallback


class AgnesImageProvider:
    name = "agnes"
    supports_reference_images = True

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://apihub.agnes-ai.com/v1",
        model: str = "agnes-image-2.5-flash",
        timeout_seconds: float = 180.0,
        client: httpx.Client | None = None,
    ):
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "https://apihub.agnes-ai.com/v1").rstrip("/")
        self.model = str(model or "agnes-image-2.5-flash").strip()
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=max(10.0, float(timeout_seconds)))

    def available(self) -> bool:
        return bool(self.api_key)

    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        if not self.api_key:
            raise RuntimeError("AGNES_API_KEY is not configured")
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": request.prompt,
            "size": request.size or "1K",
            "ratio": request.aspect_ratio or "1:1",
            "return_base64": True,
        }
        if request.reference_images:
            body["extra_body"] = {
                "image": request.reference_images,
                "response_format": "b64_json",
            }
        response = self.client.post(
            f"{self.base_url}/images/generations",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        if response.is_error:
            detail = response.text[:600]
            raise RuntimeError(f"Agnes image generation failed with HTTP {response.status_code}: {detail}")
        try:
            data = response.json()
            item = (data.get("data") or [])[0]
        except (ValueError, TypeError, IndexError, AttributeError) as exc:
            raise RuntimeError("Agnes image generation returned an invalid response") from exc

        encoded = item.get("b64_json") or item.get("base64")
        if encoded:
            try:
                payload = base64.b64decode(encoded)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("Agnes returned invalid base64 image data") from exc
            if not payload:
                raise RuntimeError("Agnes returned an empty image")
            return ImageGenerationResult(
                provider=self.name,
                model=self.model,
                payload=payload,
                mime_type=_sniff_mime(payload),
            )

        url = str(item.get("url") or "").strip()
        if not url:
            raise RuntimeError("Agnes response contained neither b64_json nor url")
        image_response = self.client.get(url, headers={"Accept": "image/*"})
        if image_response.is_error or not image_response.content:
            raise RuntimeError(f"Agnes generated image download failed with HTTP {image_response.status_code}")
        mime = str(image_response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        return ImageGenerationResult(
            provider=self.name,
            model=self.model,
            payload=image_response.content,
            mime_type=_sniff_mime(image_response.content, mime if mime.startswith("image/") else "image/png"),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class MsimgProvider:
    name = "msimg"
    supports_reference_images = False

    def __init__(
        self,
        api_key: str,
        *,
        models: str | list[str] = "qwen",
        timeout_seconds: float = 300.0,
        downloader: httpx.Client | None = None,
    ):
        self.api_key = str(api_key or "").strip()
        if isinstance(models, str):
            values = [part.strip() for part in models.split(",") if part.strip()]
        else:
            values = [str(part).strip() for part in models if str(part).strip()]
        self.models = values or ["qwen"]
        self.timeout_seconds = max(30.0, float(timeout_seconds))
        self._owns_downloader = downloader is None
        self.downloader = downloader or httpx.Client(timeout=self.timeout_seconds)

    def available(self) -> bool:
        return bool(self.api_key)

    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        if not self.api_key:
            raise RuntimeError("MSIMG_API_KEY / MODELSCOPE_API_TOKEN is not configured")
        if request.reference_images:
            raise RuntimeError("msimg 0.0.4 does not expose a reference-image contract")
        try:
            from msimg import generate_image
        except ImportError as exc:
            raise RuntimeError("msimg is not installed; install the image-generation extra") from exc

        model_value: str | list[str] = self.models[0] if len(self.models) == 1 else self.models
        result = generate_image(
            prompt=request.prompt,
            api_configs=self.api_key,
            models=model_value,
            size=request.aspect_ratio or request.size or "1:1",
            enable_failover=len(self.models) > 1,
            poll_timeout=int(self.timeout_seconds),
        )
        if not result:
            raise RuntimeError("msimg returned no image")

        image = result.get("image") if isinstance(result, dict) else None
        if image is not None and hasattr(image, "save"):
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            payload = buffer.getvalue()
            if payload:
                size = getattr(image, "size", None)
                width = int(size[0]) if isinstance(size, tuple) and len(size) >= 2 else None
                height = int(size[1]) if isinstance(size, tuple) and len(size) >= 2 else None
                return ImageGenerationResult(
                    provider=self.name,
                    model=str(result.get("model") or self.models[0]),
                    payload=payload,
                    mime_type=_sniff_mime(payload),
                    width=width,
                    height=height,
                )

        url = str(result.get("url") or "") if isinstance(result, dict) else ""
        if not url:
            raise RuntimeError("msimg result contained neither image nor url")
        response = self.downloader.get(url, headers={"Accept": "image/*"})
        if response.is_error or not response.content:
            raise RuntimeError(f"msimg generated image download failed with HTTP {response.status_code}")
        mime = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        return ImageGenerationResult(
            provider=self.name,
            model=str(result.get("model") or self.models[0]),
            payload=response.content,
            mime_type=_sniff_mime(response.content, mime if mime.startswith("image/") else "image/png"),
        )

    def close(self) -> None:
        if self._owns_downloader:
            self.downloader.close()


class VisualPromptPlanner:
    """Compile character state + visual intent into provider-neutral image prompts."""

    def __init__(self, model):
        self.model = model

    @staticmethod
    def _compact(value: Any, limit: int) -> str:
        if value is None:
            return ""
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        text = str(value).strip()
        return text[:limit]

    def plan(
        self,
        character_id: str,
        *,
        purpose: VisualPurpose,
        persona: str,
        mental_state: Any = None,
        recent_dialogue: list[str] | None = None,
        visual_intent: str = "",
        has_reference_image: bool = False,
    ) -> VisualPromptPlan:
        recent = []
        for raw in recent_dialogue or []:
            text = " ".join(str(raw or "").split()).strip()
            if text:
                recent.append(text[:360])
            if len(recent) >= 8:
                break
        prompt = f"""你正在为持续存在的聊天角色编译一条绘图提示词。你不是在回复用户，也不要决定是否应该发图；角色已经做出了视觉表达决定。

目标：
1. 严格保持人物核心身份、年龄感、发色、眼睛、气质和标志性特征的一致性，不要为了画面效果擅自换人。
2. purpose={purpose.value}。AVATAR 要适合聊天头像；SELFIE 要像人物本人自然分享的自拍；SCENE 是人物想表达的场景/配图；STICKER 要突出单一聊天情绪并简洁清晰。
3. 有参考头像时，把它视为人物身份锚点；提示词强调保留身份而不是重新设计角色。
4. 最近对话和 Mental State 只用于推断合理氛围，不要原样泄露隐私、姓名、关系秘密或长期记忆内容到绘图提示词。
5. 不虚构会改变人物事实的重要事件。自拍场景应自然、日常，不要自动变成摄影棚写真。
6. 输出给通用图像模型的 positive_prompt 要完整、具体、可直接使用；negative_prompt 只写真正有助于避免身份漂移/低质量的问题。
7. aspect_ratio 只能从 1:1、3:4、4:3、16:9、9:16、2:3、3:2、21:9 中选择。头像默认 1:1，自拍通常 3:4 或 4:3，贴图默认 1:1。
8. 不要输出解释或思维过程，只返回 VisualPromptPlan JSON。

Character ID: {character_id}
Purpose: {purpose.value}
Has reference image: {has_reference_image}

[Persona]
{self._compact(persona, 7000)}

[Mental State]
{self._compact(mental_state, 1600) or '暂无明确持续状态'}

[Recent Dialogue - mood context only]
{chr(10).join(recent) or '暂无近期对话'}

[Character Visual Intent]
{self._compact(visual_intent, 800) or '保持人物身份一致，生成自然且适合当前 purpose 的图像'}
"""
        method = getattr(self.model, "structured_for_session", None)
        if callable(method):
            result = method(prompt, VisualPromptPlan, f"visual-plan:{character_id}:{purpose.value.lower()}")
        else:
            method = getattr(self.model, "structured_with_images_for_session", None)
            if not callable(method):
                raise RuntimeError("loaded model does not support structured visual planning")
            result = method(prompt, [], VisualPromptPlan, f"visual-plan:{character_id}:{purpose.value.lower()}")
        if isinstance(result, VisualPromptPlan):
            return result
        return VisualPromptPlan.model_validate(result)


def build_image_providers(settings) -> dict[str, ImageGenerationProvider]:
    return {
        "agnes": AgnesImageProvider(
            getattr(settings, "agnes_api_key", ""),
            base_url=getattr(settings, "agnes_base_url", "https://apihub.agnes-ai.com/v1"),
            model=getattr(settings, "agnes_image_model", "agnes-image-2.5-flash"),
            timeout_seconds=getattr(settings, "image_generation_timeout_seconds", 180.0),
        ),
        "msimg": MsimgProvider(
            getattr(settings, "msimg_api_key", ""),
            models=getattr(settings, "msimg_models", "qwen"),
            timeout_seconds=getattr(settings, "image_generation_timeout_seconds", 180.0),
        ),
    }
