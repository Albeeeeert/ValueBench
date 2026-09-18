from __future__ import annotations

import base64
import json
import logging
import mimetypes
import random
import time
from pathlib import Path
from typing import Any

import requests

from ..config import ModelConfig
from ..schemas import ApiResponse


class ModelError(RuntimeError):
    """Base model request error."""


class FatalModelError(ModelError):
    """Authentication, billing, or explicitly unavailable network failure."""


class ItemModelError(ModelError):
    """Failure limited to one input item."""


_QUOTA_ERROR_MARKERS = (
    "arrearage", "insufficient_balance", "insufficient balance", "insufficient_quota",
    "insufficient quota", "overdue-payment", "overdue payment", "overdue_payment",
    "quota exhausted", "account balance", "余额不足", "额度不足",
)
_AUTH_ERROR_MARKERS = (
    "invalid_api_key", "invalid api key", "authentication failed",
    "authentication_error", "unauthorized",
)
_ITEM_POLICY_ERROR_MARKERS = (
    '"code":"cyber_policy"', '"code": "cyber_policy"',
    "possible cybersecurity risk", "trusted access for cyber",
)
_FATAL_NETWORK_ERROR_MARKERS = (
    "failed to resolve", "name or service not known",
    "temporary failure in name resolution", "network is unreachable", "no route to host",
)


def _is_fatal_network_error(exc: BaseException) -> bool:
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current).lower())
        current = current.__cause__ or current.__context__
    combined = " ".join(parts)
    return any(marker in combined for marker in _FATAL_NETWORK_ERROR_MARKERS)


