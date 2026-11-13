"""Narrow HTTP boundary for GitHub's public REST API."""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import os
import time
from collections.abc import Mapping
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


class GitHubServiceError(RuntimeError):
    """A GitHub request failed or returned an unusable response."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        rate_limit: dict[str, str] | None = None,
        kind: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.rate_limit = rate_limit or {}
        self.kind = kind


class GitHubClient:
    """Fetch JSON from the fixed api.github.com host.

    A caller may inject an httpx client for tests. ``GITHUB_TOKEN`` is optional
    and only increases GitHub's rate limit; its value is never included in
    errors or logs.
    """

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        token: str | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._http_client = http_client
        self._token = os.getenv("GITHUB_TOKEN", "") if token is None else token
        self._timeout_seconds = timeout_seconds

    def get_json(self, path: str, *, params: Mapping[str, int] | None = None) -> Any:
        if not path.startswith("/") or ".." in path.split("/"):
            raise ValueError("GitHub API paths must be repository-relative API paths.")

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            if self._http_client is not None:
                response = self._http_client.get(
                    f"https://api.github.com{path}",
                    params=params,
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
                response.raise_for_status()
                return response.json()

            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.get(
                    f"https://api.github.com{path}", params=params, headers=headers
                )
                response.raise_for_status()
                return response.json()
        except httpx.TimeoutException as exc:
            raise GitHubServiceError("GitHub did not respond before the request timed out.") from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code == 404:
                message = "The pull request was not found or is not publicly accessible."
            elif status_code == 403:
                message = "GitHub denied the request or its API rate limit was reached."
            else:
                message = f"GitHub returned HTTP {status_code}."
            raise GitHubServiceError(message, status_code=status_code) from exc
        except httpx.RequestError as exc:
            raise GitHubServiceError("Could not connect to GitHub.") from exc
        except ValueError as exc:
            raise GitHubServiceError("GitHub returned invalid JSON.") from exc


def headers(token: str | None = None) -> dict[str, str]:
    result = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    active_token = token or github_token()
    if active_token:
        result["Authorization"] = f"Bearer {active_token}"
    return result


def async_client(token: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API_ROOT, timeout=http_timeout(), headers=headers(token))


async def request_json(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    use_cache: bool = True,
) -> Any:
    cache_key: tuple[str, tuple[tuple[str, str], ...]] | None = None
    if use_cache:
        normalized_params = tuple(sorted((str(k), str(v)) for k, v in (params or {}).items()))
        cache_key = (path, normalized_params)
        cached = _response_cache.get(cache_key)
        if cached is not None:
            expires_at, data = cached
            if time.monotonic() < expires_at:
                return copy.deepcopy(data)
            _response_cache.pop(cache_key, None)
        task = _inflight_requests.get(cache_key)
        if task is not None:
            # Use shield() so a cancellation of this coroutine does not cancel
            # the shared inflight task that other waiters depend on.
            return copy.deepcopy(await asyncio.shield(task))

    task = asyncio.create_task(_execute_request(client, path, params, cache_key=cache_key))
    if cache_key is not None:
        _inflight_requests[cache_key] = task
        task.add_done_callback(lambda done: _cleanup_inflight(cache_key, done))
    # Shield the shared task from cancellation so other waiters are not disrupted.
    # If this caller is cancelled, wait for the task to finish before propagating.
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _execute_request(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None,
    *,
    cache_key: tuple[str, tuple[tuple[str, str], ...]] | None = None,
) -> Any:
    # Resolve relative paths — tests may inject a client without a base_url.
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise GitHubAPIError("GitHub API request timed out.", 504) from exc
        except httpx.RequestError as exc:
            raise GitHubAPIError("GitHub API could not be reached.", 503) from exc

        if response.status_code == 404:
            raise GitHubAPIError("The requested GitHub repository or pull request was not found.", 404)
        if response.status_code == 401:
            raise GitHubAPIError("GitHub rejected the configured API credential.", 401)
        if response.status_code in {403, 429}:
            details = _rate_limit_headers(response)
            message = _error_message(response)
            kind = _rate_limit_kind(response, details, message)
            if kind is not None and attempt < MAX_RETRIES:
                delay = _retry_delay(attempt, details, kind)
                if delay is not None:
                    _logger.warning("GitHub rate limit encountered (%s); backing off for %.2fs", kind, delay)
                    await asyncio.sleep(delay)
                    continue
            if kind is not None:
                if kind == "primary":
                    friendly = "GitHub primary rate limit exceeded."
                else:
                    friendly = "GitHub API rate limit exceeded."
                if details.get("retry-after"):
                    retry_after = _retry_after_seconds(details["retry-after"])
                    if retry_after is not None:
                        friendly += f" Retry after {retry_after:g} seconds."
                    else:
                        retry_at = _retry_after_date(details["retry-after"])
                        if retry_at:
                            friendly += f" Retry after {retry_at}."
                elif kind == "primary":
                    # Use x-ratelimit-reset for primary rate limit reset time
                    retry_at = _retry_at(details)
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
    if not task.cancelled():
        task.exception()


def _rate_limit_headers(response: httpx.Response) -> dict[str, str]:
    names = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-resource", "x-ratelimit-reset", "retry-after")
    return {name: response.headers[name] for name in names if name in response.headers}


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            message = body["message"]
            token = github_token()
            if token and token in message:
                message = message.replace(token, "[redacted]")
            auth_header = (
                response.request.headers.get("authorization")
                if hasattr(response, "request") and response.request
                else None
            )
            if auth_header and auth_header.startswith("Bearer "):
                req_token = auth_header[7:].strip()
                if req_token and req_token in message:
                    message = message.replace(req_token, "[redacted]")
            return message
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
