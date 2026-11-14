"""Bounded generation and backend verification of suggested Python fixes.

This service returns proposals only. It does not mutate canonical issues,
write source files, apply patches, or contact GitHub with write operations.
"""

from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError

from app.ai_review.client import (
    AIReviewUnavailable,
    complete_json,
    complete_validated_json,
    is_configured,
    last_call_metadata,
)
from app.ai_review.fix_prompts import (
    FIX_GENERATION_SYSTEM_PROMPT,
    build_fix_generation_prompt,
)
from app.github_service.pull_requests import MAX_SOURCE_BYTES, get_file_content
from app.schemas.issue import CanonicalIssue, SuggestedFix

FixGenerationOutcome = Literal[
    "generated", "insufficient_context", "unavailable", "failed", "no_proposal", "skipped_limit",
]

MAX_FIXES_PER_REQUEST = 5
MAX_FIX_LLM_CALLS = 5
MAX_TOTAL_GENERATION_SECONDS = 120.0
MAX_SOURCE_FETCH_SECONDS = 20.0
MAX_PROVIDER_CALL_SECONDS = 60.0
MAX_CONTEXT_LINES = 20
MAX_CONTEXT_LINE_CHARS = 300
MAX_CONTEXT_CHARS = 6_000
CONTEXT_MARGIN_LINES = 4
MAX_REPLACEMENT_LINES = 12
MAX_OBSERVATIONS = 4
MAX_OBSERVATION_CHARS = 500
MAX_EVIDENCE_CHARS = 200
MAX_DIFF_LINES = 10
MAX_DIFF_LINE_CHARS = 220
MAX_DIFF_CONTEXT_CHARS = 3_000
MAX_FIX_RESPONSE_CHARS = 32_000
SHA_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_RESPONSE_FIX_FIELDS = {
    "canonical_issue_id", "file_path", "start_line", "end_line",
    "expected_original_code", "proposed_code", "change_summary", "rationale",
    "limitations", "confidence",
}


@dataclass
class IssueFixOutcome:
    issue_id: str
    status: FixGenerationOutcome
    reason: str | None = None
    fix: SuggestedFix | None = None


@dataclass
class FixGenerationResult:
    outcomes: list[IssueFixOutcome] = field(default_factory=list)
    source_fetch_count: int = 0
    llm_call_count: int = 0
    provider_calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def fixes(self) -> list[SuggestedFix]:
        return [outcome.fix for outcome in self.outcomes if outcome.fix is not None]


@dataclass(frozen=True)
class _SourceContext:
    lines: list[str]
    context_start: int
    context_end: int
    issue_start: int
    issue_end: int
    blob_sha: str
    prompt_data: dict[str, Any]


