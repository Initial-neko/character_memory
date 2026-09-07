from __future__ import annotations

from abc import ABC, abstractmethod
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

    @abstractmethod
    def plan_day(self, context: str) -> DailyLifePlan:
        ...

    @abstractmethod
    def write_diary(self, context: str) -> DiaryResult:
        ...

    def close(self) -> None:
        pass


class OpenAICompatibleModel(PersonModel):
    def __init__(self, api_key: str, model: str = "deepseek-v4-flash", base_url: str = "https://opencode.ai/zen/go/v1", timeout: float = 120, temperature: float = 0.7, attempts: int = 2, session_id: str | None = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.attempts = attempts
        self.default_session_id = session_id or str(uuid.uuid4())
        self.session_id = self.default_session_id
        self.last_request_messages: list[dict] = []
        self.last_response_text: str = ""
        self.last_attempt: int = 0
        self.client = httpx.Client(timeout=self.timeout)
        self._call_lock = threading.RLock()
        logger.info("provider.ready model=%s base_url=%s default_session=%s attempts=%s timeout=%ss", self.model, self.base_url, self.default_session_id, self.attempts, self.timeout)

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
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "User-Agent": "character-memory/0.4", "x-opencode-client": "character-memory"}
        if include_session:
            headers["x-opencode-session"] = self.resolve_session_id(conversation_id)
        return headers

    @staticmethod
    def _message_chars(messages: list[dict]) -> int:
        return sum(len(str(message.get("content", ""))) for message in messages)

    def _request(self, messages: list[dict], *, conversation_id: str | None = None) -> str:
        payload = {"model": self.model, "messages": messages, "temperature": self.temperature}
        url = f"{self.base_url}/chat/completions"
        session_id = self.resolve_session_id(conversation_id)
        started = time.perf_counter()
        logger.info("provider.request start model=%s session=%s messages=%d input_chars=%d", self.model, session_id, len(messages), self._message_chars(messages))
        try:
            r = self.client.post(url, headers=self._headers(include_session=True, conversation_id=conversation_id), json=payload)
        except Exception:
            logger.exception("provider.request transport_error model=%s session=%s duration_ms=%d", self.model, session_id, int((time.perf_counter() - started) * 1000))
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        if r.is_error:
            request_id = r.headers.get("x-request-id") or r.headers.get("request-id") or r.headers.get("cf-ray") or ""
            body = self._error_body(r)
            logger.error("provider.request failed status=%s model=%s session=%s duration_ms=%d request_id=%s body=%s", r.status_code, self.model, session_id, duration_ms, request_id or "-", body)
            raise ProviderHTTPError(r.status_code, url, body, request_id)
        data = r.json()
        text = data["choices"][0]["message"]["content"]
        logger.info("provider.request done status=%s model=%s session=%s duration_ms=%d output_chars=%d", r.status_code, self.model, session_id, duration_ms, len(text or ""))
        return text

    @staticmethod
    def _system_prompt(schema: type[BaseModel]) -> str:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        return "你是持久化 AI 人物运行时的结构化认知模块。严格根据输入中的人物身份、经历、心理状态和事件做决定。不要把人物写成客服或无条件迎合用户。perception/reaction 只能是给开发者看的简短、安全摘要，不得输出隐藏推理过程。只输出一个合法 JSON 对象，不要 Markdown。JSON 必须符合这个 schema：" + schema_json

    def preview_messages(self, prompt: str, schema: type[BaseModel]) -> list[dict]:
        return [{"role": "system", "content": self._system_prompt(schema)}, {"role": "user", "content": prompt}]

    def _call(self, prompt: str, schema: type[BaseModel], *, conversation_id: str | None = None):
        with self._call_lock:
            messages = self.preview_messages(prompt, schema)
            self.last_request_messages = []
            self.last_response_text = ""
            self.last_attempt = 0
            last_error: Exception | None = None
            for attempt in range(self.attempts):
                try:
                    self.last_request_messages = [dict(message) for message in messages]
                    self.last_attempt = attempt + 1
                    logger.info("provider.structured_call attempt=%d/%d schema=%s", attempt + 1, self.attempts, schema.__name__)
                    text = self._request(messages, conversation_id=conversation_id)
                    self.last_response_text = text
                    result = schema.model_validate(self._json(text))
                    logger.info("provider.structured_call valid attempt=%d schema=%s", attempt + 1, schema.__name__)
                    return result
                except (json.JSONDecodeError, ValidationError, KeyError, TypeError, ValueError) as exc:
                    last_error = exc
                    logger.warning("provider.structured_call invalid attempt=%d/%d schema=%s error=%s", attempt + 1, self.attempts, schema.__name__, exc)
                    if attempt + 1 >= self.attempts:
                        break
                    messages.append({"role": "assistant", "content": text if "text" in locals() else ""})
                    messages.append({"role": "user", "content": "上一份输出不是合法的目标 JSON。请重新生成完整 JSON，只修正结构/字段约束，不添加解释或 Markdown。"})
            raise RuntimeError(f"Model returned invalid structured output after {self.attempts} attempts: {last_error}") from last_error

    def react(self, context: str) -> PersonReaction:
        return self._call(context, PersonReaction)

    def react_for_session(self, context: str, session_id: str) -> PersonReaction:
        return self._call(context, PersonReaction, conversation_id=session_id)

    def plan_day(self, context: str) -> DailyLifePlan:
        return self._call(context, DailyLifePlan)

    def write_diary(self, context: str) -> DiaryResult:
        return self._call(context, DiaryResult)

    def check_remote(self) -> dict:
        url = f"{self.base_url}/models"
        started = time.perf_counter()
        logger.info("provider.models start base_url=%s", self.base_url)
        r = self.client.get(url, headers=self._headers(include_session=False), timeout=min(self.timeout, 30))
        if r.is_error:
            request_id = r.headers.get("x-request-id") or r.headers.get("request-id") or r.headers.get("cf-ray") or ""
            body = self._error_body(r)
            raise ProviderHTTPError(r.status_code, url, body, request_id)
        data = r.json()
        logger.info("provider.models done status=%s duration_ms=%d models_visible=%d", r.status_code, int((time.perf_counter() - started) * 1000), len(data.get("data", [])))
        return {"ok": True, "model": self.model, "models_visible": len(data.get("data", []))}

    def check_remote_chat(self) -> dict:
        text = self._request([{"role": "user", "content": "Reply exactly with OK"}], conversation_id="doctor")
        return {"ok": True, "model": self.model, "reply": text[:120], "session_id": self.resolve_session_id("doctor")}

    def close(self) -> None:
        self.client.close()
