"""Map analyzer locations to added lines in a GitHub unified diff."""

import re
from pathlib import PurePosixPath
from typing import Any

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$")
_MAX_PATCH_CHARS = 200_000
_MAX_LINES_PER_FILE = 4_000


def added_lines_by_file(files: list[dict[str, Any]]) -> tuple[dict[str, set[int]], dict[str, str]]:
    """Return reliably mapped new-side added lines and per-file coverage gaps.

    Diagnostics are reported only when their new-file line is explicitly an
    added line. Files with missing or malformed patches have no eligible lines.
    Truncated patches retain mappings from complete hunks and report a gap.
    """
    changed: dict[str, set[int]] = {}
    limitations: dict[str, str] = {}
    for item in files:
        filename = item.get("filename")
        if not isinstance(filename, str):
            limitations["<unknown>"] = "A changed file had no valid path; static findings could not be mapped."
            continue
        path = _normalize_path(filename)
        if not path:
            limitations[filename] = "The changed file path was invalid; static findings could not be mapped."
            continue
        if PurePosixPath(path).suffix.lower() != ".py":
            continue
        status = item.get("status")
        if status == "removed":
            continue
        patch = item.get("patch")
        if not isinstance(patch, str) or not patch:
            limitations[path] = "GitHub did not provide a diff patch; static findings for this file were omitted."
            continue

        truncated = len(patch) > _MAX_PATCH_CHARS
        text = patch[:_MAX_PATCH_CHARS]
        lines = text.splitlines()
        if len(lines) > _MAX_LINES_PER_FILE:
            lines = lines[:_MAX_LINES_PER_FILE]
            truncated = True
        mapped, malformed = _parse_added_lines(lines)
        if malformed:
            limitations[path] = "The diff hunk was malformed; static findings for this file were omitted."
            continue
        changed[path] = mapped
        if truncated:
            limitations[path] = "The diff was truncated; only findings mapped to available added lines are shown."
    return changed, limitations


def _parse_added_lines(lines: list[str]) -> tuple[set[int], bool]:
    added: set[int] = set()
    old_line = new_line = old_count = new_count = 0
    in_hunk = False
    hunks = 0

    def counts_match() -> bool:
        return old_count == expected_old and new_count == expected_new

    expected_old = expected_new = 0
    for raw in lines:
        match = _HUNK.match(raw)
        if match:
            if in_hunk and not counts_match():
                return set(), True
            hunks += 1
            in_hunk = True
            old_line, new_line = int(match.group(1)), int(match.group(3))
            expected_old = int(match.group(2) or 1)
            expected_new = int(match.group(4) or 1)
            old_count = new_count = 0
            continue
        if not in_hunk:
            # GitHub may omit file headers and send hunk text only, but other
            # nonempty preamble content is not safe to interpret.
            if raw:
                return set(), True
            continue
        if raw.startswith("\\ No newline at end of file"):
            continue
        if raw.startswith("+"):
            if new_count >= expected_new:
                return set(), True
            added.add(new_line)
            new_line += 1
            new_count += 1
        elif raw.startswith("-"):
            if old_count >= expected_old:
                return set(), True
            old_line += 1
            old_count += 1
        elif raw.startswith(" "):
            if old_count >= expected_old or new_count >= expected_new:
                return set(), True
            old_line += 1
            new_line += 1
            old_count += 1
            new_count += 1
        else:
            return set(), True
    if not hunks or (in_hunk and not counts_match()):
        return set(), True
    return added, False


def _normalize_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not path.parts or "\x00" in normalized:
        return ""
    return path.as_posix()