async def generate_suggested_fixes(
    issues: list[CanonicalIssue],
    *,
    owner: str,
    repo: str,
    head_sha: str | None,
    diff_files: list[dict[str, Any]] | None = None,
) -> FixGenerationResult:
    """Return at most one verified proposal per eligible issue, without mutation."""
    result = FixGenerationResult()
    if not issues:
        return result

    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.issue_id] = counts.get(issue.issue_id, 0) + 1
    duplicate_ids = {issue_id for issue_id, count in counts.items() if count > 1}
    started = asyncio.get_running_loop().time()
    source_cache: dict[str, str | None] = {}
    attempted = 0

    for issue in issues:
        if issue.issue_id in duplicate_ids:
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "failed", "Canonical issue IDs are ambiguous."))
            continue
        if attempted >= MAX_FIXES_PER_REQUEST or result.llm_call_count >= MAX_FIX_LLM_CALLS:
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "skipped_limit", "Per-request fix generation limit reached."))
            continue
        attempted += 1
        if not is_configured():
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "unavailable", "The configured LLM is unavailable."))
            continue
        if not isinstance(head_sha, str) or not SHA_PATTERN.fullmatch(head_sha):
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "insufficient_context", "A valid PR head snapshot is unavailable."))
            continue
        if len(issue.suggested_fixes) >= 5:
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "unavailable", "The issue already has the maximum number of proposals."))
            continue

        path = issue.file_path
        if not _is_supported_python_path(path):
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "insufficient_context", "Only repository-relative Python source files are supported."))
            continue
        if _is_removed(path, diff_files or []):
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "insufficient_context", "The affected file was removed from the PR head."))
            continue

        focus = _issue_range(issue)
        if focus is None:
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "insufficient_context", "The issue has no unambiguous source line range."))
            continue

        remaining = MAX_TOTAL_GENERATION_SECONDS - (asyncio.get_running_loop().time() - started)
        if remaining <= 0:
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "skipped_limit", "Per-request time budget reached."))
            continue
        if path not in source_cache:
            result.source_fetch_count += 1
            try:
                source_cache[path] = await asyncio.wait_for(
                    get_file_content(owner, repo, path, head_sha),
                    timeout=min(MAX_SOURCE_FETCH_SECONDS, remaining),
                )
            except Exception:  # noqa: BLE001 - A single unavailable source file must not cancel other issue proposals.
                source_cache[path] = None
        source = source_cache[path]
        if not isinstance(source, str):
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "insufficient_context", "Source content is unavailable at the PR head."))
            continue
        if "\x00" in source:
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "insufficient_context", "Source content is not valid text code."))
            continue
        raw_source = source.encode("utf-8")
        if len(raw_source) > MAX_SOURCE_BYTES:
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "insufficient_context", "Source content exceeds the existing retrieval size limit."))
            continue

        context = _build_source_context(issue, focus, source, raw_source, diff_files or [])
        if context is None:
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "insufficient_context", "The exact issue lines could not be included in bounded source context."))
            continue
        try:
            prompt = build_fix_generation_prompt(context.prompt_data)
        except ValueError:
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "insufficient_context", "The bounded fix-generation prompt exceeds its size limit."))
            continue

        remaining = MAX_TOTAL_GENERATION_SECONDS - (asyncio.get_running_loop().time() - started)
        if remaining <= 0:
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "skipped_limit", "Per-request time budget reached."))
            continue
        result.llm_call_count += 1
        try:
            raw_response, fix_payload = await asyncio.wait_for(
                complete_validated_json(
                    "fix_generation", FIX_GENERATION_SYSTEM_PROMPT, prompt,
                    _parse_bounded_fix_response,
                    completion_function=complete_json,
                ),
                timeout=min(MAX_PROVIDER_CALL_SECONDS, remaining),
            )
            metadata = last_call_metadata()
            if metadata is not None:
                result.provider_calls.append(metadata)
            if not isinstance(raw_response, str):
                raise TypeError("Fix-generation response must be a bounded string.")
            if fix_payload is None:
                result.outcomes.append(IssueFixOutcome(issue.issue_id, "no_proposal", "The model did not return a source-grounded proposal."))
                continue
            fix = _validate_and_build_fix(issue, fix_payload, context, head_sha)
            if fix is None:
                result.outcomes.append(IssueFixOutcome(issue.issue_id, "failed", "The proposed replacement did not match the supplied source context."))
                continue
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "generated", fix=fix))
        except (AIReviewUnavailable, TimeoutError):
            metadata = last_call_metadata()
            if metadata is not None:
                result.provider_calls.append(metadata)
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "failed", "The fix-generation provider did not return a valid response."))
        except (ValueError, TypeError, ValidationError, json.JSONDecodeError):
            metadata = last_call_metadata()
            if metadata is not None:
                result.provider_calls.append(metadata)
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "failed", "The fix-generation response was malformed or invalid."))
        except Exception:  # noqa: BLE001 - Isolate one issue's proposal failure from the rest of the review.
            result.outcomes.append(IssueFixOutcome(issue.issue_id, "failed", "The fix-generation service failed for this issue."))
    return result


def _is_supported_python_path(path: str) -> bool:
    if not path or path.startswith(("/", "\\")) or "\\" in path or "\x00" in path:
        return False
    parts = path.split("/")
    return not any(part in {"", ".", ".."} for part in parts) and parts[-1].lower().endswith(".py")


def _is_removed(path: str, diff_files: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(item, dict)
        and item.get("path") == path
        and str(item.get("status", "")).lower() == "removed"
        for item in diff_files
    )


def _issue_range(issue: CanonicalIssue) -> tuple[int, int] | None:
    start = issue.line_start or issue.line_end
    end = issue.line_end or issue.line_start
    if start is not None and end is not None:
        return start, end
    ranges = {
        (finding.line_start or finding.line_end, finding.line_end or finding.line_start)
        for observation in issue.observations
        for finding in [observation.original_finding]
        if finding.line_start is not None or finding.line_end is not None
    }
    ranges.discard((None, None))
    if len(ranges) != 1:
        return None
    inferred = next(iter(ranges))
    if inferred[0] is None or inferred[1] is None:
        return None
    return inferred  # type: ignore[return-value]


