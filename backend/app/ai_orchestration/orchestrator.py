"""Centralized AI Orchestrator coordinating routing, resilience, validation, and observability."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

import httpx
from pydantic import ValidationError

from app.ai_orchestration.health import FailureCategory, ProviderHealthTracker, get_health_tracker
from app.ai_orchestration.models import ModelRegistry, get_model_registry
from app.ai_orchestration.router import HybridRouter, RoutedCandidate, get_hybrid_router
from app.ai_orchestration.tasks import TaskDefinition, TaskRegistry, get_task_registry
from app.ai_review.providers import ProviderConfig

logger = logging.getLogger("pr_mentor.ai_orchestration")

_current_execution_record: ContextVar[dict[str, Any] | None] = ContextVar("pr_mentor_execution_record", default=None)
_current_task_var: ContextVar[str | None] = ContextVar("pr_mentor_active_task", default=None)


class AIReviewUnavailable(RuntimeError):
    """AI review or generation is unavailable, misconfigured, or returned invalid output."""


@dataclass
class ExecutionRecord:
    task_id: str
    provider: str
    model: str
    status: str  # "success" | "failed" | "fallback_success" | "unavailable"
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float | None = None
    retry_count: int = 0
    fallback_used: bool = False
    providers_attempted: list[str] = field(default_factory=list)
    failure_category: str | None = None
    failure_reason: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    model_configuration: dict[str, Any] = field(default_factory=dict)
    response_schema_validated: bool = False

    def to_safe_dict(self) -> dict[str, Any]:
        """Return audit-safe telemetry without keys or raw secret content."""
        data = asdict(self)
        # Redact any accidental credential leak
        data["model_configuration"].pop("api_key", None)
        return data


class AIOrchestrator:
    """Centralized orchestration engine for PR Mentor AI workflows."""

    def __init__(
        self,
        router: HybridRouter | None = None,
        task_registry: TaskRegistry | None = None,
        model_registry: ModelRegistry | None = None,
        health_tracker: ProviderHealthTracker | None = None,
    ) -> None:
        self._router = router or get_hybrid_router()
        self._task_registry = task_registry or get_task_registry()
        self._model_registry = model_registry or get_model_registry()
        self._health_tracker = health_tracker or get_health_tracker()
        self._last_instance_record: dict[str, Any] | None = None

    def get_last_record(self) -> dict[str, Any] | None:
        val = _current_execution_record.get() or self._last_instance_record
        return dict(val) if val else None

    def clear_last_record(self) -> None:
        _current_execution_record.set(None)
        self._last_instance_record = None

    def annotate_last_record(self, **kwargs: Any) -> None:
        rec = self.get_last_record()
        if rec is not None:
            rec.update(kwargs)
            _current_execution_record.set(rec)
            self._last_instance_record = dict(rec)

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        task: str | None = None,
        timeout: float | None = None,
        max_response_bytes: int | None = None,
        exclude_providers: list[str] | None = None,
    ) -> str:
        active_task_id = task or _current_task_var.get() or "code_review"
        task_def = self._task_registry.get(active_task_id)

        # Validate arguments
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0
        ):
            raise ValueError("timeout must be a positive number")
        if max_response_bytes is not None and (
            isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes < 1
        ):
            raise ValueError("max_response_bytes must be a positive integer")

        effective_timeout = timeout or task_def.default_timeout_seconds
        effective_limit = max_response_bytes or task_def.max_response_bytes

        candidates = self._router.route_candidates(task_def.task_id, exclude_providers)
        if not candidates:
            rec = ExecutionRecord(
                task_id=task_def.task_id,
                provider="none",
                model="none",
                status="unavailable",
                failure_reason="No eligible AI provider configured or available for this task.",
                failure_category=FailureCategory.CONFIGURATION_ERROR.value,
            )
            _current_execution_record.set(rec.to_safe_dict())
            raise AIReviewUnavailable("AI review is unavailable because LLM_API_KEY and LLM_MODEL are not configured.")

        configured_retries = max(0, min(int(os.getenv("LLM_PROVIDER_RETRIES", str(task_def.default_max_retries))), 2))
        attempted_providers: list[str] = []
        total_attempts = 0
        last_exception_msg = "All provider candidates failed"

        for candidate_index, candidate in enumerate(candidates):
            provider = candidate.provider
            model_desc = candidate.model
            attempted_providers.append(provider.name)

            for attempt in range(configured_retries + 1):
                total_attempts += 1
                try:
                    content, usage, latency_ms = await self._send_request(
                        provider, system_prompt, user_prompt, effective_timeout, effective_limit,
                    )

                    # Success: update provider health tracker
                    self._health_tracker.record_success(provider.name)

                    # Extract token counts
                    prompt_toks = int(usage.get("prompt_tokens", 0) or 0)
                    comp_toks = int(usage.get("completion_tokens", 0) or 0)
                    total_toks = int(usage.get("total_tokens", prompt_toks + comp_toks) or (prompt_toks + comp_toks))

                    # Calculate cost estimate if pricing metadata exists
                    cost_est = self._model_registry.estimate_cost(
                        provider.model, provider.name, prompt_toks, comp_toks
                    )

                    is_fallback = candidate_index > 0 or bool(exclude_providers)
                    status_str = "fallback_success" if is_fallback else "success"

                    rec = ExecutionRecord(
                        task_id=task_def.task_id,
                        provider=provider.name,
                        model=provider.model,
                        status=status_str,
                        latency_ms=latency_ms,
                        prompt_tokens=prompt_toks,
                        completion_tokens=comp_toks,
                        total_tokens=total_toks,
                        estimated_cost_usd=cost_est,
                        retry_count=attempt,
                        fallback_used=is_fallback,
                        providers_attempted=attempted_providers.copy(),
                        model_configuration={
                            "provider": provider.name,
                            "model": provider.model,
                            "protocol": "openai_chat_completions",
                            "temperature": 0,
                            "response_format": "json_object",
                            "request_timeout_seconds": effective_timeout,
                            "max_response_bytes": effective_limit,
                            "supports_json": provider.supports_json,
                        },
                    )
                    safe_rec = rec.to_safe_dict()
                    safe_rec["usage"] = usage
                    safe_rec["attempt_count"] = total_attempts
                    _current_execution_record.set(safe_rec)
                    self._last_instance_record = dict(safe_rec)
                    return content

                except Exception as exc:
                    category, retryable, retry_after = self._health_tracker.classify_exception(exc)
                    self._health_tracker.record_failure(
                        provider.name, category, reason=str(exc), retry_after_seconds=retry_after
                    )
                    last_exception_msg = str(exc)

                    # If failure is non-retryable (e.g. permanent auth 401/403), skip remaining retries for this provider
                    if not retryable or attempt >= configured_retries:
                        break

                    # Apply backoff before retrying same provider
                    backoff_delay = retry_after if retry_after is not None else (0.2 * (2 ** attempt) + random.uniform(0.05, 0.15))
                    await asyncio.sleep(min(backoff_delay, 5.0))

        # If all candidates failed
        rec = ExecutionRecord(
            task_id=task_def.task_id,
            provider="none",
            model="none",
            status="unavailable",
            retry_count=total_attempts,
            providers_attempted=attempted_providers.copy(),
            failure_reason=last_exception_msg,
            failure_category=FailureCategory.UNKNOWN.value,
        )
        safe_rec = rec.to_safe_dict()
        safe_rec["attempt_count"] = total_attempts
        _current_execution_record.set(safe_rec)
        self._last_instance_record = dict(safe_rec)
        raise AIReviewUnavailable(f"Configured AI provider chain failed: {last_exception_msg}")

    async def complete_validated(
        self,
        task: str,
        system_prompt: str,
        user_prompt: str,
        validator: Callable[[str], Any],
        *,
        completion_function: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> tuple[str, Any]:
        excluded: list[str] = []
        candidates = self._router.route_candidates(task)
        max_hops = max(1, len(candidates))
        cumulative_attempts = 0
        cumulative_providers: list[str] = []

        for _ in range(max_hops):
            self.clear_last_record()
            complete_fn = completion_function or self.complete
            token = _current_task_var.set(task)
            try:
                raw = await complete_fn(
                    system_prompt,
                    user_prompt,
                    task=task,
                    exclude_providers=excluded if excluded else None,
                    **kwargs,
                )
            finally:
                _current_task_var.reset(token)

            metadata = self.get_last_record() or {}
            cumulative_attempts += int(metadata.get("attempt_count", 0) or 0)
            for name in metadata.get("providers_attempted", []):
                if isinstance(name, str) and name not in cumulative_providers:
                    cumulative_providers.append(name)
            prov = metadata.get("provider")
            if isinstance(prov, str) and prov not in cumulative_providers and prov != "none":
                cumulative_providers.append(prov)

            self.annotate_last_record(
                attempt_count=cumulative_attempts,
                providers_attempted=cumulative_providers.copy(),
                fallback_used=bool(metadata.get("fallback_used")) or bool(excluded),
            )

            try:
                validated = validator(raw)
                self.annotate_last_record(
                    task_name=task,
                    response_schema_validated=True,
                    system_prompt_sha256=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
                    user_prompt_sha256=hashlib.sha256(user_prompt.encode("utf-8")).hexdigest(),
                )
                return raw, validated
            except (AIReviewUnavailable, ValueError, TypeError, ValidationError) as exc:
                self.annotate_last_record(task_name=task, response_schema_validated=False)
                # Provider returned schema-violating output: exclude it and try next candidate
                current_meta = self.get_last_record() or {}
                attempted = current_meta.get("providers_attempted", [])
                newly_excluded = [n for n in attempted if isinstance(n, str) and n not in excluded and n != "none"]
                if not newly_excluded and prov and prov != "none" and prov not in excluded:
                    newly_excluded.append(prov)

                if not newly_excluded:
                    raise
                excluded.extend(newly_excluded)
                if len(excluded) >= max_hops:
                    raise AIReviewUnavailable(
                        "All configured providers returned responses that failed task schema validation."
                    ) from exc

        raise AIReviewUnavailable("No provider returned a response matching the task schema.")

    async def _send_request(
        self,
        provider: ProviderConfig,
        system_prompt: str,
        user_prompt: str,
        timeout: float,
        max_response_bytes: int,
    ) -> tuple[str, dict[str, int], int]:
        from app.ai_review.client import _request_provider
        return await _request_provider(provider, system_prompt, user_prompt, timeout, max_response_bytes)


# Global singleton
_global_orchestrator = AIOrchestrator()


def get_ai_orchestrator() -> AIOrchestrator:
    return _global_orchestrator
