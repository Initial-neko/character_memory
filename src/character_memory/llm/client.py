from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
import logging
import re
import time
import threading
import uuid

import httpx
from pydantic import BaseModel, ValidationError

from character_memory.domain.models import DailyLifePlan, DiaryResult, PersonReaction


logger = logging.getLogger("character_memory.llm")


@dataclass(frozen=True)
class ModelCallTrace:
    request_messages: list[dict] = field(default_factory=list)
    response_text: str = ""
    attempt: int = 0
    model: str = ""


@dataclass(frozen=True)
class ModelCallResult:
    value: BaseModel
    trace: ModelCallTrace


class ProviderHTTPError(RuntimeError):
    def __init__(self, status_code: int, url: str, body: str, request_id: str = ""):
        self.status_code = status_code
        self.url = url
        self.body = body
        self.request_id = request_id
        suffix = f" | request_id={request_id}" if request_id else ""
        super().__init__(f"Provider HTTP {status_code} from {url}: {body}{suffix}")


class PersonModel(ABC):
    @abstractmethod
    def react(self, context: str) -> PersonReaction:
        ...

    def react_for_session(self, context: str, session_id: str) -> PersonReaction:
        return self.react(context)

    def react_with_images_for_session(self, context: str, image_data_urls: list[str], session_id: str) -> PersonReaction:
        return self.react_for_session(context, session_id)

    def react_call_for_session(self, context: str, session_id: str) -> ModelCallResult:
        return ModelCallResult(
            value=self.react_for_session(context, session_id),
            trace=ModelCallTrace(model=str(getattr(self, "model", "") or "")),
        )

    def react_call_with_images_for_session(self, context: str, image_data_urls: list[str], session_id: str) -> ModelCallResult:
        return ModelCallResult(
            value=self.react_with_images_for_session(context, image_data_urls, session_id),
            trace=ModelCallTrace(model=str(getattr(self, "vision_model", getattr(self, "model", "")) or "")),
        )

    def structured_with_images_for_session(
        self,
        prompt: str,
        image_data_urls: list[str],
        schema: type[BaseModel],
        session_id: str,
    ):
        raise RuntimeError("this PersonModel does not support generic structured vision analysis")

    def structured_for_session(self, prompt: str, schema: type[BaseModel], session_id: str):
        return self.structured_with_images_for_session(prompt, [], schema, session_id)

    @abstractmethod
    def plan_day(self, context: str) -> DailyLifePlan:
        ...

    @abstractmethod
    def write_diary(self, context: str) -> DiaryResult:
        ...

    def close(self) -> None:
        pass