def _build_source_context(
    issue: CanonicalIssue,
    focus: tuple[int, int],
    source: str,
    raw_source: bytes,
    diff_files: list[dict[str, Any]],
) -> _SourceContext | None:
    lines = source.splitlines()
    issue_start, issue_end = focus
    if issue_start < 1 or issue_end < issue_start or issue_end > len(lines):
        return None
    if issue_end - issue_start + 1 > MAX_REPLACEMENT_LINES:
        return None
    start = max(1, issue_start - CONTEXT_MARGIN_LINES)
    end = min(len(lines), issue_end + CONTEXT_MARGIN_LINES)
    if end - start + 1 > MAX_CONTEXT_LINES:
        return None
    selected = lines[start - 1:end]
    if any(len(line) > MAX_CONTEXT_LINE_CHARS for line in selected):
        return None
    excerpt = "\n".join(f"{line_number}: {lines[line_number - 1]}" for line_number in range(start, end + 1))
    if len(excerpt) > MAX_CONTEXT_CHARS:
        return None
    diff_context = _diff_context(issue.file_path, focus, diff_files)
    observations = []
    for observation in issue.observations[:MAX_OBSERVATIONS]:
        finding = observation.original_finding
        observations.append({
            "source": finding.source,
            "tool": observation.tool_name,
            "provider": observation.provider_name,
            "rule_id": observation.rule_id,
            "message": finding.description[:MAX_OBSERVATION_CHARS],
            "evidence": [item[:MAX_EVIDENCE_CHARS] for item in finding.evidence[:2]],
            "line_start": finding.line_start,
            "line_end": finding.line_end,
        })
    explanation = None
    if issue.explanation is not None:
        explanation = {
            "summary": issue.explanation.summary[:700],
            "why_it_matters": issue.explanation.why_it_matters[:700],
            "root_cause": issue.explanation.root_cause[:700],
            "correction": issue.explanation.correction[:700],
            "concept": issue.explanation.concept[:700],
            "code_example": None if issue.explanation.code_example is None else {
                "before": issue.explanation.code_example.before[:1_000],
                "after": issue.explanation.code_example.after[:1_000],
                "language": issue.explanation.code_example.language,
            },
            "caveats": [value[:300] for value in issue.explanation.caveats[:3]],
        }
    prompt_data = {
        "canonical_issue": {
            "issue_id": issue.issue_id,
            "description": issue.normalized_description[:1_000],
            "severity": issue.severity,
            "category": issue.category,
            "file_path": issue.file_path,
            "issue_line_start": issue_start,
            "issue_line_end": issue_end,
        },
        "observations": observations,
        "existing_explanation": explanation,
        "source_context": {
            "language": "python",
            "source_revision": "PR head",
            "numbered_lines": excerpt,
            "allowed_replacement_line_start": start,
            "allowed_replacement_line_end": end,
        },
        "pull_request_diff_context": diff_context,
    }
    return _SourceContext(
        lines=lines,
        context_start=start,
        context_end=end,
        issue_start=issue_start,
        issue_end=issue_end,
        blob_sha=_git_blob_sha(raw_source),
        prompt_data=prompt_data,
    )


