"""Normalize changed-file records and retrieve bounded source text."""

import base64
import binascii
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import quote

from app.schemas.github import ChangedFile, PullRequestReference

from .client import (
    GitHubAPIError,
    GitHubClient,
    GitHubNotFoundError,
)

MAX_SOURCE_FILE_BYTES = 1_000_000
MAX_SOURCE_FILES_PER_PR = 100
MAX_SOURCE_FETCH_WORKERS = 8
SOURCE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
    ".java", ".js", ".jsx", ".kt", ".kts", ".php", ".py", ".rb", ".rs",
    ".scala", ".sh", ".sql", ".swift", ".ts", ".tsx", ".vue", ".svelte",
}


class UnsafeChangedFilePathError(ValueError):
    """A GitHub-provided path cannot safely be materialized in a workspace."""


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def normalize_changed_file(item: Any) -> ChangedFile:
    """Validate a single changed-file object from GitHub's PR files endpoint."""
    if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
        raise GitHubAPIError(200, "GitHub returned an invalid changed-file record")

    status = item.get("status")
    if not isinstance(status, str):
        raise GitHubAPIError(200, "GitHub returned a changed file without a status")

    def count(name: str) -> int:
        value = item.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GitHubAPIError(200, f"GitHub returned an invalid {name} count")
        return value

    return ChangedFile(
        filename=item["filename"],
        previous_filename=_optional_text(item.get("previous_filename")),
        status=status,
        additions=count("additions"),
        deletions=count("deletions"),
        changes=count("changes"),
        sha=_optional_text(item.get("sha")),
        patch=_optional_text(item.get("patch")),
    )


def fetch_source_content(
    client: GitHubClient,
    reference: PullRequestReference,
    head_sha: str,
    changed_file: ChangedFile,
) -> ChangedFile:
    """Attach UTF-8 text at the PR head, when the file is supported and small.

    Missing patches are expected for binary or large diffs; source retrieval is
    independent of the patch field. A per-file 404 is recorded on the file,
    while authentication and rate-limit failures still fail the ingestion.
    """
    if changed_file.status == "removed":
        changed_file.content_unavailable_reason = "deleted_in_head"
        return changed_file
    if not any(changed_file.filename.lower().endswith(ext) for ext in SOURCE_EXTENSIONS):
        changed_file.content_unavailable_reason = "unsupported_file_type"
        return changed_file

    path = quote(changed_file.filename, safe="/")
    endpoint = (
        f"/repos/{reference.owner}/{reference.repo}/contents/{path}"
    )
    try:
        data = client.get_json(endpoint, params={"ref": head_sha})
    except GitHubNotFoundError:
        changed_file.content_unavailable_reason = "unavailable_at_head"
        return changed_file

    if not isinstance(data, dict) or data.get("type") != "file":
        changed_file.content_unavailable_reason = "not_a_regular_file"
        return changed_file

    size = data.get("size")
    if isinstance(size, int) and not isinstance(size, bool) and size > MAX_SOURCE_FILE_BYTES:
        changed_file.content_unavailable_reason = "file_too_large"
        return changed_file

    encoded = data.get("content")
    encoding = data.get("encoding")
    if encoding != "base64" or not isinstance(encoded, str):
        changed_file.content_unavailable_reason = "content_unavailable"
        return changed_file

    try:
        raw = base64.b64decode(encoded, validate=False)
        if len(raw) > MAX_SOURCE_FILE_BYTES:
            changed_file.content_unavailable_reason = "file_too_large"
            return changed_file
        changed_file.source_content = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        changed_file.content_unavailable_reason = "not_utf8_text"
    return changed_file


def fetch_source_contents(
    client: GitHubClient,
    reference: PullRequestReference,
    head_sha: str,
    files: list[ChangedFile],
) -> list[ChangedFile]:
    """Fetch eligible source files concurrently within a bounded API budget.

    GitHub's PR-files endpoint supplies patches but not full file contents, so
    eligible files need individual Contents API requests. A small worker pool
    avoids serial network latency without creating an unbounded request burst.
    ``executor.map`` preserves input ordering; each worker mutates only its own
    ChangedFile object.
    """
    pending: list[ChangedFile] = []
    for changed_file in files:
        eligible = (
            changed_file.status != "removed"
            and any(changed_file.filename.lower().endswith(ext) for ext in SOURCE_EXTENSIONS)
        )
        if not eligible:
            fetch_source_content(client, reference, head_sha, changed_file)
            continue
        if len(pending) >= MAX_SOURCE_FILES_PER_PR:
            changed_file.content_unavailable_reason = "per_pr_content_limit"
            continue

        pending.append(changed_file)

    if pending:
        fetch_one = partial(fetch_source_content, client, reference, head_sha)
        with ThreadPoolExecutor(
            max_workers=min(MAX_SOURCE_FETCH_WORKERS, len(pending)),
            thread_name_prefix="github-source",
        ) as executor:
            # Consume the iterator so worker exceptions (auth, rate limit,
            # network) propagate to the caller instead of being suppressed.
            list(executor.map(fetch_one, pending))
    return files


@contextmanager
def stage_python_files(files: Iterable[ChangedFile]) -> Iterator[list[str]]:
    """Stage available changed Python files in an isolated temporary directory.

    Use the yielded paths only inside the context manager, while running Ruff
    or Bandit. The temporary workspace is removed when the context exits. PR
    source is written as data and is never executed by this helper.
    """
    with TemporaryDirectory(prefix="pr-mentor-review-") as temporary_directory:
        root = Path(temporary_directory).resolve()
        staged_paths: list[str] = []
        staged_names: set[str] = set()

        for changed_file in files:
            if changed_file.status == "removed" or changed_file.source_content is None:
                continue
            if not changed_file.filename.lower().endswith(".py"):
                continue

            filename = changed_file.filename
            relative = PurePosixPath(filename)
            if (
                not filename
                or "\\" in filename
                or "\x00" in filename
                or relative.is_absolute()
                or ".." in relative.parts
                or re.match(r"^[A-Za-z]:", filename)
            ):
                raise UnsafeChangedFilePathError(
                    f"Unsafe changed-file path cannot be staged: {filename!r}"
                )

            normalized_name = relative.as_posix()
            if normalized_name in staged_names:
                raise UnsafeChangedFilePathError(
                    f"Duplicate changed-file path cannot be staged: {normalized_name!r}"
                )

            destination = (root / Path(*relative.parts)).resolve()
            try:
                destination.relative_to(root)
            except ValueError as exc:
                raise UnsafeChangedFilePathError(
                    f"Changed-file path escapes the staging directory: {filename!r}"
                ) from exc

            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(changed_file.source_content, encoding="utf-8")
            staged_names.add(normalized_name)
            staged_paths.append(str(destination))

        yield staged_paths
