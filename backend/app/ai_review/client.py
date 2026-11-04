"""Configurable client for an OpenAI-compatible chat completions API."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Protocol

import httpx


class AIReviewError(RuntimeError):
    """Base error for AI review configuration or provider failures."""


class AIConfigurationError(AIReviewError):
    """The AI provider is not configured correctly."""


class AIProviderError(AIReviewError):
    """The AI provider could not return a usable response."""


class AIProvider(Protocol):
    """Interface the reviewer uses; tests can supply a fake implementation."""

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Return the provider's text response."""
        ...


@dataclass(frozen=True)
class AIProviderConfig:
    """Settings for an OpenAI-compatible provider."""

    api_key: str = field(repr=False)
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise AIConfigurationError("Set the AI_API_KEY environment variable.")
        if not self.model.strip():
            raise AIConfigurationError("Set the AI_MODEL environment variable.")
        if not self.base_url.strip():
            raise AIConfigurationError("AI_BASE_URL cannot be empty.")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise AIConfigurationError("AI_TIMEOUT_SECONDS must be a positive number.")

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> AIProviderConfig:
        """Load settings when the provider is created, not when this module imports."""
        env = os.environ if environ is None else environ

        api_key = env.get("AI_API_KEY", "").strip()
        model = env.get("AI_MODEL", "").strip()
        base_url = env.get(
            "AI_BASE_URL", "https://api.openai.com/v1"
        ).strip()

        try:
            timeout_seconds = float(env.get("AI_TIMEOUT_SECONDS", "30"))
        except ValueError as exc:
            raise AIConfigurationError(
                "AI_TIMEOUT_SECONDS must be a number."
            ) from exc

        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )


class OpenAICompatibleProvider:
    """Call a provider that implements POST /chat/completions."""

    def __init__(
        self,
        config: AIProviderConfig | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config or AIProviderConfig.from_env()
        self._http_client = http_client

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        if not system_prompt.strip() or not user_prompt.strip():
            raise AIReviewError("Both system and user prompts must be provided.")

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        client_context = (
            nullcontext(self._http_client)
            if self._http_client is not None
            else httpx.Client(timeout=self.config.timeout_seconds)
        )

        try:
            with client_context as client:
                response = client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.config.timeout_seconds,
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise AIProviderError("The AI provider request timed out.") from exc
        except httpx.HTTPStatusError as exc:
            raise AIProviderError(
                f"The AI provider returned HTTP {exc.response.status_code}."
            ) from exc
        except httpx.RequestError as exc:
            raise AIProviderError("Could not connect to the AI provider.") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise AIProviderError("The AI provider returned invalid JSON.") from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIProviderError(
                "The AI provider response did not contain a chat message."
            ) from exc

        if not isinstance(content, str) or not content.strip():
            raise AIProviderError("The AI provider returned an empty response.")

        return content.strip()
"""Replaceable OpenAI-compatible JSON completion client."""

import httpx

from app.config import http_timeout, llm_configuration


class AIReviewUnavailable(RuntimeError):
    """AI is unconfigured, unreachable, or returned an invalid response."""


def is_configured() -> bool:
    key, model, _ = llm_configuration()
    return bool(key and model)


async def complete_json(system_prompt: str, user_prompt: str) -> str:
    api_key, model, endpoint = llm_configuration()
    if not api_key or not model:
        raise AIReviewUnavailable("AI review is unavailable because LLM_API_KEY and LLM_MODEL are not configured.")
    if not endpoint.startswith("https://") and not endpoint.startswith("http://127.0.0.1") and not endpoint.startswith("http://localhost"):
        raise AIReviewUnavailable("AI review endpoint must use HTTPS (or localhost for development).")
    try:
        async with httpx.AsyncClient(timeout=http_timeout(), follow_redirects=False) as client:
            response = await client.post(endpoint, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json={
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            })
    except httpx.TimeoutException as exc:
        raise AIReviewUnavailable("AI provider did not respond in time.") from exc
    except httpx.RequestError as exc:
        raise AIReviewUnavailable("AI provider is temporarily unavailable.") from exc
    if response.status_code >= 400:
        raise AIReviewUnavailable(f"AI provider returned HTTP {response.status_code}.")
    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise AIReviewUnavailable("AI provider returned a malformed response.") from exc
    if not isinstance(content, str):
        raise AIReviewUnavailable("AI provider response did not contain JSON text.")
    return content
