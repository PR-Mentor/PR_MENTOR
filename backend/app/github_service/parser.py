"""Validate GitHub pull-request URLs and normalize API payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from app.github_service.client import GitHubServiceError

_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_PR_PATH = re.compile(
    r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)"
    r"(?:/(?:files|commits|checks|conversation))?/?$"
)


class PullRequestCoordinates(NamedTuple):
    owner: str
    repo: str
    number: int


def parse_pull_request_url(url: str) -> PullRequestCoordinates:
    """Accept only HTTPS URLs for a GitHub pull request."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Enter a GitHub pull request URL.")

    parsed = urlsplit(url.strip())
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username:
        raise ValueError("Only public github.com pull request URLs are supported.")

    match = _PR_PATH.fullmatch(parsed.path)
    if match is None:
        raise ValueError("Use a URL like https://github.com/owner/repo/pull/123.")

    owner, repo, number = match.groups()
    if not _SEGMENT.fullmatch(owner) or not _SEGMENT.fullmatch(repo):
        raise ValueError("The GitHub owner or repository name is invalid.")
    return PullRequestCoordinates(owner=owner, repo=repo, number=int(number))


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GitHubServiceError(f"GitHub returned malformed {label} data.")
    return value


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise GitHubServiceError(f"GitHub returned malformed {label} data.")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GitHubServiceError(f"GitHub returned malformed {label} data.")
    return value


def parse_pull_request_metadata(
    data: Any,
    coordinates: PullRequestCoordinates,
) -> dict[str, Any]:
    """Convert a GitHub pull request response to the agreed metadata shape."""
    pull_request = _mapping(data, "pull request")
    base = _mapping(pull_request.get("base"), "base branch")
    head = _mapping(pull_request.get("head"), "head branch")
    user = _mapping(pull_request.get("user"), "author")
    number = _integer(pull_request.get("number"), "pull request number")
    if number != coordinates.number:
        raise GitHubServiceError(
            "GitHub returned a different pull request number than requested."
        )

    body = pull_request.get("body")
    if body is not None and not isinstance(body, str):
        raise GitHubServiceError("GitHub returned malformed pull request body data.")

    return {
        "owner": coordinates.owner,
        "repo": coordinates.repo,
        "number": number,
        "title": _text(pull_request.get("title"), "pull request title"),
        "body": body,
        "state": _text(pull_request.get("state"), "pull request state"),
        "html_url": _text(pull_request.get("html_url"), "pull request URL"),
        "author": _text(user.get("login"), "pull request author"),
        "base_ref": _text(base.get("ref"), "base branch name"),
        "base_sha": _text(base.get("sha"), "base commit"),
        "head_ref": _text(head.get("ref"), "head branch name"),
        "head_sha": _text(head.get("sha"), "head commit"),
        "changed_files": _integer(pull_request.get("changed_files"), "changed file count"),
    }


def parse_pull_request_file(data: Any) -> dict[str, Any]:
    """Convert one GitHub files API entry to the agreed file shape."""
    file_data = _mapping(data, "changed file")
    patch = file_data.get("patch")
    if patch is not None and not isinstance(patch, str):
        raise GitHubServiceError("GitHub returned malformed file patch data.")

    previous_filename = file_data.get("previous_filename")
    if previous_filename is not None and not isinstance(previous_filename, str):
        raise GitHubServiceError("GitHub returned malformed previous filename data.")

    return {
        "filename": _text(file_data.get("filename"), "changed filename"),
        "previous_filename": previous_filename,
        "status": _text(file_data.get("status"), "changed file status"),
        "additions": _integer(file_data.get("additions"), "file additions"),
        "deletions": _integer(file_data.get("deletions"), "file deletions"),
        "changes": _integer(file_data.get("changes"), "file change count"),
        "sha": _text(file_data.get("sha"), "changed file SHA"),
        "patch": patch,
        "source_content": None,
        "content_unavailable_reason": (
            None
            if patch is not None
            else "GitHub did not provide a text patch for this file."
        ),
    }

"""Parse public GitHub pull-request URLs without making network requests."""

import re
from urllib.parse import urlsplit


class InvalidPullRequestURL(ValueError):
    """The submitted value is not a supported GitHub pull request URL."""


_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def parse_pull_request_url(value: str) -> tuple[str, str, int]:
    if not isinstance(value, str) or not value.strip():
        raise InvalidPullRequestURL("A pull request URL is required.")
    try:
        parts = urlsplit(value.strip())
        if parts.scheme != "https" or parts.hostname != "github.com" or parts.username or parts.password or parts.port:
            raise ValueError
        segments = parts.path.strip("/").split("/")
        if len(segments) != 4 or segments[2].lower() != "pull":
            raise ValueError
        owner, repo, _, raw_number = segments
        if not _SEGMENT.fullmatch(owner) or not _SEGMENT.fullmatch(repo) or owner in {".", ".."} or repo in {".", ".."}:
            raise ValueError
        if not raw_number.isascii() or not raw_number.isdigit() or int(raw_number) < 1:
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise InvalidPullRequestURL("Use an https://github.com/{owner}/{repo}/pull/{number} URL.") from exc
    return owner, repo.removesuffix(".git"), int(raw_number)
