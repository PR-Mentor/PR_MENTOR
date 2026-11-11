"""Narrow HTTP boundary for GitHub's public REST API."""

import asyncio
import copy
import logging
import math
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.config import github_token, http_timeout

API_ROOT = "https://api.github.com"
MAX_RETRIES = 2
BACKOFF_BASE_SECONDS = 0.25
BACKOFF_MAX_SECONDS = 2.0
CACHE_TTL_SECONDS = 60.0
CACHE_MAX_ENTRIES = 256
_response_cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, Any]] = {}
_inflight_requests: dict[tuple[str, tuple[tuple[str, str], ...]], asyncio.Task[Any]] = {}
_logger = logging.getLogger(__name__)


class GitHubAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, *, rate_limit: dict[str, str] | None = None, kind: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.rate_limit = rate_limit or {}
        self.kind = kind


def headers() -> dict[str, str]:
    result = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = github_token()
    if token:
        result["Authorization"] = f"Bearer {token}"
    return result


async def request_json(
    client: httpx.AsyncClient,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    use_cache: bool = True,
) -> Any:
    if not path.startswith("/") or path.startswith("//") or ".." in path.split("/"):
        raise ValueError("GitHub API paths must be relative to the fixed API host.")
    if not use_cache:
        # Freshness checks must not use either a cached response or an
        # in-flight request that may have started before the current check.
        return await _request_json_uncached(client, path, params, None)
    cache_key = (path, tuple(sorted((str(k), str(v)) for k, v in (params or {}).items())))
    now = time.monotonic()
    cached = _response_cache.get(cache_key)
    if cached and cached[0] > now:
        return copy.deepcopy(cached[1])
    _response_cache.pop(cache_key, None)

    task = _inflight_requests.get(cache_key)
    if task is None:
        task = asyncio.create_task(_request_json_uncached(client, path, params, cache_key))
        _inflight_requests[cache_key] = task
        task.add_done_callback(lambda completed, key=cache_key: _cleanup_inflight(key, completed))
    # An individual request cancellation must not cancel work shared by other
    # callers. The HTTP client's timeout bounds the shared request lifetime.
    try:
        return copy.deepcopy(await asyncio.shield(task))
    except asyncio.CancelledError:
        # Keep the caller's client context alive until the shared operation
        # finishes; otherwise its context manager could close a client still
        # being used by other waiters. The configured HTTP timeout bounds this.
        try:
            await asyncio.shield(task)
        except (asyncio.CancelledError, GitHubAPIError, httpx.HTTPError, ValueError, TypeError, RuntimeError) as exc:
            _logger.debug("Shared GitHub request finished after cancellation (%s).", type(exc).__name__)
        raise


async def _request_json_uncached(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None,
    cache_key: tuple[str, tuple[tuple[str, str], ...]] | None,
) -> Any:
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.get(f"{API_ROOT}{path}", params=params, headers=headers())
        except httpx.TimeoutException as exc:
            raise GitHubAPIError("GitHub did not respond in time.") from exc
        except httpx.RequestError as exc:
            raise GitHubAPIError("GitHub is temporarily unavailable.") from exc
        if response.status_code >= 400:
            details = _rate_limit_headers(response)
            message = _error_message(response)
            kind = _rate_limit_kind(response, details, message)
            if kind:
                retry_at = _retry_at(details)
                if attempt < MAX_RETRIES:
                    delay = _retry_delay(attempt, details, kind)
                    # Do not wait beyond the bounded retry budget. Return the
                    # provider's retry estimate so callers can retry later.
                    if delay is not None:
                        await asyncio.sleep(delay)
                        continue
                friendly = "GitHub primary rate limit reached." if kind == "primary" else "GitHub secondary rate limit reached."
                if retry_at:
                    friendly += f" Try again after {retry_at}."
                elif details.get("retry-after"):
                    retry_after = _retry_after_seconds(details["retry-after"])
                    if retry_after is not None:
                        friendly += f" Retry after {retry_after:g} seconds."
                    else:
                        retry_at = _retry_after_date(details["retry-after"])
                        if retry_at:
                            friendly += f" Try again after {retry_at}."
                raise GitHubAPIError(friendly, 429, rate_limit=details, kind=kind)
            raise GitHubAPIError(message, response.status_code, rate_limit=details)
        try:
            result = response.json()
        except (ValueError, TypeError) as exc:
            raise GitHubAPIError("GitHub returned malformed JSON.", 502) from exc
        if cache_key is not None:
            if len(_response_cache) >= CACHE_MAX_ENTRIES:
                _response_cache.pop(next(iter(_response_cache)))
            _response_cache[cache_key] = (time.monotonic() + CACHE_TTL_SECONDS, copy.deepcopy(result))
        return result
    raise GitHubAPIError("GitHub API request could not be completed.", 502)


def _cleanup_inflight(key: tuple[str, tuple[tuple[str, str], ...]], task: asyncio.Task[Any]) -> None:
    if _inflight_requests.get(key) is task:
        _inflight_requests.pop(key, None)
    # Retrieve exceptions even if every waiter was cancelled, avoiding an
    # unobserved-task warning. Awaiting callers still receive the exception.
    if not task.cancelled():
        task.exception()


def _rate_limit_headers(response: httpx.Response) -> dict[str, str]:
    names = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-resource", "x-ratelimit-reset", "retry-after")
    return {name: response.headers[name] for name in names if name in response.headers}


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            # Never reflect credentials, even if an upstream error body does.
            message = body["message"]
            token = github_token()
            return message.replace(token, "[redacted]") if token else message
    except (ValueError, TypeError):
        pass
    return "GitHub rejected the request."


def _rate_limit_kind(response: httpx.Response, details: dict[str, str], message: str) -> str | None:
    normalized = message.lower()
    if response.status_code == 429 or "secondary rate limit" in normalized or "abuse detection" in normalized:
        return "secondary"
    if response.status_code == 403 and "retry-after" in details:
        return "secondary"
    if response.status_code == 403 and details.get("x-ratelimit-remaining") == "0":
        return "primary"
    return None


def _retry_at(details: dict[str, str]) -> str | None:
    try:
        reset = int(details["x-ratelimit-reset"])
        return datetime.fromtimestamp(reset, UTC).isoformat().replace("+00:00", "Z")
    except (KeyError, ValueError, OverflowError, OSError):
        return None


def _retry_delay(attempt: int, details: dict[str, str], kind: str) -> float | None:
    delay = BACKOFF_BASE_SECONDS * (2 ** attempt)
    if kind == "secondary" and details.get("retry-after"):
        retry_after = _retry_after_seconds(details["retry-after"])
        if retry_after is None:
            retry_at = _retry_after_date(details["retry-after"])
            if retry_at:
                retry_after = (retry_at - datetime.now(UTC)).total_seconds()
        if retry_after is not None:
            delay = max(delay, retry_after)
    elif kind == "primary":
        retry_at = _retry_at(details)
        if retry_at:
            reset_delay = (datetime.fromisoformat(retry_at) - datetime.now(UTC)).total_seconds()
            delay = max(delay, reset_delay)
    return delay if 0 <= delay <= BACKOFF_MAX_SECONDS else None


def _retry_after_seconds(value: str) -> float | None:
    try:
        seconds = float(value.strip())
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (ValueError, AttributeError):
        return None


def _retry_after_date(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=http_timeout(), follow_redirects=False)