class OpenAICompatibleModel(PersonModel):
    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-flash",
        base_url: str = "https://opencode.ai/zen/go/v1",
        timeout: float = 120,
        temperature: float = 0.7,
        attempts: int = 2,
        session_id: str | None = None,
        vision_model: str = "deepseek-v4-flash-vision-exp",
    ):
        self.api_key = api_key
        self.model = model
        self.vision_model = vision_model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.attempts = attempts
        self.default_session_id = session_id or str(uuid.uuid4())
        self.session_id = self.default_session_id
        # Compatibility/debug fields only. Runtime persistence must use the
        # ModelCallTrace returned from the same invocation, never these globals.
        # They are guarded only while being updated; provider calls themselves
        # are intentionally concurrent so different characters do not queue
        # behind one shared model-wide lock.
        self.last_request_messages: list[dict] = []
        self.last_response_text: str = ""
        self.last_attempt: int = 0
        self.last_model: str = model
        self.client = httpx.Client(timeout=self.timeout)
        self._debug_lock = threading.Lock()
        logger.info(
            "provider.ready model=%s vision_model=%s base_url=%s default_session=%s attempts=%s timeout=%ss",
            self.model,
            self.vision_model,
            self.base_url,
            self.default_session_id,
            self.attempts,
            self.timeout,
        )

    @staticmethod
    def _json(text: str):
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            a, b = text.find("{"), text.rfind("}")
            if a >= 0 and b > a:
                return json.loads(text[a : b + 1])
            raise

    @staticmethod
    def _error_body(response: httpx.Response, limit: int = 4000) -> str:
        text = (response.text or "").strip()
        if not text:
            return "<empty response body>"
        try:
            text = json.dumps(response.json(), ensure_ascii=False, separators=(",", ":"))
        except (ValueError, TypeError):
            pass
        if len(text) > limit:
            text = text[:limit] + "…"
        return text

    def resolve_session_id(self, conversation_id: str | None = None) -> str:
        if not conversation_id:
            return self.default_session_id
        try:
            return str(uuid.UUID(str(conversation_id)))
        except ValueError:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, f"character-memory:{conversation_id}"))

    def _headers(self, include_session: bool = True, conversation_id: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "character-memory/0.4",
            "x-opencode-client": "character-memory",
        }
        if include_session:
            headers["x-opencode-session"] = self.resolve_session_id(conversation_id)
        return headers

    @staticmethod
    def _message_chars(messages: list[dict]) -> int:
        return sum(len(str(message.get("content", ""))) for message in messages)

    @staticmethod
    def _trace_safe_messages(messages: list[dict]) -> list[dict]:
        """Keep multimodal request shape without persisting base64 image bytes."""
        safe: list[dict] = []
        for message in messages:
            copied = dict(message)
            content = copied.get("content")
            if isinstance(content, list):
                blocks = []
                for block in content:
                    item = dict(block)
                    if item.get("type") == "image_url":
                        image_url = item.get("image_url")
                        raw_url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url or "")
                        if raw_url.startswith("data:"):
                            header = raw_url.split(",", 1)[0]
                            item["image_url"] = {"url": f"{header},<base64 omitted>"}
                    blocks.append(item)
                copied["content"] = blocks
            safe.append(copied)
        return safe

    def _request(
        self,
        messages: list[dict],
        *,
        conversation_id: str | None = None,
        json_object: bool = False,
        model: str | None = None,
    ) -> str:
        selected_model = model or self.model
        payload = {"model": selected_model, "messages": messages, "temperature": self.temperature}
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        url = f"{self.base_url}/chat/completions"
        session_id = self.resolve_session_id(conversation_id)
        started = time.perf_counter()
        logger.info(
            "provider.request start model=%s session=%s messages=%d input_chars=%d json_object=%s",
            selected_model,
            session_id,
            len(messages),
            self._message_chars(messages),
            json_object,
        )
        try:
            r = self.client.post(
                url,
                headers=self._headers(include_session=True, conversation_id=conversation_id),
                json=payload,
            )
        except Exception:
            logger.exception(
                "provider.request transport_error model=%s session=%s duration_ms=%d",
                selected_model,
                session_id,
                int((time.perf_counter() - started) * 1000),
            )
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        if r.is_error:
            request_id = r.headers.get("x-request-id") or r.headers.get("request-id") or r.headers.get("cf-ray") or ""
            body = self._error_body(r)
            logger.error(
                "provider.request failed status=%s model=%s session=%s duration_ms=%d request_id=%s body=%s",
                r.status_code,
                selected_model,
                session_id,
                duration_ms,
                request_id or "-",
                body,
            )
            raise ProviderHTTPError(r.status_code, url, body, request_id)
        data = r.json()
        text = data["choices"][0]["message"]["content"]
        logger.info(
            "provider.request done status=%s model=%s session=%s duration_ms=%d output_chars=%d",
            r.status_code,
            selected_model,
            session_id,
            duration_ms,
            len(text or ""),
        )
        return text

    @staticmethod
    def _system_prompt(schema: type[BaseModel]) -> str:
        if schema is PersonReaction:
            return (
                "你正在决定一个持续存在人物对当前事件的反应。严格遵循输入中的 Persona、Memory、Mental State、Available Stickers、Available Images 和 Behavioral Contract。"
                "返回一个 JSON 对象。主要对外字段是 actions：0 到 3 个动作；通常使用 MESSAGE，单独字符表情可用 EMOJI；若输入列出了 Available Stickers，可以用 STICKER 并填写 sticker_id；若列出了 Available Images，可以用 IMAGE 并填写 image_id。"
                "STICKER/IMAGE 只能选择输入中真实存在的 id，不要编造资源 id，也不要为了显得活泼而强行发送媒体。"
                "如果当前用户事件附带真实图片，请结合你实际看到的图片内容理解和回应，不要只依赖文件名。"
                "没有真正想回复的内容时 actions 必须可以是空数组，不要因为用户发了消息就强行回复。"
                "perception 和 reaction 在有明确内容时尽量各写一句非常短的开发者安全摘要；mental_state_update 没有持续变化时留空。"
                "memory_candidates、intent_candidates 没有必要时都用空数组。不要输出隐藏思维链，也不要为了填字段编造内部活动。"
                "对外表达必须像这个人物本人自然聊天：不要刻意惜字，标点、停顿、emoji、颜文字、自然追问、表情包、图片和连续两三条消息都按 Persona 使用，但不要机械拆句或刷屏。"
                "不要把人物写成客服，也不要无条件迎合用户。"
            )
        if schema is DailyLifePlan:
            return "根据输入规划人物当天少量自然生活事件。返回 JSON 对象：events 为数组；social_post、image_prompt 可以为空。不要为了填满字段而编造事件。"
        if schema is DiaryResult:
            return "根据输入写简短日记并返回 JSON 对象，包含 diary、mental_state_update、memory_candidates。没有值得记忆的内容时 memory_candidates 可以为空数组。"
        return "根据输入返回符合目标对象语义的 JSON 对象，不要添加 JSON 之外的解释。"

    @staticmethod
    def _repair_prompt(schema: type[BaseModel], error: Exception) -> str:
        """Build a schema-specific repair request after structured validation fails."""
        if schema is PersonReaction:
            return (
                "上一份 JSON 不符合 PersonReaction。只修正结构，不扩写内容："
                "actions 必须是 0~3 个动作，MESSAGE/EMOJI 需要 message，STICKER 需要 sticker_id，IMAGE 需要 image_id；"
                "没有想回复时 actions=[]；memory_candidates 和 intent_candidates 必须是数组。只返回修正后的 JSON。"
            )

        fields = ", ".join(schema.model_fields.keys()) or "目标字段"
        message = " ".join(str(error).split())
        if len(message) > 700:
            message = message[:700] + "…"
        return (
            f"上一份 JSON 不符合 {schema.__name__}。请只修正为该对象的 JSON 结构，不要改成聊天回复格式。"
            f"目标字段：{fields}。校验错误：{message}。"
            "字段为空时使用该对象允许的空值/空数组；不要添加 JSON 之外的解释，只返回修正后的 JSON。"
        )

    def preview_messages(self, prompt: str, schema: type[BaseModel], image_data_urls: list[str] | None = None) -> list[dict]:
        if image_data_urls:
            user_content: str | list[dict] = [{"type": "text", "text": prompt}]
            user_content.extend({"type": "image_url", "image_url": {"url": value}} for value in image_data_urls)
        else:
            user_content = prompt
        return [
            {"role": "system", "content": self._system_prompt(schema)},
            {"role": "user", "content": user_content},
        ]

    def _call_result(
        self,
        prompt: str,
        schema: type[BaseModel],
        *,
        conversation_id: str | None = None,
        image_data_urls: list[str] | None = None,
    ) -> ModelCallResult:
        selected_model = self.vision_model if image_data_urls else self.model
        if image_data_urls and not selected_model:
            raise RuntimeError("vision_model is required for image input")
        messages = self.preview_messages(prompt, schema, image_data_urls=image_data_urls)
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            safe_messages = self._trace_safe_messages(messages)
            attempt_number = attempt + 1
            # Compatibility fields are best-effort only. Per-call ModelCallTrace
            # is authoritative and remains isolated even when calls overlap.
            with self._debug_lock:
                self.last_request_messages = safe_messages
                self.last_response_text = ""
                self.last_attempt = attempt_number
                self.last_model = selected_model
            try:
                logger.info(
                    "provider.structured_call attempt=%d/%d schema=%s model=%s images=%d",
                    attempt_number,
                    self.attempts,
                    schema.__name__,
                    selected_model,
                    len(image_data_urls or []),
                )
                text = self._request(
                    messages,
                    conversation_id=conversation_id,
                    json_object=True,
                    model=selected_model,
                )
                with self._debug_lock:
                    self.last_response_text = text
                result = schema.model_validate(self._json(text))
                logger.info(
                    "provider.structured_call valid attempt=%d schema=%s model=%s",
                    attempt_number,
                    schema.__name__,
                    selected_model,
                )
                return ModelCallResult(
                    value=result,
                    trace=ModelCallTrace(
                        request_messages=safe_messages,
                        response_text=text,
                        attempt=attempt_number,
                        model=selected_model,
                    ),
                )
            except (json.JSONDecodeError, ValidationError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "provider.structured_call invalid attempt=%d/%d schema=%s error=%s",
                    attempt_number,
                    self.attempts,
                    schema.__name__,
                    exc,
                )
                if attempt_number >= self.attempts:
                    break
                messages.append({"role": "assistant", "content": text if "text" in locals() else "{}"})
                messages.append({"role": "user", "content": self._repair_prompt(schema, exc)})
        raise RuntimeError(
            f"Model returned invalid structured output after {self.attempts} attempts: {last_error}"
        ) from last_error

    def _call(
        self,
        prompt: str,
        schema: type[BaseModel],
        *,
        conversation_id: str | None = None,
        image_data_urls: list[str] | None = None,
    ):
        return self._call_result(
            prompt,
            schema,
            conversation_id=conversation_id,
            image_data_urls=image_data_urls,
        ).value

    def react(self, context: str) -> PersonReaction:
        return self._call(context, PersonReaction)

    def react_for_session(self, context: str, session_id: str) -> PersonReaction:
        return self._call(context, PersonReaction, conversation_id=session_id)

    def react_with_images_for_session(self, context: str, image_data_urls: list[str], session_id: str) -> PersonReaction:
        return self._call(
            context,
            PersonReaction,
            conversation_id=session_id,
            image_data_urls=image_data_urls,
        )

    def react_call_for_session(self, context: str, session_id: str) -> ModelCallResult:
        return self._call_result(context, PersonReaction, conversation_id=session_id)

    def react_call_with_images_for_session(self, context: str, image_data_urls: list[str], session_id: str) -> ModelCallResult:
        return self._call_result(
            context,
            PersonReaction,
            conversation_id=session_id,
            image_data_urls=image_data_urls,
        )

    def structured_for_session(self, prompt: str, schema: type[BaseModel], session_id: str):
        return self._call(prompt, schema, conversation_id=session_id)

    def structured_with_images_for_session(
        self,
        prompt: str,
        image_data_urls: list[str],
        schema: type[BaseModel],
        session_id: str,
    ):
        return self._call(
            prompt,
            schema,
            conversation_id=session_id,
            image_data_urls=image_data_urls,
        )

    def plan_day(self, context: str) -> DailyLifePlan:
        return self._call(context, DailyLifePlan)

    def write_diary(self, context: str) -> DiaryResult:
        return self._call(context, DiaryResult)

    def check_remote(self) -> dict:
        url = f"{self.base_url}/models"
        started = time.perf_counter()
        logger.info("provider.models start base_url=%s", self.base_url)
        r = self.client.get(
            url,
            headers=self._headers(include_session=False),
            timeout=min(self.timeout, 30),
        )
        if r.is_error:
            request_id = r.headers.get("x-request-id") or r.headers.get("request-id") or r.headers.get("cf-ray") or ""
            body = self._error_body(r)
            raise ProviderHTTPError(r.status_code, url, body, request_id)
        data = r.json()
        logger.info(
            "provider.models done status=%s duration_ms=%d models_visible=%d",
            r.status_code,
            int((time.perf_counter() - started) * 1000),
            len(data.get("data", [])),
        )
        return {
            "ok": True,
            "model": self.model,
            "vision_model": self.vision_model,
            "models_visible": len(data.get("data", [])),
        }

    def check_remote_chat(self) -> dict:
        text = self._request(
            [{"role": "user", "content": "Reply exactly with OK"}],
            conversation_id="doctor",
        )
        return {
            "ok": True,
            "model": self.model,
            "reply": text[:120],
            "session_id": self.resolve_session_id("doctor"),
        }

    def close(self) -> None:
        self.client.close()
