from __future__ import annotations

import base64
import importlib.util
import io
import logging
import re
import sys
from enum import Enum
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field


logger = logging.getLogger("character_memory.visual_generation")


class VisualPurpose(str, Enum):
    AVATAR = "AVATAR"
    SELFIE = "SELFIE"
    SCENE = "SCENE"
    STICKER = "STICKER"


_DEFAULT_ASPECT_RATIO = {
    VisualPurpose.AVATAR: "1:1",
    VisualPurpose.SELFIE: "3:4",
    VisualPurpose.SCENE: "4:3",
    VisualPurpose.STICKER: "1:1",
}


def visual_aspect_ratio(purpose: VisualPurpose) -> str:
    """Business-owned output shape; the prompt LLM does not control transport parameters."""
    return _DEFAULT_ASPECT_RATIO.get(purpose, "1:1")


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

    def configured(self) -> bool: ...
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


_AGNES_SIZE_BY_RATIO = {
    "1:1": "1024x1024",
    "3:4": "768x1024",
    "4:3": "1024x768",
    "16:9": "1024x576",
    "9:16": "576x1024",
    "2:3": "682x1024",
    "3:2": "1024x682",
    "21:9": "1024x439",
}


class AgnesImageProvider:
    name = "agnes"
    supports_reference_images = True

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://apihub.agnes-ai.com/v1",
        model: str = "agnes-image-2.1-flash",
        timeout_seconds: float = 180.0,
        client: httpx.Client | None = None,
    ):
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "https://apihub.agnes-ai.com/v1").rstrip("/")
        self.model = str(model or "agnes-image-2.1-flash").strip()
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=max(10.0, float(timeout_seconds)))

    def configured(self) -> bool:
        return bool(self.api_key)

    def available(self) -> bool:
        return self.configured()

    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        if not self.api_key:
            raise RuntimeError("AGNES_API_KEY is not configured")
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": request.prompt,
            "size": _AGNES_SIZE_BY_RATIO.get(request.aspect_ratio or "1:1", "1024x1024"),
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

    def configured(self) -> bool:
        return bool(self.api_key)

    def available(self) -> bool:
        if not self.configured():
            return False
        if "msimg" in sys.modules:
            return True
        try:
            return importlib.util.find_spec("msimg") is not None
        except (ImportError, ValueError):
            return False

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
    """Compile character state + visual intent into one provider-ready text prompt.

    The character LLM already made the structured GENERATE_IMAGE decision. This
    stage deliberately returns plain text only; aspect ratio, reference images,
    provider selection and persistence remain application-owned.
    """

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

    @staticmethod
    def _clean_output(value: str) -> str:
        text = str(value or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:text|txt|markdown)?\s*|\s*```$", "", text, flags=re.S | re.I).strip()
        if not text:
            raise RuntimeError("visual prompt planner returned an empty prompt")
        return text

    def compile_prompt(
        self,
        character_id: str,
        *,
        purpose: VisualPurpose,
        persona: str,
        mental_state: Any = None,
        recent_dialogue: list[str] | None = None,
        visual_intent: str = "",
        has_reference_image: bool = False,
    ) -> str:
        recent: list[str] = []
        for raw in recent_dialogue or []:
            text = " ".join(str(raw or "").split()).strip()
            if text:
                recent.append(text[:360])
            if len(recent) >= 8:
                break

        purpose_guidance = {
            VisualPurpose.AVATAR: "适合作为聊天头像，主体清楚，构图简洁自然。",
            VisualPurpose.SELFIE: "像人物本人自然分享的日常自拍，不要自动变成摄影棚写真。",
            VisualPurpose.SCENE: "生成角色想表达给对方看的场景或配图，不要求人物必须出镜。",
            VisualPurpose.STICKER: "突出一个清晰聊天情绪，画面简洁。",
        }[purpose]

        user_prompt = f"""把下面信息编译成一条可以直接交给图像生成模型的最终绘图提示词。

要求：
- 只输出最终绘图提示词纯文本；不要 JSON、Markdown、代码块、字段名、解释或分析。
- 保持 Persona 中人物核心身份、年龄感、发色、眼睛、气质和标志性特征一致，不要为了画面效果擅自重新设计角色。
- Purpose={purpose.value}：{purpose_guidance}
- 最近对话和 Mental State 只用于推断合理氛围，不要把姓名、关系秘密、长期记忆原文等隐私内容机械抄进绘图提示词。
- 不虚构会改变人物事实的重要事件。
- 是否携带参考头像、画布比例、Provider 参数由程序处理；不要输出这些控制参数。

Character ID: {character_id}
Has reference image: {has_reference_image}

[Persona]
{self._compact(persona, 7000)}

[Mental State]
{self._compact(mental_state, 1600) or '暂无明确持续状态'}

[Recent Dialogue - mood context only]
{chr(10).join(recent) or '暂无近期对话'}

[Character Visual Intent]
{self._compact(visual_intent, 800) or '保持人物身份一致，生成自然且适合当前用途的图像'}
"""
        messages = [
            {
                "role": "system",
                "content": "你是绘图提示词编译器。只写最终可直接用于图像生成的 prompt 纯文本，不返回 JSON。",
            },
            {"role": "user", "content": user_prompt},
        ]
        session_id = f"visual-plan:{character_id}:{purpose.value.lower()}"
        request = getattr(self.model, "_request", None)
        if not callable(request):
            raise RuntimeError("loaded model does not expose a plain-text request path for visual prompt compilation")
        text = self._clean_output(
            request(
                messages,
                conversation_id=session_id,
                json_object=False,
            )
        )

        if has_reference_image:
            identity_policy = (
                "Use the supplied reference image as the identity anchor. Preserve the same person, facial features, "
                "hair color and style, eye color, age impression, and defining visual traits. Do not redesign, replace, "
                "or substitute the character."
            )
            text = f"{text}\n\n{identity_policy}"

        if len(text) > 8000:
            text = text[:8000].rstrip()
        return text


def build_image_providers(settings) -> dict[str, ImageGenerationProvider]:
    return {
        "agnes": AgnesImageProvider(
            getattr(settings, "agnes_api_key", ""),
            base_url=getattr(settings, "agnes_base_url", "https://apihub.agnes-ai.com/v1"),
            model=getattr(settings, "agnes_image_model", "agnes-image-2.1-flash"),
            timeout_seconds=getattr(settings, "image_generation_timeout_seconds", 180.0),
        ),
        "msimg": MsimgProvider(
            getattr(settings, "msimg_api_key", ""),
            models=getattr(settings, "msimg_models", "qwen"),
            timeout_seconds=getattr(settings, "image_generation_timeout_seconds", 180.0),
        ),
    }
