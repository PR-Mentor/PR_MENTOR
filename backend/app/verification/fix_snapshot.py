"""Pure, bounded helpers for validating and reconstructing Python fixes."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.github_service.pull_requests import (
    MAX_SOURCE_BYTES,
    GitHubFileSnapshotError,
    git_blob_sha,
    validate_repository_python_path,
)
from app.schemas.issue import SuggestedFix


@dataclass(frozen=True)
class ReconstructedSource:
    text: str
    content: bytes
    blob_sha: str


def _records(text: str) -> list[tuple[str, str]]:
    """Split only on source line terminators, retaining each exact terminator."""
    if not text:
        return []
    records: list[tuple[str, str]] = []
    start = 0
    for match in re.finditer(r"\r\n|\n|\r", text):
        records.append((text[start : match.start()], match.group()))
        start = match.end()
    if start < len(text):
        records.append((text[start:], ""))
    return records


def reconstruct_proposed_source(source: str, fix: SuggestedFix) -> ReconstructedSource:
    """Apply exactly one schema-declared range in memory, preserving file endings."""
    validate_repository_python_path(fix.file_path)
    if not isinstance(source, str) or "\x00" in source:
        raise GitHubFileSnapshotError(
            "The source is not valid text for reconstruction."
        )
    source_bytes = source.encode("utf-8", errors="strict")
    if len(source_bytes) > MAX_SOURCE_BYTES:
        raise GitHubFileSnapshotError(
            "The source file exceeds the reconstruction size limit."
        )

    lines = _records(source)
    start = fix.start_line - 1
    stop = fix.end_line
    if start < 0 or stop > len(lines):
        raise GitHubFileSnapshotError(
            "The expected replacement line range is outside the source file."
        )
    expected = fix.expected_original_code.splitlines()
    current = [line for line, _ending in lines[start:stop]]
    if current != expected:
        raise GitHubFileSnapshotError(
            "The expected original text does not match the source range."
        )

    replacement_lines = fix.proposed_code.splitlines()
    if not replacement_lines:
        raise GitHubFileSnapshotError(
            "The proposed replacement contains no source lines."
        )
    newline = next((ending for _line, ending in lines if ending), "\n")
    old_final_ending = lines[stop - 1][1]
    replacement = [(line, newline) for line in replacement_lines[:-1]]
    replacement.append((replacement_lines[-1], old_final_ending))
    result_records = lines[:start] + replacement + lines[stop:]
    proposed = "".join(line + ending for line, ending in result_records)
    content = proposed.encode("utf-8", errors="strict")
    if len(content) > MAX_SOURCE_BYTES:
        raise GitHubFileSnapshotError(
            "The reconstructed proposed file exceeds the source size limit."
        )
    return ReconstructedSource(proposed, content, git_blob_sha(content))
