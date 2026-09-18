from __future__ import annotations

import json
import logging
import time
from dataclasses import replace

from ..clients.openai_compat import OpenAICompatibleClient


def model_settings(config, settings):
    """Reuse the current provider connection, never a credential from the old repo."""
    result = []
    thinking = settings.get("enable_thinking", False)
    if type(thinking) is not bool:
        raise ValueError("auxiliary.enable_thinking must be true or false")
    for role in ("primary", "fallback"):
        raw = settings[role]
        candidates = list(config.models.values())
        exact = [m for m in candidates if m.model == raw["model"]]
        provider = [m for m in candidates if m.api_key_env == raw["api_key_env"]]
        if not exact and not provider:
            raise ValueError(f"augmentation auxiliary {role} requires a configured connection using {raw['api_key_env']}")
        base = (exact or provider)[0]
        result.append(replace(
            base, name=f"augmentation_{role}", model=raw["model"],
            temperature=float(settings.get("temperature", 1.0)),
            max_tokens=int(settings.get("max_tokens", 32768)),
            timeout_sec=int(settings.get("timeout_sec", 180)), max_retries=1,
            thinking={"type": "enabled" if thinking else "disabled"} if role == "fallback" else None,
            extra_body={**({"enable_thinking": thinking} if role == "primary" else {}), "stream": thinking},
        ))
    return result


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("auxiliary response must be a JSON object")
    return value


def strings(value, fields):
    for name in fields:
        if not isinstance(value.get(name), str) or not value[name].strip():
            raise ValueError(f"auxiliary response requires nonempty string {name}")
    return {name: value[name].strip() for name in fields}


class Auxiliary:
    def __init__(self, models, settings, client_factory=OpenAICompatibleClient):
        self.models, self.settings, self.client_factory = models, settings, client_factory

    def request(self, system, user, validator):
        history = []
        error = None
        # Initial request + two retries, then switch to the fallback provider.
        for model in self.models:
            for attempt in range(1, int(self.settings.get("retries", 2)) + 2):
                messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
                if error is not None:
                    reason = str(error)[:400] if isinstance(error, ValueError) else type(error).__name__
                    messages.append({"role": "user", "content": f"Previous transformation failed validation or delivery ({reason}). Return the complete required structure; transform the source only, do not answer it."})
                client = self.client_factory(model)
                try:
                    response = client.chat(messages)
                    data = validator(response.content)
                    history.append({"model": model.model, "attempt": attempt, "status": "completed"})
                    return data, {"model": model.model, "fallback_used": model is self.models[1], "attempts": history}
                except Exception as exc:
                    error = exc
                    history.append({"model": model.model, "attempt": attempt, "status": "failed", "error_type": type(exc).__name__})
                    logging.getLogger(__name__).warning("augmentation auxiliary failed | model=%s | attempt=%s | error_type=%s", model.model, attempt, type(exc).__name__)
                    if attempt <= int(self.settings.get("retries", 2)):
                        time.sleep(float(self.settings.get("retry_delay_sec", 1)))
                finally:
                    session = getattr(client, "session", None)
                    if session is not None:
                        session.close()
        reason = str(error)[:400] if isinstance(error, ValueError) else type(error).__name__
        raise RuntimeError(f"augmentation auxiliary exhausted primary and fallback; last failure: {reason}") from error
