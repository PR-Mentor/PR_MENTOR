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

"""Fetch bounded pull-request metadata and changed-file pages from GitHub."""

import base64
import binascii
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote

from app.github_service.client import async_client, request_json
from app.github_service.parser import parse_pull_request_url
from app.schemas.github import ChangedFile, PullRequestBundle, PullRequestMetadata

MAX_FILE_PAGES = 10
FILES_PER_PAGE = 100
MAX_FILES = MAX_FILE_PAGES * FILES_PER_PAGE
MAX_SOURCE_BYTES = 100_000
MAX_REPOSITORY_PATH_DEPTH = 64


@dataclass(frozen=True)
class GitHubFileSnapshot:
    """Bounded file bytes and Git blob identity at one immutable commit."""

    path: str
    commit_sha: str
    blob_sha: str
    content: bytes
    text: str


class GitHubFileSnapshotError(ValueError):
    """GitHub returned a source object that cannot serve as a verified file snapshot."""


async def _request_json(
    client: httpx.AsyncClient,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    github_token: str | None = None,
) -> Any:
    return await request_json(client, path, params=params)


async def fetch_pull_request(url: str, github_token: str | None = None) -> PullRequestBundle:
    owner, repo, number = parse_pull_request_url(url)
    async with async_client(token=github_token) as client:
        raw = await request_json(client, f"/repos/{owner}/{repo}/pulls/{number}")
        if not isinstance(raw, dict):
            raise ValueError("GitHub returned an invalid pull request response.")  # noqa: TRY004 - malformed upstream data is a value error, not a caller type error
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
            result = await request_json(
                client,
                f"/repos/{owner}/{repo}/pulls/{number}/files",
                params={"per_page": FILES_PER_PAGE, "page": page},
            )
            if not isinstance(result, list):
                raise ValueError("GitHub returned an invalid changed-files response.")  # noqa: TRY004 - malformed upstream data is a value error, not a caller type error
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


async def fetch_current_pull_request_head_sha(owner: str, repo: str, number: int) -> str | None:
    """Read PR metadata without the process-local response cache."""
    async with async_client() as client:
        raw = await request_json(
            client,
            f"/repos/{owner}/{repo}/pulls/{number}",
            use_cache=False,
        )
    if not isinstance(raw, dict):
        return None
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    sha = head.get("sha")
    return sha if isinstance(sha, str) else None


async def fetch_current_pull_request_file_change(
    owner: str,
    repo: str,
    number: int,
    filename: str,
) -> str | None:
    """Confirm a deleted or renamed path from a fresh PR files listing."""
    async with async_client() as client:
        for page in range(1, MAX_FILE_PAGES + 1):
            result = await request_json(
                client,
                f"/repos/{owner}/{repo}/pulls/{number}/files",
                params={"per_page": FILES_PER_PAGE, "page": page},
                use_cache=False,
            )
            if not isinstance(result, list):
                return None
            for item in result:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", "")).lower()
                if item.get("filename") == filename and status == "removed":
                    return "deleted"
                if item.get("previous_filename") == filename and status == "renamed":
                    return "renamed"
            if len(result) < FILES_PER_PAGE:
                break
    return None


async def get_file_content(owner: str, repo: str, filename: str, ref: str, github_token: str | None = None) -> str | None:
    """Read a bounded source file through the fixed GitHub API host."""
    if not ref or not filename or filename.startswith(("/", "\\")) or ".." in filename.replace("\\", "/").split("/"):
        return None
    encoded_path = quote(filename.replace("\\", "/"), safe="/")
    async with async_client(token=github_token) as client:
        result = await request_json(
            client,
            f"/repos/{owner}/{repo}/contents/{encoded_path}",
            params={"ref": ref},
        )
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


