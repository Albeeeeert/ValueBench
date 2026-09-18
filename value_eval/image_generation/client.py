from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class QwenImageConfig:
    api_key: str
    model: str = "qwen-image-2.0"
    endpoint: str = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    task_endpoint: str = "https://dashscope.aliyuncs.com/api/v1/tasks"
    size: str = "2048*2048"
    prompt_extend: bool = False
    async_call: bool = False
    timeout_sec: int = 120
    poll_interval_sec: float = 2.0
    poll_timeout_sec: int = 900
    max_retries: int = 3
    rate_limit_retries: int = 6


class ImageApiError(RuntimeError):
    pass


class ImageModerationError(ImageApiError):
    pass


class FatalImageApiError(ImageApiError):
    pass


class QwenImageClient:
    def __init__(
        self,
        config: QwenImageConfig,
        *,
        session: requests.Session | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.logger = logger or logging.getLogger(__name__)
        self.request_ids: list[str] = []

    def clone(self) -> "QwenImageClient":
        return QwenImageClient(self.config, logger=self.logger)

    def _headers(self, *, asynchronous: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": "Bearer " + self.config.api_key,
            "Content-Type": "application/json",
        }
        if asynchronous:
            headers["X-DashScope-Async"] = "enable"
        return headers

    @staticmethod
    def _error(response: requests.Response) -> tuple[str, str]:
        try:
            data = response.json()
        except ValueError:
            return "", response.text[:500]
        if not isinstance(data, dict):
            return "", response.text[:500]
        error = data.get("error")
        if not isinstance(error, dict) or not error:
            error = data
        return str(error.get("code", "")), str(error.get("message", ""))[:500]

    @staticmethod
    def _fatal(status: int, code: str, message: str) -> bool:
        combined = f"{code} {message}".lower()
        return status in (401, 402, 403) or any(
            marker in combined
            for marker in (
                "invalid api key", "unauthorized", "forbidden", "arrearage",
                "insufficient balance", "allocationquota", "quota exhausted",
            )
        )

    @staticmethod
    def _moderated(code: str, message: str) -> bool:
        combined = f"{code} {message}".lower()
        return any(marker in combined for marker in ("inspection", "moderation", "policy", "sensitive"))

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        max_attempts = max(self.config.max_retries, self.config.rate_limit_retries)
        for attempt in range(max_attempts):
            status = 0
            try:
                response = self.session.request(method, url, timeout=self.config.timeout_sec, **kwargs)
                status = response.status_code
            except requests.RequestException as exc:
                last_error = exc
                retry_limit = self.config.max_retries
            else:
                code, message = self._error(response)
                if self._fatal(status, code, message):
                    raise FatalImageApiError(f"image API authentication or billing failure: {code or status}")
                if status >= 400:
                    if self._moderated(code, message):
                        raise ImageModerationError(f"image prompt rejected: {code or message}")
                    last_error = ImageApiError(f"HTTP {status} {code}: {message}")
                    retry_limit = self.config.rate_limit_retries if status == 429 else self.config.max_retries
                    if status not in (408, 409, 429) and status < 500:
                        raise last_error
                else:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        last_error = ImageApiError(f"image API returned invalid JSON: {exc}")
                        retry_limit = self.config.max_retries
                    else:
                        if not isinstance(data, dict):
                            raise ImageApiError("image API response must be an object")
                        code = str(data.get("code", ""))
                        message = str(data.get("message", ""))
                        if code and code not in ("OK", "200"):
                            if self._moderated(code, message):
                                raise ImageModerationError(f"image prompt rejected: {code}")
                            raise ImageApiError(f"image API error {code}: {message[:500]}")
                        request_id = str(data.get("request_id") or response.headers.get("x-request-id", ""))
                        if request_id:
                            self.request_ids.append(request_id)
                        return data
            if attempt + 1 >= retry_limit:
                if isinstance(last_error, requests.RequestException) or status in (408, 409, 429) or status >= 500:
                    raise FatalImageApiError(f"image API connectivity retries exhausted: {last_error}")
                raise ImageApiError(str(last_error))
            delay = min(10.0 * (2**attempt), 60.0) if status == 429 else 2**attempt
            self.logger.warning("image API retry attempt=%s/%s", attempt + 1, retry_limit)
            time.sleep(delay)
        raise ImageApiError(f"image API request failed: {last_error}")

    @staticmethod
    def _result(output: Any) -> tuple[bytes, str] | None:
        results = output.get("results") if isinstance(output, dict) else None
        if isinstance(results, list) and results and isinstance(results[0], dict):
            if results[0].get("b64_json"):
                return base64.b64decode(results[0]["b64_json"]), "base64"
            if results[0].get("url"):
                return b"", str(results[0]["url"])
        if isinstance(output, dict) and output.get("b64_json"):
            return base64.b64decode(output["b64_json"]), "base64"
        if isinstance(output, dict) and output.get("url"):
            return b"", str(output["url"])
        choices = output.get("choices") if isinstance(output, dict) else None
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message", {})
            content = message.get("content", []) if isinstance(message, dict) else []
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                value = part.get("image") or part.get("image_url")
                if isinstance(value, dict):
                    value = value.get("url")
                if value:
                    return b"", str(value)
        return None

    def generate(self, prompt: str) -> bytes:
        self.request_ids = []
        payload = {
            "model": self.config.model,
            "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
            "parameters": {
                "size": self.config.size,
                "n": 1,
                "prompt_extend": self.config.prompt_extend,
                "watermark": False,
            },
        }
        data = self._request(
            "POST", self.config.endpoint,
            headers=self._headers(asynchronous=self.config.async_call), json=payload,
        )
        output = data.get("output", {})
        result = self._result(output)
        if result is None:
            task_id = str(output.get("task_id", "")) if isinstance(output, dict) else ""
            if not task_id:
                raise ImageApiError("image API returned neither an image nor a task ID")
            result = self._poll(task_id)
        image_bytes, source = result
        return image_bytes if source == "base64" else self._download(source)

    def _download(self, url: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries):
            try:
                response = self.session.get(url, timeout=self.config.timeout_sec)
                response.raise_for_status()
                return response.content
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 < self.config.max_retries:
                    time.sleep(2**attempt)
        raise FatalImageApiError(f"image download retries exhausted: {last_error}")

    def _poll(self, task_id: str) -> tuple[bytes, str]:
        deadline = time.monotonic() + self.config.poll_timeout_sec
        while time.monotonic() < deadline:
            data = self._request(
                "GET", self.config.task_endpoint.rstrip("/") + "/" + task_id,
                headers=self._headers(),
            )
            output = data.get("output", {})
            result = self._result(output)
            if result is not None:
                return result
            status = str(output.get("task_status", "")).upper() if isinstance(output, dict) else ""
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                code = str(output.get("code", ""))
                message = str(output.get("message", ""))
                if self._moderated(code, message):
                    raise ImageModerationError(f"image task rejected: {code or status}")
                raise ImageApiError(f"image task ended with {status}: {message[:500]}")
            time.sleep(self.config.poll_interval_sec)
        raise ImageApiError(f"image task polling timed out: {task_id}")

