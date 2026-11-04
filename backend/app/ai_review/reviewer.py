"""Validate AI review responses and convert them to shared Finding objects."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

from app.ai_review.client import (
    AIProvider,
    AIProviderError,
    OpenAICompatibleProvider,
)
from app.ai_review.prompts import SYSTEM_PROMPT, build_user_prompt
from app.schemas.finding import Finding
from pydantic import ValidationError

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _added_line_numbers(diff_text: str) -> set[int]:
    """Get new-file line numbers for added lines in a unified diff."""
    added: set[int] = set()
    new_line: int | None = None

    for line in diff_text.splitlines():
        match = _HUNK_HEADER.match(line)
        if match:
            new_line = int(match.group(1))
            continue

        if new_line is None:
            continue
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added.add(new_line)
            new_line += 1
        elif line.startswith(("-", "\\")):
            continue
        elif line.startswith(" "):
            new_line += 1

    return added


def _evidence_is_supported(evidence: str, supplied_text: str) -> bool:
    """Check evidence against the supplied source or diff text."""
    if evidence in supplied_text:
        return True

    if "@@" not in supplied_text:
        return False

    # Diff markers are formatting; allow evidence copied from the code itself.
    code_lines = []
    for line in supplied_text.splitlines():
        if line.startswith(("diff --git ", "index ", "---", "+++", "@@")):
            continue
        if line.startswith(("+", "-", " ")):
            line = line[1:]
        code_lines.append(line)

    return evidence in "\n".join(code_lines)


def _make_finding(
    item: object,
    changed_files: Mapping[str, str],
) -> Finding:
    if not isinstance(item, dict):
        raise AIProviderError("An AI finding must be a JSON object.")

    file_path = item.get("file_path")
    if not isinstance(file_path, str) or file_path not in changed_files:
        raise AIProviderError("An AI finding referenced an unsupported file path.")

    supplied_text = changed_files[file_path]

    line = item.get("line")
    if line is not None:
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise AIProviderError("An AI finding contained an invalid line number.")

        added_lines = _added_line_numbers(supplied_text)
        if line not in added_lines:
            raise AIProviderError(
                "An AI finding referenced a line not shown as added in the supplied diff."
            )

    evidence = item.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        raise AIProviderError("An AI finding must include supporting evidence.")
    if not _evidence_is_supported(evidence, supplied_text):
        raise AIProviderError(
            "An AI finding included evidence that was not present in the supplied text."
        )

    message = item.get("message")
    explanation = item.get("explanation")
    suggested_fix = item.get("suggested_fix")
    rule_id = item.get("rule_id")

    if not isinstance(message, str) or not message.strip():
        raise AIProviderError("An AI finding must include a message.")
    if not isinstance(explanation, str) or not explanation.strip():
        raise AIProviderError("An AI finding must include an explanation.")
    if not isinstance(suggested_fix, str) or not suggested_fix.strip():
        raise AIProviderError("An AI finding must include a suggested fix.")
    if rule_id is not None and not isinstance(rule_id, str):
        raise AIProviderError("An AI finding contained an invalid rule_id.")

    try:
        return Finding(
            file_path=file_path,
            line=line,
            rule_id=rule_id,
            severity=item.get("severity"),
            message=message.strip(),
            source="ai",
            evidence=evidence,
            verification_status="unverified",
            explanation=explanation.strip(),
            suggested_fix=suggested_fix.strip(),
        )
    except ValidationError as exc:
        raise AIProviderError("An AI finding did not match the shared Finding schema.") from exc


def review_changed_files(
    changed_files: Mapping[str, str],
    provider: AIProvider | None = None,
) -> list[Finding]:
    """Review supplied file diffs and return validated, unverified AI findings.

    `changed_files` maps each repository-relative path to its unified diff.
    A provider can be injected for tests; otherwise settings are read from
    environment variables when the default provider is created.
    """
    if not isinstance(changed_files, Mapping) or not changed_files:
        raise ValueError("changed_files must contain at least one file.")

    if any(
        not isinstance(path, str)
        or not path.strip()
        or not isinstance(diff, str)
        for path, diff in changed_files.items()
    ):
        raise ValueError("Each changed file must have a non-empty path and text diff.")

    active_provider = provider if provider is not None else OpenAICompatibleProvider()
    response_text = active_provider.generate(
        SYSTEM_PROMPT,
        build_user_prompt(changed_files),
    )

    if not isinstance(response_text, str):
        raise AIProviderError("The AI provider response must be text.")

    try:
        response = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise AIProviderError("The AI response was not valid JSON.") from exc

    if not isinstance(response, dict) or not isinstance(response.get("findings"), list):
        raise AIProviderError('The AI response must be a JSON object with a "findings" list.')

    return [_make_finding(item, changed_files) for item in response["findings"]]


def review_pull_request_payload(
    payload: Mapping[str, object],
    provider: AIProvider | None = None,
) -> list[Finding]:
    """Review the agreed PR-ingestion payload at the AI boundary.

    The ingestion contract supplies ``pull_request``, ``files``, and
    ``files_truncated``. Each file record has a repository-relative
    ``filename`` and may contain a unified-diff ``patch``. Files without a
    patch are not reviewed because source content alone cannot establish
    which lines changed. A truncated file list is rejected to avoid implying
    that the complete pull request was reviewed.
    """
    if not isinstance(payload, Mapping):
        raise TypeError("The pull request payload must be an object.")

    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, Mapping):
        raise TypeError("The pull request payload must contain pull_request metadata.")

    files_truncated = payload.get("files_truncated")
    if not isinstance(files_truncated, bool):
        raise TypeError("files_truncated must be a boolean.")
    if files_truncated:
        raise ValueError("The pull request file list is truncated; a complete list is required.")

    files = payload.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        raise TypeError("The pull request payload must contain a files list.")

    changed_files: dict[str, str] = {}
    seen_paths: set[str] = set()
    for changed_file in files:
        if not isinstance(changed_file, Mapping):
            raise TypeError("Each file entry must be an object.")

        path = changed_file.get("filename")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Each file entry must have a non-empty filename.")
        if path in seen_paths:
            raise ValueError(f"The files list contains duplicate filename: {path}")
        seen_paths.add(path)

        patch = changed_file.get("patch")
        if isinstance(patch, str) and patch.strip():
            changed_files[path] = patch
            continue

    if not changed_files:
        raise ValueError("The pull request payload contains no reviewable file patches.")

    return review_changed_files(changed_files, provider=provider)

"""Schema-validated AI candidate findings; provider transport stays replaceable."""

import json
from uuid import uuid4
from typing import Any

from app.ai_review.client import AIReviewUnavailable, complete_json
from app.ai_review.prompts import SYSTEM_PROMPT, build_user_prompt
from app.schemas.finding import ReviewFinding


async def generate_candidates(metadata: dict[str, Any], files: list[dict[str, Any]], verification: list[dict[str, Any]]) -> list[ReviewFinding]:
    raw = await complete_json(SYSTEM_PROMPT, build_user_prompt(metadata, files, verification))
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIReviewUnavailable("AI provider returned invalid JSON.") from exc
    if not isinstance(body, dict) or not isinstance(body.get("findings"), list):
        raise AIReviewUnavailable("AI provider response must contain a findings array.")
    findings: list[ReviewFinding] = []
    for item in body["findings"][:100]:
        if not isinstance(item, dict):
            continue
        candidate = dict(item)
        candidate.update({"id": str(candidate.get("id") or uuid4().hex), "source": "ai_candidate", "verification_status": "needs_review"})
        try:
            findings.append(ReviewFinding.model_validate(candidate))
        except Exception as exc:
            raise AIReviewUnavailable("AI provider returned a finding that did not match the review schema.") from exc
    return findings