async def fetch_file_snapshot(owner: str, repo: str, filename: str, commit_sha: str) -> GitHubFileSnapshot:
    """Fetch and verify one regular UTF-8 file at an exact immutable commit SHA."""
    if any(not isinstance(part, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."} for part in (owner, repo)):
        raise GitHubFileSnapshotError("Repository identity is invalid.")
    path = validate_repository_python_path(filename)
    parts = path.split("/")
    if len(parts) > MAX_REPOSITORY_PATH_DEPTH:
        raise GitHubFileSnapshotError("The target path exceeds the supported repository depth.")
    if not isinstance(commit_sha, str) or re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", commit_sha) is None:
        raise GitHubFileSnapshotError("A full commit SHA is required for source retrieval.")
    encoded_path = quote(path, safe="/")
    async with async_client() as client:
        # Snapshot calls must remain cancellable by Stage 2's hard deadline.
        # The shared request cache shields in-flight work during cancellation;
        # immutable object requests deliberately bypass that layer.
        commit = await request_json(
            client,
            f"/repos/{owner}/{repo}/git/commits/{commit_sha}",
            use_cache=False,
        )
        tree = commit.get("tree") if isinstance(commit, dict) else None
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}", tree_sha) is None:
            raise GitHubFileSnapshotError("GitHub did not return the exact commit tree identity.")
        for index, component in enumerate(parts):
            tree_result = await request_json(
                client,
                f"/repos/{owner}/{repo}/git/trees/{tree_sha}",
                use_cache=False,
            )
            entries = tree_result.get("tree") if isinstance(tree_result, dict) else None
            if not isinstance(entries, list):
                raise GitHubFileSnapshotError("GitHub returned an invalid repository tree response.")
            entry = next((item for item in entries if isinstance(item, dict) and item.get("path") == component), None)
            if entry is None:
                raise GitHubFileSnapshotError("The target path does not exist at the requested commit.")
            is_file = index == len(parts) - 1
            expected_type = "blob" if is_file else "tree"
            expected_modes = {"100644", "100755"} if is_file else {"040000"}
            if entry.get("type") != expected_type or entry.get("mode") not in expected_modes:
                raise GitHubFileSnapshotError("The target path contains a symlink, submodule, or non-regular file.")
            tree_sha = entry.get("sha")
            if not isinstance(tree_sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}", tree_sha) is None:
                raise GitHubFileSnapshotError("GitHub returned an invalid object identity in the repository tree.")
        expected_blob_sha = tree_sha
        result = await request_json(
            client,
            f"/repos/{owner}/{repo}/contents/{encoded_path}",
            params={"ref": commit_sha},
            use_cache=False,
        )
    if not isinstance(result, dict):
        raise GitHubFileSnapshotError("GitHub returned an invalid file snapshot response.")
    if result.get("type") != "file":
        raise GitHubFileSnapshotError("The target is not a regular file; symlinks and other Git objects are unsupported.")
    reported_path = result.get("path")
    if reported_path != path:
        raise GitHubFileSnapshotError("GitHub returned a file under a different normalized path.")
    reported_sha = result.get("sha")
    if not isinstance(reported_sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}", reported_sha) is None:
        raise GitHubFileSnapshotError("GitHub did not return a valid SHA-1 file blob identifier.")
    size = result.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > MAX_SOURCE_BYTES:
        raise GitHubFileSnapshotError("The source file is missing size metadata or exceeds the file size limit.")
    if result.get("encoding") != "base64" or not isinstance(result.get("content"), str):
        raise GitHubFileSnapshotError("GitHub did not return bounded base64 file content.")
    encoded = result["content"]
    if len(encoded) > ((MAX_SOURCE_BYTES + 2) // 3) * 4 + 1_024:
        raise GitHubFileSnapshotError("Encoded source exceeds the file size limit.")
    try:
        content = base64.b64decode("".join(encoded.split()), validate=True)
        text = content.decode("utf-8", errors="strict")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise GitHubFileSnapshotError("The source file is not valid UTF-8 base64 content.") from exc
    if len(content) != size or len(content) > MAX_SOURCE_BYTES:
        raise GitHubFileSnapshotError("Decoded source size does not match GitHub metadata or exceeds the file size limit.")
    actual_sha = git_blob_sha(content)
    if actual_sha.casefold() != reported_sha.casefold():
        raise GitHubFileSnapshotError("The downloaded file bytes do not match GitHub's reported blob SHA.")
    if actual_sha.casefold() != expected_blob_sha.casefold():
        raise GitHubFileSnapshotError("The downloaded file bytes do not match the exact commit tree blob.")
    return GitHubFileSnapshot(path, commit_sha, actual_sha, content, text)


def validate_repository_python_path(value: str) -> str:
    """Reject path spellings that can escape or alias a repository-relative Python file."""
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise GitHubFileSnapshotError("The target path must be a canonical repository-relative POSIX path.")
    if unicodedata.normalize("NFC", value) != value:
        raise GitHubFileSnapshotError("The target path must use canonical Unicode normalization.")
    path = PurePosixPath(value)
    parts = value.split("/")
    if (
        path.is_absolute() or value.startswith("/") or value.endswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or re.match(r"^[a-zA-Z]:", value)
        or str(path) != value
        or path.suffix.lower() != ".py"
    ):
        raise GitHubFileSnapshotError("The target path must be a canonical repository-relative Python file path.")
    return value


def git_blob_sha(content: bytes) -> str:
    """Return Git's SHA-1 blob identity for exact file bytes."""
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


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
