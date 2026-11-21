"""Lightweight provider health tracking, failure classification, and rate-limit cooldowns."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx


class FailureCategory(str, Enum):
    TRANSIENT_NETWORK = "transient_network"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    AUTHENTICATION_ERROR = "authentication_error"
    SCHEMA_VALIDATION_ERROR = "schema_validation_error"
    SIZE_LIMIT_EXCEEDED = "size_limit_exceeded"
    CONFIGURATION_ERROR = "configuration_error"
    UNKNOWN = "unknown"


@dataclass
class ProviderHealthState:
    provider_name: str
    consecutive_failures: int = 0
    last_failure_category: FailureCategory | None = None
    last_failure_reason: str = ""
    cooldown_until: float = 0.0  # monotonic timestamp
    last_successful_call: float = 0.0

    @property
    def is_cooling_down(self) -> bool:
        return time.monotonic() < self.cooldown_until

    @property
    def remaining_cooldown_seconds(self) -> float:
        remaining = self.cooldown_until - time.monotonic()
        return max(0.0, round(remaining, 2))


class ProviderHealthTracker:
    """In-memory provider health monitor for intelligent routing and cooldowns."""

    def __init__(self) -> None:
        self._states: dict[str, ProviderHealthState] = {}

    def get_state(self, provider_name: str) -> ProviderHealthState:
        normalized = provider_name.strip().lower()
        if normalized not in self._states:
            self._states[normalized] = ProviderHealthState(provider_name=normalized)
        return self._states[normalized]

    def is_available(self, provider_name: str) -> bool:
        """Return True if provider is healthy or cooldown has expired."""
        state = self.get_state(provider_name)
        return not state.is_cooling_down

    def record_success(self, provider_name: str) -> None:
        state = self.get_state(provider_name)
        state.consecutive_failures = 0
        state.cooldown_until = 0.0
        state.last_failure_category = None
        state.last_failure_reason = ""
        state.last_successful_call = time.monotonic()

    def record_failure(
        self,
        provider_name: str,
        category: FailureCategory,
        reason: str = "",
        retry_after_seconds: float | None = None,
    ) -> None:
        state = self.get_state(provider_name)
        state.consecutive_failures += 1
        state.last_failure_category = category
        state.last_failure_reason = reason

        now = time.monotonic()
        if category == FailureCategory.RATE_LIMIT:
            # Respect explicit Retry-After if provided, else exponential cooldown capped at 60s
            duration = retry_after_seconds if retry_after_seconds is not None else min(60.0, 2.0 ** min(state.consecutive_failures, 5))
            state.cooldown_until = now + duration
        elif category in (FailureCategory.SERVER_ERROR, FailureCategory.TRANSIENT_NETWORK):
            # Short cooldown for transient 5xx or connection drops
            duration = min(30.0, 1.0 * min(state.consecutive_failures, 4))
            state.cooldown_until = now + duration
        elif category == FailureCategory.AUTHENTICATION_ERROR:
            # Longer cooldown for permanent auth failures
            state.cooldown_until = now + 300.0
        else:
            state.cooldown_until = now + 1.0

    def reset(self) -> None:
        self._states.clear()

    @staticmethod
    def classify_exception(exc: Exception) -> tuple[FailureCategory, bool, float | None]:
        """Classify exception into category, retryability, and optional retry-after."""
        target_exc = exc.__cause__ if exc.__cause__ is not None else exc
        if isinstance(target_exc, httpx.TimeoutException):
            return FailureCategory.TRANSIENT_NETWORK, True, None
        if isinstance(target_exc, (httpx.ConnectError, httpx.NetworkError)):
            return FailureCategory.TRANSIENT_NETWORK, True, None
        if isinstance(target_exc, httpx.HTTPStatusError):
            code = target_exc.response.status_code
            if code == 429:
                retry_after: float | None = None
                raw_header = target_exc.response.headers.get("Retry-After")
                if raw_header:
                    try:
                        retry_after = float(raw_header)
                    except ValueError:
                        pass
                return FailureCategory.RATE_LIMIT, True, retry_after
            if code in (401, 403):
                return FailureCategory.AUTHENTICATION_ERROR, False, None
            if code == 413:
                return FailureCategory.SIZE_LIMIT_EXCEEDED, False, None
            if 500 <= code <= 599:
                return FailureCategory.SERVER_ERROR, True, None
            return FailureCategory.UNKNOWN, False, None

        msg = str(exc).lower()
        if "http 429" in msg or "rate limit" in msg:
            return FailureCategory.RATE_LIMIT, True, None
        if "http 401" in msg or "http 403" in msg or "unauthorized" in msg or "forbidden" in msg:
            return FailureCategory.AUTHENTICATION_ERROR, False, None
        if any(f"http 50{i}" in msg for i in range(10)):
            return FailureCategory.SERVER_ERROR, True, None
        if "timeout" in msg or "timed out" in msg:
            return FailureCategory.TRANSIENT_NETWORK, True, None
        if "size limit" in msg:
            return FailureCategory.SIZE_LIMIT_EXCEEDED, False, None
        return FailureCategory.UNKNOWN, True, None


# Global singleton
_global_health_tracker = ProviderHealthTracker()


def get_health_tracker() -> ProviderHealthTracker:
    return _global_health_tracker
