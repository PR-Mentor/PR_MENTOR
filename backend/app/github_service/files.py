"""Fetch every page of changed files for a GitHub pull request."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.github_service.client import GitHubClient, GitHubServiceError

FILES_PER_PAGE = 100
MAX_FILE_PAGES = 10


def fetch_pull_request_files(
    client: GitHubClient,
    owner: str,
    repo: str,
    number: int,
    *,
    max_pages: int = MAX_FILE_PAGES,
) -> tuple[list[Mapping[str, Any]], bool]:
    """Return changed-file records and whether the safety page limit truncated them."""
    if max_pages < 1:
        raise ValueError("max_pages must be at least one.")

    files: list[Mapping[str, Any]] = []
    for page in range(1, max_pages + 1):
        data = client.get_json(
            f"/repos/{owner}/{repo}/pulls/{number}/files",
            params={"per_page": FILES_PER_PAGE, "page": page},
        )
        if not isinstance(data, list) or any(not isinstance(item, Mapping) for item in data):
            raise GitHubServiceError("GitHub returned malformed changed-file data.")

        files.extend(data)
        if len(data) < FILES_PER_PAGE:
            return files, False

    return files, True

"""Turn GitHub's unified patch text into the review UI's stable diff shape."""

import re
from typing import Any


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*$")
MAX_PATCH_CHARS = 200_000
MAX_LINES_PER_FILE = 4_000


def parse_patch(item: dict[str, Any]) -> dict[str, Any]:
    patch = item.get("patch")
    filename = item.get("filename") if isinstance(item.get("filename"), str) else ""
    result: dict[str, Any] = {
        "path": filename,
        "previous_path": item.get("previous_filename") if isinstance(item.get("previous_filename"), str) else None,
        "status": item.get("status") if isinstance(item.get("status"), str) else "modified",
        "added": _count(item.get("additions")),
        "removed": _count(item.get("deletions")),
        "hunks": [],
        "truncated": not isinstance(patch, str),
    }
    if not isinstance(patch, str):
        return result
    if len(patch) > MAX_PATCH_CHARS:
        patch = patch[:MAX_PATCH_CHARS]
        result["truncated"] = True

    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    old_line = new_line = 0
    for raw in patch.splitlines():
        match = _HUNK.match(raw)
        if match:
            current = {"header": raw, "lines": []}
            hunks.append(current)
            old_line, new_line = int(match.group(1)), int(match.group(3))
            continue
        if current is None or raw.startswith("\\ No newline"):
            continue
        if len(current["lines"]) >= MAX_LINES_PER_FILE:
            result["truncated"] = True
            break
        if raw.startswith("+"):
            current["lines"].append({"kind": "added", "line": new_line, "text": raw[1:]})
            new_line += 1
        elif raw.startswith("-"):
            current["lines"].append({"kind": "removed", "line": old_line, "text": raw[1:]})
            old_line += 1
        elif raw.startswith(" "):
            current["lines"].append({"kind": "context", "line": new_line, "text": raw[1:]})
            old_line += 1
            new_line += 1
    result["hunks"] = hunks
    return result


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
