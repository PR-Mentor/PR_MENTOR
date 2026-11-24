"""Replaceable OpenAI-compatible JSON completion client."""

import asyncio
import hashlib
import json
import math
import os
import time
import weakref
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.ai_review.providers import ProviderConfig, provider_registry
from app.config import http_timeout, llm_configuration

_current_task: ContextVar[str | None] = ContextVar("pr_mentor_llm_task", default=None)
_last_call: ContextVar[dict | None] = ContextVar("pr_mentor_last_provider_call", default=None)
_provider_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]] = weakref.WeakKeyDictionary()


class AIReviewUnavailable(RuntimeError):
    """AI is unconfigured, unreachable, or returned an invalid response."""


def is_configured(task: str | None = None) -> bool:
    return bool(provider_registry(task))


def configured_model(task: str | None = None) -> str | None:
    """Return configured model identity without exposing credentials or endpoint."""
    providers = provider_registry(task)
    return providers[0].model if providers else None


def last_call_metadata() -> dict | None:
    """Return safe provenance for the latest successful provider call in this context."""
    value = _last_call.get()
    return dict(value) if value else None


def clear_last_call_metadata() -> None:
    """Avoid accidentally reusing provider provenance from an earlier task call."""
    _last_call.set(None)


def annotate_last_call(**values: object) -> None:
    """Attach non-secret request provenance to the current successful provider call."""
    current = last_call_metadata()
    if current is not None:
        current.update(values)
        _last_call.set(current)


def _semaphore(provider: ProviderConfig) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphores = _provider_semaphores.get(loop)
    if semaphores is None:
        semaphores = {}
        _provider_semaphores[loop] = semaphores
    semaphore = semaphores.get(provider.name)
    if semaphore is None:
        semaphore = asyncio.Semaphore(provider.max_concurrency)
        semaphores[provider.name] = semaphore
    return semaphore


def _is_json_generation_failure(body: bytes) -> bool:
    try:
        return json.loads(body)["error"]["code"] == "json_validate_failed"
    except (ValueError, TypeError, KeyError):
        return False


async def _request_provider(
    provider: ProviderConfig,
    system_prompt: str,
    user_prompt: str,
    timeout: float,
    max_response_bytes: int,
    *,
    allow_top_level_array: bool = False,
) -> tuple[str, dict, int]:
    if not provider.supports_json:
        raise AIReviewUnavailable("Configured provider does not support structured JSON responses.")
    if not provider.endpoint.startswith("https://") and not provider.endpoint.startswith("http://127.0.0.1") and not provider.endpoint.startswith("http://localhost"):
        raise AIReviewUnavailable("AI provider endpoint must use HTTPS (or localhost for development).")
    request = {
        "model": provider.model, "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    }
    async with _semaphore(provider):
        try:
            started = time.monotonic()
            chunks: list[bytes] = []
            size = 0
            async with asyncio.timeout(timeout), httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                for use_json_mode in (True, False):
                    async with client.stream(
                        "POST", provider.endpoint,
                        headers={"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"},
                        json=request,
                    ) as response:
                        groq_bad_request = response.status_code == 400 and urlsplit(provider.endpoint).hostname == "api.groq.com"
                        if not groq_bad_request:
                            response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > max_response_bytes:
                                response.raise_for_status()
                                raise AIReviewUnavailable("AI provider response exceeded the configured size limit.")
                            chunks.append(chunk)
                        if (
                            use_json_mode and groq_bad_request
                            and _is_json_generation_failure(b"".join(chunks))
                        ):
                            # Keep local JSON and task validation on the second response.
                            request.pop("response_format")
                            chunks.clear()
                            size = 0
                            continue
                        response.raise_for_status()
                        break
            body = httpx.Response(200, content=b"".join(chunks)).json()
            latency_ms = max(0, int((time.monotonic() - started) * 1000))
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise AIReviewUnavailable("AI provider did not respond in time.") from exc
        except httpx.RequestError as exc:
            raise AIReviewUnavailable("AI provider is temporarily unavailable.") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 413:
                raise AIReviewUnavailable("AI provider request payload exceeded size limit (HTTP 413).") from exc
            raise AIReviewUnavailable(f"AI provider returned HTTP {exc.response.status_code}.") from exc
        except ValueError as exc:
            raise AIReviewUnavailable("AI provider returned malformed JSON.") from exc
    try:
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise AIReviewUnavailable("AI provider returned a malformed structured response.")
        cleaned_content = content.strip()
        if cleaned_content.startswith("```"):
            lines = cleaned_content.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned_content = "\n".join(lines).strip()
        parsed = json.loads(cleaned_content)
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise AIReviewUnavailable("AI provider returned a malformed structured response.") from exc
    if allow_top_level_array:
        if not isinstance(parsed, (dict, list)):
            raise AIReviewUnavailable("AI provider response must contain one JSON object or array.")
    else:
        if not isinstance(parsed, dict):
            raise AIReviewUnavailable("AI provider response must contain one JSON object.")
    usage = body.get("usage") if isinstance(body, dict) else None
    usage_data = {
        key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance(usage, dict) and isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool) and usage[key] >= 0
    }
    finish_reason = body["choices"][0].get("finish_reason")
    if isinstance(finish_reason, str) and finish_reason in ("stop", "length", "content_filter", "tool_calls"):
        usage_data["finish_reason"] = finish_reason
    usage_data["response_format"] = "json_object" if "response_format" in request else "text"
    return cleaned_content, usage_data, latency_ms


