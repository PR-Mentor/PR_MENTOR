"""Validate GitHub pull-request URLs and normalize API payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from app.github_service.client import GitHubServiceError

_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_PR_PATH = re.compile(
    r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)"
    r"(?:/(?:files|commits|checks|conversation))?/?$"
)
_FULL_SHA = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


class InvalidPullRequestURL(ValueError):
    """The submitted value is not a supported GitHub pull request URL."""


class PullRequestCoordinates(NamedTuple):
    owner: str
    repo: str
    number: int


def parse_pull_request_url(value: str) -> PullRequestCoordinates:
    """Parse canonical pull-request URLs and GitHub's common PR tabs."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidPullRequestURL("A pull request URL is required.")

    try:
        parts = urlsplit(value.strip())
        if (
            parts.scheme != "https"
            or parts.hostname != "github.com"
            or parts.username is not None
            or parts.password is not None
            or parts.port is not None
        ):
            raise ValueError

        match = _PR_PATH.fullmatch(parts.path)
        if match is None:
            raise ValueError

        owner, repo, raw_number = match.groups()
        if (
            not _SEGMENT.fullmatch(owner)
            or not _SEGMENT.fullmatch(repo)
            or owner in {".", ".."}
            or repo in {".", ".."}
            or not raw_number.isascii()
            or not raw_number.isdigit()
            or int(raw_number) < 1
        ):
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise InvalidPullRequestURL(
            "Use an https://github.com/{owner}/{repo}/pull/{number} URL."
        ) from exc

    return PullRequestCoordinates(
        owner=owner,
        repo=repo.removesuffix(".git"),
        number=int(raw_number),
    )


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

    base_repository = _mapping(base.get("repo"), "base repository")
    base_owner = _mapping(base_repository.get("owner"), "base repository owner")
    if (
        _text(base_owner.get("login"), "base repository owner").casefold() != coordinates.owner.casefold()
        or _text(base_repository.get("name"), "base repository name").casefold() != coordinates.repo.casefold()
    ):
        raise GitHubServiceError("GitHub returned a pull request for a different base repository.")
    base_sha = _text(base.get("sha"), "base commit")
    head_sha = _text(head.get("sha"), "head commit")
    if not _FULL_SHA.fullmatch(base_sha) or not _FULL_SHA.fullmatch(head_sha):
        raise GitHubServiceError("GitHub returned an invalid pull request commit SHA.")
    html_url = _text(pull_request.get("html_url"), "pull request URL")
    expected_url = f"https://github.com/{coordinates.owner}/{coordinates.repo}/pull/{coordinates.number}"
    if html_url.rstrip("/").casefold() != expected_url.casefold():
        raise GitHubServiceError("GitHub returned a noncanonical pull request URL.")
    title = _text(pull_request.get("title"), "pull request title")
    author = _text(user.get("login"), "pull request author")
    base_ref = _text(base.get("ref"), "base branch name")
    head_ref = _text(head.get("ref"), "head branch name")
    if any(len(value) > 500 for value in (title, author, base_ref, head_ref)) or (body and len(body) > 100_000):
        raise GitHubServiceError("GitHub pull request metadata exceeded a safety limit.")

    return {
        "owner": coordinates.owner,
        "repo": coordinates.repo,
        "number": number,
        "title": title,
        "body": body,
        "state": _text(pull_request.get("state"), "pull request state"),
        "html_url": html_url,
        "author": author,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "head_ref": head_ref,
        "head_sha": head_sha,
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