def _diff_context(path: str, focus: tuple[int, int], diff_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    diff = next((item for item in diff_files if isinstance(item, dict) and item.get("path") == path), None)
    if diff is None:
        return []
    focus_start, focus_end = focus
    output: list[dict[str, Any]] = []
    total = 0
    diff_line_count = 0
    for hunk in diff.get("hunks", []):
        if not isinstance(hunk, dict):
            continue
        hunk_lines = hunk.get("lines", [])
        if not isinstance(hunk_lines, list):
            continue
        relevant_indices = [
            index for index, line in enumerate(hunk_lines)
            if isinstance(line, dict)
            and line.get("kind") in {"added", "context"}
            and isinstance(line.get("line"), int)
            and focus_start - CONTEXT_MARGIN_LINES <= line["line"] <= focus_end + CONTEXT_MARGIN_LINES
        ]
        if not relevant_indices:
            continue
        if diff_line_count >= MAX_DIFF_LINES:
            break
        focus_center = (focus_start + focus_end) / 2
        anchor = min(
            relevant_indices,
            key=lambda index: abs(hunk_lines[index]["line"] - focus_center),
        )
        half_window = MAX_DIFF_LINES // 2
        window_start = max(0, anchor - half_window)
        selected_lines = hunk_lines[window_start:window_start + MAX_DIFF_LINES]
        header = hunk.get("header")
        if isinstance(header, str):
            output.append({"hunk_header": header[:160]})
            total += min(len(header), 160) + 32
        for line in selected_lines:
            if diff_line_count >= MAX_DIFF_LINES or not isinstance(line, dict):
                break
            kind = line.get("kind")
            number = line.get("line")
            text = line.get("text")
            if kind not in {"added", "removed", "context"} or not isinstance(number, int) or not isinstance(text, str):
                continue
            label = "old line" if kind == "removed" else "head line"
            value = {"kind": kind, label: number, "text": text[:MAX_DIFF_LINE_CHARS], "truncated": len(text) > MAX_DIFF_LINE_CHARS}
            cost = len(text[:MAX_DIFF_LINE_CHARS]) + 80
            if total + cost > MAX_DIFF_CONTEXT_CHARS:
                return output
            output.append(value)
            total += cost
            diff_line_count += 1
        if diff_line_count >= MAX_DIFF_LINES:
            break
    return output


def _git_blob_sha(raw_source: bytes) -> str:
    header = f"blob {len(raw_source)}\0".encode("ascii")
    return hashlib.sha1(header + raw_source, usedforsecurity=False).hexdigest()


def _parse_fix_response(raw_response: str) -> dict[str, Any] | None:
    try:
        body = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid JSON response.") from exc
    if not isinstance(body, dict) or set(body) != {"fix"}:
        raise ValueError("Response must contain exactly one fix field.")
    fix = body["fix"]
    if fix is None:
        return None
    if not isinstance(fix, dict) or set(fix) != _RESPONSE_FIX_FIELDS:
        raise ValueError("Fix object has missing or unexpected fields.")
    return fix


def _parse_bounded_fix_response(raw_response: str) -> dict[str, Any] | None:
    if not isinstance(raw_response, str) or len(raw_response) > MAX_FIX_RESPONSE_CHARS:
        raise ValueError("Fix-generation response exceeds the configured response limit.")
    return _parse_fix_response(raw_response)


def _validate_and_build_fix(
    issue: CanonicalIssue,
    payload: dict[str, Any],
    context: _SourceContext,
    head_sha: str,
) -> SuggestedFix | None:
    if payload["canonical_issue_id"] != issue.issue_id or payload["file_path"] != issue.file_path:
        return None
    start = payload["start_line"]
    end = payload["end_line"]
    if (
        isinstance(start, bool) or not isinstance(start, int)
        or isinstance(end, bool) or not isinstance(end, int)
        or start < context.context_start or end > context.context_end
        or start > end or end - start + 1 > MAX_REPLACEMENT_LINES
        or start < context.issue_start or end > context.issue_end
    ):
        return None
    expected = payload["expected_original_code"]
    proposed = payload["proposed_code"]
    if not isinstance(expected, str) or not isinstance(proposed, str):
        return None
    retrieved = "\n".join(context.lines[start - 1:end])
    if expected != retrieved or proposed == expected:
        return None

    reconstructed_lines = (
        context.lines[:start - 1]
        + proposed.splitlines()
        + context.lines[end:]
    )
    try:
        ast.parse("\n".join(reconstructed_lines), filename=issue.file_path)
    except (SyntaxError, ValueError, TypeError):
        return None

    proposal_data = {
        "fix_id": uuid4().hex,
        "canonical_issue_id": issue.issue_id,
        "file_path": issue.file_path,
        "start_line": start,
        "end_line": end,
        "language": "python",
        "base_commit_sha": head_sha,
        "file_blob_sha": context.blob_sha,
        "expected_original_code": retrieved,
        "proposed_code": proposed,
        "change_summary": payload["change_summary"],
        "rationale": payload["rationale"],
        "limitations": payload["limitations"],
        "confidence": payload["confidence"],
        "generation_status": "generated",
        "decision_status": "pending",
        "generated_at": datetime.now(timezone.utc),  # noqa: UP017 - Keep the existing datetime timezone style.
    }
    try:
        return SuggestedFix.model_validate(proposal_data)
    except (ValidationError, TypeError, ValueError):
        return None