async def complete_json(
    system_prompt: str,
    user_prompt: str,
    *,
    task: str | None = None,
    timeout: float | None = None,
    max_response_bytes: int | None = None,
    exclude_providers: list[str] | None = None,
    allow_top_level_array: bool = False,
) -> str:
    selected_task = task or _current_task.get()
    effective_allow_array = allow_top_level_array or (selected_task == "issue_explanation")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout) or timeout <= 0
    ):
        raise ValueError("timeout must be a positive number")
    if max_response_bytes is not None and (
        isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes < 1
    ):
        raise ValueError("max_response_bytes must be a positive integer")
    api_key, model_name, _ = llm_configuration()
    if not api_key or not model_name:
        # Allow provider_registry to be patched in tests even without env vars.
        all_providers = provider_registry(selected_task)
        if not all_providers:
            providers = []
        else:
            providers = [item for item in all_providers if item.name not in set(exclude_providers or [])]
    else:
        providers = [item for item in provider_registry(selected_task) if item.name not in set(exclude_providers or [])]
    if not providers:
        _last_call.set({"status": "unavailable", "providers_attempted": [], "attempt_count": 0})
        raise AIReviewUnavailable("AI review is unavailable because LLM_API_KEY and LLM_MODEL are not configured.")
    try:
        retries = max(0, min(int(os.getenv("LLM_PROVIDER_RETRIES", "1")), 2))
    except ValueError:
        retries = 1
    try:
        rate_limit_retries = max(retries, min(int(os.getenv("LLM_PROVIDER_RATE_LIMIT_RETRIES", "3")), 5))
    except ValueError:
        rate_limit_retries = 3
    response_limit = max_response_bytes or 1_000_000
    total_attempts = 0
    attempted_providers: list[str] = []
    last_reason = "provider request failed"
    from app.ai_orchestration.health import FailureCategory, get_health_tracker
    from app.ai_orchestration.models import get_model_registry
    health_tracker = get_health_tracker()
    model_registry = get_model_registry()

    for provider_index, provider in enumerate(providers):
        max_attempts = rate_limit_retries + 1
        for attempt in range(max_attempts):
            total_attempts += 1
            if provider.name not in attempted_providers:
                attempted_providers.append(provider.name)
            try:
                content, usage, latency_ms = await _request_provider(
                    provider, system_prompt, user_prompt, timeout or http_timeout(), response_limit,
                    allow_top_level_array=effective_allow_array,
                )
                response_format = usage.pop("response_format", "json_object")
                finish_reason = usage.pop("finish_reason", None)
                if response_format == "text":
                    total_attempts += 1
                health_tracker.record_success(provider.name)
                prompt_toks = int(usage.get("prompt_tokens", 0) or 0)
                comp_toks = int(usage.get("completion_tokens", 0) or 0)
                cost = model_registry.estimate_cost(provider.model, provider.name, prompt_toks, comp_toks)

                _last_call.set({
                    "provider": provider.name, "model": provider.model,
                    "fallback_used": provider_index > 0 or bool(exclude_providers),
                    "attempt_count": total_attempts, "providers_attempted": attempted_providers.copy(),
                    "usage": usage, "latency_ms": latency_ms, "finish_reason": finish_reason,
                    "estimated_cost_usd": cost,
                    "model_configuration": {
                        "provider": provider.name, "model": provider.model,
                        "protocol": "openai_chat_completions", "temperature": 0,
                        "response_format": response_format, "request_timeout_seconds": timeout or http_timeout(),
                        "max_response_bytes": response_limit, "supports_json": provider.supports_json,
                    },
                })
                return content
            except AIReviewUnavailable as exc:
                category, retryable, retry_after = health_tracker.classify_exception(exc)
                health_tracker.record_failure(provider.name, category, str(exc), retry_after)
                last_reason = str(exc)
                if not retryable:
                    break
                is_rate_limit = (category == FailureCategory.RATE_LIMIT)
                allowed_retries = rate_limit_retries if is_rate_limit else retries
                if attempt < allowed_retries:
                    if is_rate_limit:
                        import random
                        jitter = random.uniform(0.1, 0.5)
                        if retry_after is not None and retry_after > 0:
                            delay = retry_after + jitter
                        else:
                            delay = (1.0 * (2 ** attempt)) + jitter
                        await asyncio.sleep(min(delay, 15.0))
                    else:
                        delay = retry_after if retry_after is not None else (0.2 * (2 ** attempt))
                        await asyncio.sleep(min(delay, 5.0))
                else:
                    break
    _last_call.set({"status": "unavailable", "providers_attempted": attempted_providers, "attempt_count": total_attempts})
    raise AIReviewUnavailable(f"Configured AI provider chain failed: {last_reason}")


