"""Small HTTP client for GitHub's public REST API."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import github_token, http_timeout

"""Narrow HTTP boundary for GitHub's public REST API."""


class GitHubServiceError(RuntimeError):
    """A GitHub request failed or returned an unusable response."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code

API_ROOT = "https://api.github.com"


class GitHubAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


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
    token = token or github_token()
    if token:
        result["Authorization"] = f"Bearer {token}"
    return result


async def request_json(client: httpx.AsyncClient, path: str, *, params: dict[str, Any] | None = None, github_token: str | None = None) -> Any:
    if not path.startswith("/") or path.startswith("//") or ".." in path.split("/"):
        raise ValueError("GitHub API paths must be relative to the fixed API host.")
    try:
        response = await client.get(f"{API_ROOT}{path}", params=params, headers=headers(github_token))
    except httpx.TimeoutException as exc:
        raise GitHubAPIError("GitHub did not respond in time.") from exc
    except httpx.RequestError as exc:
        raise GitHubAPIError("GitHub is temporarily unavailable.") from exc
    if response.status_code >= 400:
        if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
            reset_at = response.headers.get("X-RateLimit-Reset")
            retry_after = response.headers.get("Retry-After")
            try:
                wait_until = datetime.fromtimestamp(int(reset_at), timezone.utc).strftime("%H:%M UTC")
                detail = f"GitHub API rate limit reached. Try again after {wait_until}."
            except (TypeError, ValueError, OverflowError, OSError):
                detail = f"GitHub API rate limit reached. Try again in {retry_after} seconds." if retry_after and retry_after.isdigit() else "GitHub API rate limit reached. Try again in about one minute."
            raise GitHubAPIError(detail, 429)
        message = "GitHub rejected the request."
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("message"), str):
                message = body["message"]
        except (ValueError, TypeError):
            pass
        raise GitHubAPIError(message, response.status_code)
    try:
        return response.json()
    except (ValueError, TypeError) as exc:
        raise GitHubAPIError("GitHub returned malformed JSON.", 502) from exc


def async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=http_timeout(), follow_redirects=False)