class OpenAICompatibleClient:
    def __init__(
        self,
        config: ModelConfig,
        *,
        session: requests.Session | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.logger = logger or logging.getLogger(__name__)

    def clone(self) -> "OpenAICompatibleClient":
        return OpenAICompatibleClient(self.config, logger=self.logger)

    @property
    def endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else base + "/chat/completions"

    @staticmethod
    def _image_part(path: Path) -> dict[str, Any]:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}

    def user_message(self, text: str, image_paths: list[Path] | None = None) -> dict[str, Any]:
        images = image_paths or []
        if images and not self.config.supports_images:
            raise ValueError(f"model {self.config.model} is not configured for image input")
        if not images:
            return {"role": "user", "content": text}
        return {
            "role": "user",
            "content": [self._image_part(path) for path in images]
            + [{"type": "text", "text": text}],
        }

    @staticmethod
    def _error(response: requests.Response) -> tuple[str, str]:
        try:
            value = response.json()
        except ValueError:
            return "", response.text[:500]
        if not isinstance(value, dict):
            return "", response.text[:500]
        candidates: list[Any] = [value.get("error"), value]
        nested = value.get("response")
        if isinstance(nested, dict):
            candidates.append(nested.get("error"))
        for error in candidates:
            if not isinstance(error, dict):
                continue
            code = str(error.get("code") or error.get("type") or "")
            message = str(error.get("message") or "")[:500]
            if code or message:
                return code, message
        if isinstance(value.get("choices"), list):
            # Completion text (including reasoning) is model output, not an API
            # error message. It may legitimately discuss authorization or quotas.
            return "", ""
        return "", response.text[:500]

    @staticmethod
    def _check_error(status: int, code: str, message: str) -> None:
        combined = f"{code} {message}".lower()
        if code.lower() == "cyber_policy" or any(marker in combined for marker in _ITEM_POLICY_ERROR_MARKERS):
            raise ItemModelError(f"model content policy rejected current item: {code or status}")
        if status in (401, 402) or any(marker in combined for marker in (*_AUTH_ERROR_MARKERS, *_QUOTA_ERROR_MARKERS)):
            raise FatalModelError(f"model authentication or billing failure: {code or status}")
        if status >= 400:
            raise ItemModelError(f"HTTP {status} {code}: {message}")

    @classmethod
    def _stream_data(cls, response: requests.Response) -> dict[str, Any]:
        """Collect SSE answer and reasoning separately; reject incomplete streams."""
        def events():
            parts: list[str] = []
            for raw in response.iter_lines():
                line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                if not line:
                    if parts:
                        yield "\n".join(parts)
                        parts = []
                elif line.startswith("data:"):
                    parts.append(line[5:].lstrip())
            if parts:
                yield "\n".join(parts)

        answer, reasoning = [], []
        request_id, finish_reason = "", None
        usage: dict[str, Any] = {}
        complete = False
        for event in events():
            if event.strip() == "[DONE]":
                complete = True
                break
            data = json.loads(event)
            if not isinstance(data, dict):
                raise ValueError("stream event must be an object")
            if data.get("error") or (data.get("code") and not data.get("choices")):
                error = data.get("error", data)
                error = error if isinstance(error, dict) else {"message": str(error)}
                code, message = str(error.get("code") or error.get("type") or ""), str(error.get("message", ""))[:500]
                cls._check_error(400, code, message)
            request_id = str(data.get("id") or request_id)
            if isinstance(data.get("usage"), dict):
                usage = data["usage"]
            choices = data.get("choices", [])
            if not isinstance(choices, list):
                raise ValueError("stream choices must be an array")
            for choice in choices:
                if not isinstance(choice, dict):
                    raise ValueError("stream choice must be an object")
                if choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta", {})
                if not isinstance(delta, dict):
                    raise ValueError("stream delta must be an object")
                for field, output in (("content", answer), ("reasoning_content", reasoning)):
                    value = delta.get(field)
                    if value is not None:
                        if not isinstance(value, str):
                            raise ValueError(f"stream {field} must be a string")
                        output.append(value)
                if choice.get("finish_reason") is not None:
                    finish_reason = choice["finish_reason"]
                    complete = True
        if not complete:
            raise ItemModelError("model response stream ended before completion")
        return {"id": request_id, "usage": usage, "choices": [{"finish_reason": finish_reason, "message": {
            "content": "".join(answer), "reasoning_content": "".join(reasoning),
        }}]}

    def chat(self, messages: list[dict[str, Any]], *, json_mode: bool = False) -> ApiResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.config.thinking is not None:
            payload["thinking"] = self.config.thinking
        payload.update(self.config.extra_body)
        headers = {
            "Authorization": "Bearer " + self.config.api_key(),
            "Content-Type": "application/json",
        }
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries):
            request_started = time.perf_counter()
            try:
                response = self.session.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.config.timeout_sec,
                    **({"stream": True} if payload.get("stream") else {}),
                )
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout) as exc:
                raise ItemModelError(f"model request timed out: {exc}") from exc
            except requests.exceptions.ConnectionError as exc:
                if _is_fatal_network_error(exc):
                    raise FatalModelError(f"model network failure: {exc}") from exc
                raise ItemModelError(f"model transient connection failure: {exc}") from exc
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 < self.config.max_retries:
                    base = self.config.backoff_base_sec * (2**attempt)
                    time.sleep(base + random.uniform(0.0, self.config.backoff_jitter_sec))
                    continue
                raise ItemModelError(f"model request retries exhausted: {exc}") from exc
            else:
                streamed = bool(payload.get("stream"))
                json_response = not streamed or "application/json" in response.headers.get("Content-Type", "").lower()
                if response.status_code >= 400 or json_response:
                    try:
                        code, message = self._error(response)
                        self._check_error(response.status_code, code, message)
                    except Exception:
                        if streamed:
                            response.close()
                        raise
                if response.status_code >= 400:
                    raise ItemModelError(f"HTTP {response.status_code}")
                else:
                    try:
                        data = response.json() if json_response else self._stream_data(response)
                        first_choice = data["choices"][0]
                        if not isinstance(first_choice, dict):
                            raise TypeError("choices[0] must be an object")
                        choice = first_choice.get("message")
                        if not isinstance(choice, dict):
                            choice = {}
                        content_value = choice.get("content")
                        if content_value is None or str(content_value) == "":
                            content_value = first_choice.get("text", "")
                        content = str(content_value or "")
                    except ModelError:
                        raise
                    except requests.RequestException as exc:
                        raise ItemModelError(f"model response stream interrupted: {exc}") from exc
                    except (KeyError, IndexError, TypeError, ValueError) as exc:
                        last_error = exc
                        if attempt + 1 < self.config.max_retries:
                            base = self.config.backoff_base_sec * (2**attempt)
                            time.sleep(base + random.uniform(0.0, self.config.backoff_jitter_sec))
                            continue
                        raise ItemModelError(f"invalid chat completion response: {exc}") from exc
                    finally:
                        if streamed:
                            response.close()
                    if not content.strip():
                        last_error = RuntimeError("model returned an empty response")
                        if attempt + 1 < self.config.max_retries:
                            base = self.config.backoff_base_sec * (2**attempt)
                            time.sleep(base + random.uniform(0.0, self.config.backoff_jitter_sec))
                            continue
                        raise ItemModelError("model returned an empty response")
                    return ApiResponse(
                        content=content,
                        reasoning_content=str(choice.get("reasoning_content", "")),
                        usage=data.get("usage", {}) if isinstance(data.get("usage"), dict) else {},
                        elapsed_sec=round(time.perf_counter() - request_started, 3),
                        request_id=str(data.get("id", "")),
                    )
        raise ItemModelError(f"model request retries exhausted: {last_error}")

    def chat_completions(
        self,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """兼容场景构建器的文本接口，底层仍复用统一请求与错误边界。"""
        json_mode = response_format == {"type": "json_object"}
        return self.chat(messages, json_mode=json_mode).content