async def complete_for_task(task: str, function, system_prompt: str, user_prompt: str, **kwargs) -> str:
    """Select a task-specific model while preserving existing mock/client call signatures."""
    token = _current_task.set(task)
    try:
        return await function(system_prompt, user_prompt, **kwargs)
    finally:
        _current_task.reset(token)


async def complete_validated_json(
    task: str, system_prompt: str, user_prompt: str, validator, *, completion_function=None, allow_top_level_array: bool = False, **kwargs
):
    """Validate each provider response and try an untried fallback on schema failure."""
    import inspect

    excluded: list[str] = []
    cumulative_attempts = 0
    cumulative_providers: list[str] = []
    configured = provider_registry(task)
    target_fn = completion_function or complete_json
    sig = inspect.signature(target_fn)
    has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

    for _ in range(max(1, len(configured))):
        clear_last_call_metadata()
        call_kwargs = dict(kwargs)
        if allow_top_level_array:
            call_kwargs["allow_top_level_array"] = True
        if excluded:
            call_kwargs["exclude_providers"] = excluded
        filtered_kwargs = call_kwargs if has_var_keyword else {k: v for k, v in call_kwargs.items() if k in sig.parameters}
        raw = await complete_for_task(
            task, target_fn, system_prompt, user_prompt,
            **filtered_kwargs,
        )
        metadata = last_call_metadata() or {}
        cumulative_attempts += int(metadata.get("attempt_count", 0) or 0)
        for name in metadata.get("providers_attempted", []):
            if isinstance(name, str) and name not in cumulative_providers:
                cumulative_providers.append(name)
        provider = metadata.get("provider")
        if isinstance(provider, str) and provider not in cumulative_providers:
            cumulative_providers.append(provider)
        annotate_last_call(
            attempt_count=cumulative_attempts,
            providers_attempted=cumulative_providers.copy(),
            fallback_used=bool(metadata.get("fallback_used")) or bool(excluded),
        )
        try:
            validated = validator(raw)
            annotate_last_call(
                task_name=task,
                response_schema_validated=True,
                system_prompt_sha256=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
                user_prompt_sha256=hashlib.sha256(user_prompt.encode("utf-8")).hexdigest(),
            )
            return raw, validated
        except (AIReviewUnavailable, ValueError, TypeError, ValidationError) as exc:
            annotate_last_call(task_name=task, response_schema_validated=False)
            metadata = last_call_metadata() or {}
            attempted = metadata.get("providers_attempted", [])
            newly_excluded = [name for name in attempted if isinstance(name, str) and name not in excluded]
            provider = metadata.get("provider")
            if isinstance(provider, str) and provider not in excluded and provider not in newly_excluded:
                newly_excluded.append(provider)
            if not newly_excluded:
                raise
            excluded.extend(newly_excluded)
            if len(excluded) >= len(configured):
                raise AIReviewUnavailable("All configured providers returned responses that failed task schema validation.") from exc
    raise AIReviewUnavailable("No provider returned a response matching the task schema.")
