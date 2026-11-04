"""Build the agreed ingestion payload for a public GitHub pull request."""

from __future__ import annotations

from typing import Any

from app.github_service.client import GitHubClient
from app.github_service.files import fetch_pull_request_files
from app.github_service.parser import (
    parse_pull_request_file,
    parse_pull_request_metadata,
    parse_pull_request_url,
)


def ingest_pull_request(
    url: str,
    *,
    client: GitHubClient | None = None,
    max_file_pages: int = 10,
) -> dict[str, Any]:
    """Fetch PR metadata and changed-file patches without checking out code."""
    coordinates = parse_pull_request_url(url)
    active_client = client or GitHubClient()
    api_prefix = f"/repos/{coordinates.owner}/{coordinates.repo}"

    pull_request_data = active_client.get_json(
        f"{api_prefix}/pulls/{coordinates.number}"
    )
    metadata = parse_pull_request_metadata(pull_request_data, coordinates)
    file_data, files_truncated = fetch_pull_request_files(
        active_client,
        coordinates.owner,
        coordinates.repo,
        coordinates.number,
        max_pages=max_file_pages,
    )

    return {
        "pull_request": metadata,
        "files": [parse_pull_request_file(file) for file in file_data],
        "files_truncated": files_truncated,
    }

 main

"""Fetch bounded pull-request metadata and changed-file pages from GitHub."""

import base64
import binascii
from urllib.parse import quote

import httpx

from app.github_service.client import async_client, request_json
from app.github_service.parser import parse_pull_request_url
from app.schemas.github import ChangedFile, PullRequestBundle, PullRequestMetadata

MAX_FILE_PAGES = 10
FILES_PER_PAGE = 100
MAX_FILES = MAX_FILE_PAGES * FILES_PER_PAGE
MAX_SOURCE_BYTES = 100_000


async def fetch_pull_request(url: str) -> PullRequestBundle:
    owner, repo, number = parse_pull_request_url(url)
    async with async_client() as client:
        raw = await request_json(client, f"/repos/{owner}/{repo}/pulls/{number}")
        if not isinstance(raw, dict):
            raise ValueError("GitHub returned an invalid pull request response.")
        base = raw.get("base") if isinstance(raw.get("base"), dict) else {}
        head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        canonical_url = f"https://github.com/{owner}/{repo}/pull/{number}"
        metadata = PullRequestMetadata(
            repository_owner=owner,
            repository_name=repo,
            repository_url=f"https://github.com/{owner}/{repo}",
            pull_request_number=number,
            pull_request_url=canonical_url,
            title=_required_str(raw.get("title"), "title"),
            description=_nullable_str(raw.get("body")),
            author=_nullable_str(user.get("login")),
            base_branch=_nullable_str(base.get("ref")),
            head_branch=_nullable_str(head.get("ref")),
            head_sha=_nullable_str(head.get("sha")),
            state=_required_str(raw.get("state"), "state"),
            changed_files=_required_nonnegative(raw.get("changed_files"), "changed_files"),
            additions=_required_nonnegative(raw.get("additions"), "additions"),
            deletions=_required_nonnegative(raw.get("deletions"), "deletions"),
        )
        files: list[ChangedFile] = []
        warnings: list[str] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            result = await request_json(client, f"/repos/{owner}/{repo}/pulls/{number}/files", params={"per_page": FILES_PER_PAGE, "page": page})
            if not isinstance(result, list):
                raise ValueError("GitHub returned an invalid changed-files response.")
            for item in result:
                if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                    warnings.append("GitHub returned a malformed changed-file entry; it was skipped.")
                    continue
                files.append(ChangedFile(
                    filename=item["filename"],
                    status=_str(item.get("status"), "modified"),
                    additions=_nonnegative(item.get("additions")),
                    deletions=_nonnegative(item.get("deletions")),
                    changes=_nonnegative(item.get("changes")),
                    previous_filename=item.get("previous_filename") if isinstance(item.get("previous_filename"), str) else None,
                    patch=item.get("patch") if isinstance(item.get("patch"), str) else None,
                    size=_nonnegative(item.get("size")) if isinstance(item.get("size"), int) else None,
                ))
                if len(files) >= MAX_FILES:
                    warnings.append(f"Changed-file listing was capped at {MAX_FILES} files.")
                    return PullRequestBundle(metadata=metadata, files=files, warnings=warnings)
            if len(result) < FILES_PER_PAGE:
                break
        if metadata.changed_files > len(files):
            warnings.append(f"GitHub reported {metadata.changed_files} changed files; {len(files)} were returned for analysis.")
        return PullRequestBundle(metadata=metadata, files=files, warnings=warnings)


async def get_file_content(owner: str, repo: str, filename: str, ref: str) -> str | None:
    """Read a bounded source file through the fixed GitHub API host."""
    if not ref or not filename or filename.startswith(("/", "\\")) or ".." in filename.replace("\\", "/").split("/"):
        return None
    encoded_path = quote(filename.replace("\\", "/"), safe="/")
    async with async_client() as client:
        result = await request_json(client, f"/repos/{owner}/{repo}/contents/{encoded_path}", params={"ref": ref})
    if not isinstance(result, dict) or result.get("encoding") != "base64" or not isinstance(result.get("content"), str):
        return None
    if isinstance(result.get("size"), int) and result["size"] > MAX_SOURCE_BYTES:
        return None
    try:
        decoded = base64.b64decode(result["content"], validate=False)
        if len(decoded) > MAX_SOURCE_BYTES:
            return None
        return decoded.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def _str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _required_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"GitHub pull request is missing {field}.")
    return value


def _nullable_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _nonnegative(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _required_nonnegative(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"GitHub pull request has invalid {field}.")
    return value
 main
